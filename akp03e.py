"""Minimal driver for the AJAZZ AKP03E (rev 2, USB 0300:3002) Stream Dock.

Protocol ported from the open-source mirajazz crate (github.com/4ndv/mirajazz),
protocol v3: 1024-byte packets, 64x64 JPEG key images rotated 90 deg clockwise.

Layout: 6 LCD keys (index 0-5), 3 plain buttons (6-8), 3 knobs (0 left, 1 middle, 2 right).
"""
import io
import threading
import time

import hid
from PIL import Image

VID, PID = 0x0300, 0x3002
USAGE_PAGE, USAGE = 0xFFA0, 1
PACKET = 1024
KEY_SIZE = 64
LCD_KEYS = 6
CRT = bytes([0x43, 0x52, 0x54, 0x00, 0x00])  # "CRT\0\0" command prefix

# input code -> (kind, index)
BUTTON_CODES = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 0x25: 6, 0x30: 7, 0x31: 8}
KNOB_PRESS = {0x33: 0, 0x35: 1, 0x34: 2}
KNOB_TURN = {0x90: (0, -1), 0x91: (0, 1), 0x50: (1, -1), 0x51: (1, 1), 0x60: (2, -1), 0x61: (2, 1)}


def find_path():
    for d in hid.enumerate(VID, PID):
        if d["usage_page"] == USAGE_PAGE and d["usage"] == USAGE:
            return d["path"]
    return None


class AKP03E:
    def __init__(self):
        path = find_path()
        if not path:
            raise RuntimeError("AKP03E not found (is it plugged in?)")
        self.dev = hid.device()
        self.dev.open_path(path)
        self.lock = threading.Lock()
        self._alive = None
        self.firmware = self.read_firmware()
        self._cmd(b"DIS")
        self._cmd(b"LIG\x00\x00\x00\x00")

    # --- low level -------------------------------------------------------
    def read_firmware(self):
        """Feature report 0x01 = firmware string. Vendor app does this on open."""
        for fn in (self.dev.get_feature_report, getattr(self.dev, "get_input_report", None)):
            if not fn:
                continue
            try:
                r = bytes(fn(0x01, 513))
                s = r[1:].split(b"\x00")[0].decode("ascii", "replace")
                if s:
                    return s
            except Exception:
                pass
        return None

    def _write(self, payload: bytes):
        buf = b"\x00" + payload.ljust(PACKET, b"\x00")
        with self.lock:
            n = self.dev.write(buf)
        if n < 0:
            raise IOError(f"HID write failed ({n})")

    def _cmd(self, body: bytes):
        self._write(CRT + body)

    # --- output ----------------------------------------------------------
    def brightness(self, percent: int):
        self._cmd(b"LIG\x00\x00" + bytes([max(0, min(100, percent))]))

    def keepalive(self):
        self._cmd(b"CONNECT")

    def start_keepalive(self, every=15):
        def loop():
            while self._alive:
                time.sleep(every)
                try:
                    self.keepalive()
                except Exception:
                    pass
        self._alive = True
        threading.Thread(target=loop, daemon=True).start()

    @staticmethod
    def encode(img: Image.Image) -> bytes:
        img = img.convert("RGB").resize((KEY_SIZE, KEY_SIZE), Image.LANCZOS)
        img = img.transpose(Image.Transpose.ROTATE_270)  # = 90 deg clockwise
        out = io.BytesIO()
        img.save(out, "JPEG", quality=90)
        return out.getvalue()

    def set_key(self, key: int, img: Image.Image, commit=True):
        """key 0-5. Call commit() after several set_key(commit=False)."""
        data = self.encode(img)
        n = len(data)
        self._cmd(b"BAT\x00\x00" + bytes([n >> 8, n & 0xFF, key + 1]))
        for i in range(0, n, PACKET):
            self._write(data[i:i + PACKET])
        if commit:
            self.commit()

    def commit(self):
        self._cmd(b"STP")

    def clear_key(self, key: int):
        self._cmd(b"CLE\x00\x00\x00" + bytes([key + 1]))
        self.commit()

    def clear_all(self):
        self._cmd(b"CLE\x00\x00\x00\xff")
        self.commit()

    # --- input -----------------------------------------------------------
    def read_raw(self, timeout_ms=100):
        with self.lock:
            pass  # reads don't need the write lock; keep API symmetric
        return bytes(self.dev.read(512, timeout_ms))

    def read_event(self, timeout_ms=100):
        """Returns (kind, index, value) or None.
        kind: 'key' (value 1 down / 0 up), 'knob_press' (1/0), 'knob_turn' (-1/+1)."""
        data = self.read_raw(timeout_ms)
        if not data:
            return None
        i = data.find(b"ACK")
        if i < 0 or len(data) < i + 11:
            return None
        code, state = data[i + 9], data[i + 10]
        if code in BUTTON_CODES:
            return ("key", BUTTON_CODES[code], 1 if state else 0)
        if code in KNOB_PRESS:
            return ("knob_press", KNOB_PRESS[code], 1 if state else 0)
        if code in KNOB_TURN:
            k, v = KNOB_TURN[code]
            return ("knob_turn", k, v)
        return ("unknown", code, state)

    def close(self):
        self._alive = False
        try:
            self.dev.close()
        except Exception:
            pass
