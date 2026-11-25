"""deckd - our own Stream Dock (AJAZZ AKP03E) daemon. Replaces the vendor app.

Controls: 6 LCD keys (k0-k5, per page), 3 plain buttons (global), 3 knobs (global,
turn + press). Config: config.json (edited via the web editor at http://127.0.0.1:8765).
"""
import asyncio
import ctypes
import hashlib
import html
import io
import json
import logging
import logging.handlers
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw, ImageFont

import akp03e
from spotify_api import Spotify

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.json")
ICON_DIR = os.path.join(BASE, "icons")
PORT = 8765
TILE = 128  # render size; driver downsizes to 64 for the deck

log = logging.getLogger("deckd")


def setup_logging():
    h = logging.handlers.RotatingFileHandler(os.path.join(BASE, "deckd.log"), maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    if sys.stdout and sys.stdout.isatty():
        log.addHandler(logging.StreamHandler(sys.stdout))
    log.setLevel(logging.INFO)


# ---------------------------------------------------------------- fonts / glyphs
def _font(name, size):
    for n in (name, "segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(os.path.join("C:/Windows/Fonts", n), size)
        except OSError:
            continue
    return ImageFont.load_default()


GLYPHS = {  # Segoe MDL2 Assets code points
    "prev": "\uE892", "next": "\uE893", "play": "\uE768", "pause": "\uE769",
    "shuffle": "\uE8B1", "repeat": "\uE8EE", "repeat1": "\uE8ED", "plus": "\uE710",
    "power": "\uE7E8", "home": "\uE80F", "music": "\uE8D6", "volume": "\uE767",
    "mute": "\uE74F", "globe": "\uE774", "gear": "\uE713", "folder": "\uE8B7",
    "server": "\uE968", "heart": "\uEB51", "heartfill": "\uEB52", "light": "\uE781", "camera": "\uE722",
    "print": "\uE749", "mail": "\uE715", "search": "\uE721", "lock": "\uE72E",
}


# ---------------------------------------------------------------- tile rendering
def wrap(draw, text, font, width, max_lines):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
            while draw.textlength(cur, font=font) > width and len(cur) > 1:
                cur = cur[:-1]
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip()[: max(1, len(lines[-1]) - 1)] + "…"
    return lines


def render(bg="#202020", fg="white", label="", icon=None, glyph=None, big=None, sub=None,
           text=None, image=None, badge=None, bar=None):
    img = Image.new("RGB", (TILE, TILE), bg)
    d = ImageDraw.Draw(img)
    lab_h = 30 if label else 0
    area = TILE - lab_h
    if image is not None:
        im = image.convert("RGB").resize((TILE, TILE), Image.LANCZOS)
        img.paste(im)
    if icon:
        try:
            ic = Image.open(os.path.join(ICON_DIR, icon)).convert("RGBA")
            s = area - 10 if label else TILE
            ic.thumbnail((s, s), Image.LANCZOS)
            img.paste(ic, ((TILE - ic.width) // 2, (area - ic.height) // 2 + (4 if label else 0)), ic)
        except Exception:
            pass
    if glyph and glyph in GLYPHS:
        f = _font("segmdl2.ttf", 56 if label else 68)
        d.text((TILE / 2, area / 2 + (4 if label else 0)), GLYPHS[glyph], font=f, fill=fg, anchor="mm")
    if big is not None:
        size = 46 if len(str(big)) <= 4 else 34 if len(str(big)) <= 6 else 26
        d.text((TILE / 2, area / 2 - (8 if sub else 0) + 4), str(big), font=_font("segoeuib.ttf", size), fill=fg, anchor="mm")
        if sub:
            d.text((TILE / 2, area / 2 + 24), str(sub), font=_font("segoeuib.ttf", 17), fill=fg, anchor="mm")
    if text:
        f = _font("segoeuib.ttf", 19)
        lines = []
        for part in text.split("\n"):
            lines += wrap(d, part, f, TILE - 12, 6 - len(lines))
        y = (TILE - 23 * len(lines)) / 2 + 11
        for ln in lines:
            d.text((TILE / 2, y), ln, font=f, fill=fg, anchor="mm")
            y += 23
    if bar is not None:
        d.rectangle([12, area - 20, TILE - 12, area - 8], outline=fg, width=2)
        d.rectangle([14, area - 18, 14 + (TILE - 28) * max(0, min(1, bar)), area - 10], fill=fg)
    if label:
        f = _font("segoeuib.ttf", 20)
        lab = wrap(d, label, f, TILE - 8, 1)[0] if label else ""
        d.text((TILE / 2, TILE - 16), lab, font=f, fill=fg, anchor="mm",
               stroke_width=2 if (icon or image is not None) else 0, stroke_fill="black")
    if badge:
        f = _font("segmdl2.ttf", 26)
        d.ellipse([TILE - 44, 4, TILE - 4, 44], fill=(0, 0, 0, 160))
        d.text((TILE - 24, 24), GLYPHS.get(badge, ""), font=f, fill="white", anchor="mm")
    return img


# ---------------------------------------------------------------- input helpers
user32 = ctypes.windll.user32
VK = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "esc": 0x1B,
      "escape": 0x1B, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "space": 0x20, "backspace": 0x08,
      "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pgup": 0x21,
      "pgdn": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "printscreen": 0x2C,
      "minus": 0xBD, "-": 0xBD, "plus": 0xBB, "=": 0xBB, "comma": 0xBC, "period": 0xBE,
      "playpause": 0xB3, "nexttrack": 0xB0, "prevtrack": 0xB1, "stop": 0xB2,
      "volup": 0xAF, "voldown": 0xAE, "volmute": 0xAD, "capslock": 0x14}
for i in range(1, 25):
    VK[f"f{i}"] = 0x6F + i
EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B, 0x2C}


def vk_of(name):
    n = name.strip().lower()
    if n in VK:
        return VK[n]
    if len(n) == 1 and n.isalnum():
        return ord(n.upper())
    raise ValueError(f"unknown key '{name}'")


def key_event(vk, up=False):
    flags = (0x2 if up else 0) | (0x1 if vk in EXTENDED else 0)
    user32.keybd_event(vk, 0, flags, 0)


def send_hotkey(combo):
    vks = [vk_of(k) for k in combo.split("+")]
    for v in vks:
        key_event(v)
    time.sleep(0.02)
    for v in reversed(vks):
        key_event(v, up=True)


# ---------------------------------------------------------------- audio (pycaw)
class Audio:
    def __init__(self):
        import comtypes
        comtypes.CoInitialize()
        from pycaw.pycaw import AudioUtilities
        self.AU = AudioUtilities

    def _master(self):
        return self.AU.GetSpeakers().EndpointVolume

    def _sessions(self, exe):
        return [s for s in self.AU.GetAllSessions() if s.Process and s.Process.name().lower() == exe.lower()]

    def change(self, target, delta_pct):
        if target == "master":
            ep = self._master()
            v = max(0.0, min(1.0, ep.GetMasterVolumeLevelScalar() + delta_pct / 100))
            ep.SetMasterVolumeLevelScalar(v, None)
            if v > 0 and ep.GetMute():
                ep.SetMute(0, None)
            return v
        ss = self._sessions(target)
        v = None
        for s in ss:
            sv = s.SimpleAudioVolume
            v = max(0.0, min(1.0, sv.GetMasterVolume() + delta_pct / 100))
            sv.SetMasterVolume(v, None)
        return v

    def toggle_mute(self, target):
        if target == "master":
            ep = self._master()
            m = not ep.GetMute()
            ep.SetMute(int(m), None)
            return m
        m = None
        for s in self._sessions(target):
            sv = s.SimpleAudioVolume
            m = not sv.GetMute()
            sv.SetMute(int(m), None)
        return m


# ---------------------------------------------------------------- media (Windows SMTC)
class Media:
    """Now-playing info + control via Windows media session; prefers Spotify."""

    def __init__(self):
        self.state = {"title": "", "artist": "", "status": 0, "shuffle": False, "repeat": 0, "art": None, "app": ""}
        self._art_key = None
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._poll())

    async def _session(self):
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
        if not hasattr(self, "_mgr"):
            self._mgr = await M.request_async()
        ss = list(self._mgr.get_sessions())
        for s in ss:
            if "spotify" in s.source_app_user_model_id.lower():
                return s
        return self._mgr.get_current_session()

    async def _poll(self):
        from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions
        while True:
            try:
                s = await self._session()
                if s is None:
                    self.state.update(title="", artist="", status=0, art=None, app="")
                else:
                    p = await s.try_get_media_properties_async()
                    pb = s.get_playback_info()
                    st = self.state
                    st.update(title=p.title or "", artist=p.artist or "", status=int(pb.playback_status),
                              shuffle=bool(pb.is_shuffle_active), app=s.source_app_user_model_id,
                              repeat=int(pb.auto_repeat_mode) if pb.auto_repeat_mode is not None else 0)
                    key = (p.title, p.artist, p.album_title)
                    if key != self._art_key and p.thumbnail is not None:
                        stream = await p.thumbnail.open_read_async()
                        buf = Buffer(stream.size)
                        await stream.read_async(buf, buf.capacity, InputStreamOptions.READ_AHEAD)
                        data = bytes(DataReader.from_buffer(buf).read_buffer(buf.length))
                        st["art"] = Image.open(io.BytesIO(data)).convert("RGB")
                        self._art_key = key
                    elif p.thumbnail is None:
                        st["art"] = None
            except Exception as e:
                log.debug("media poll: %s", e)
            await asyncio.sleep(1.0)

    async def _cmd(self, cmd):
        s = await self._session()
        if s is None:
            fallback = {"playpause": "playpause", "next": "nexttrack", "prev": "prevtrack"}.get(cmd)
            if fallback:
                send_hotkey(fallback)
            return
        if cmd == "playpause":
            await s.try_toggle_play_pause_async()
        elif cmd == "next":
            await s.try_skip_next_async()
        elif cmd == "prev":
            await s.try_skip_previous_async()
        elif cmd == "shuffle":
            await s.try_change_shuffle_active_async(not self.state["shuffle"])
        elif cmd == "repeat":
            from winrt.windows.media import MediaPlaybackAutoRepeatMode as R
            nxt = {0: R.LIST, 2: R.TRACK, 1: R.NONE}.get(self.state["repeat"], R.NONE)
            await s.try_change_auto_repeat_mode_async(nxt)

    def command(self, cmd):
        asyncio.run_coroutine_threadsafe(self._cmd(cmd), self.loop)


# ---------------------------------------------------------------- home lab (optional status.json feed)
class Lab:
    """Parses a Homer message-style status.json (config: "homelab": {"status_url": ...}). Optional."""

    def __init__(self, url):
        self.url = url
        self.d = {}
        self.ok = False
        if url:
            threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                raw = json.loads(urllib.request.urlopen(self.url, timeout=5).read())
                txt = re.sub(r"<[^>]+>", " ", html.unescape(raw.get("content", "")))
                txt = re.sub(r"\s+", " ", txt)
                g = lambda rx: re.search(rx, txt, re.I)
                d = {}
                if m := g(r"LINKS (\d+)/(\d+)"):
                    d["links"] = (int(m[1]), int(m[2]))
                if m := g(r"offline: (.+?)(?: CONTAINERS| ·|$)"):
                    d["links_off"] = m[1].strip()
                if m := g(r"CONTAINERS (\d+)/(\d+)"):
                    d["containers"] = (int(m[1]), int(m[2]))
                m = g(r"(\d+) UPDATES? AVAILABLE")
                d["updates"] = int(m[1]) if m else 0
                m = g(r"(\d+) JOBS? FAILED")
                d["jobs_failed"] = int(m[1]) if m else 0
                if m := g(r"SYSTEMS (\d+)/(\d+)"):
                    d["systems"] = (int(m[1]), int(m[2]))
                m = g(r"(\d+) NEEDS? ATTENTION")
                d["attention"] = int(m[1]) if m else 0
                if m := g(r"PI-HOLE ([\d,]+) queries .*?\(([\d.]+)%\)"):
                    d["pihole"] = (m[1], float(m[2]))
                self.d, self.ok = d, True
            except Exception as e:
                self.ok = False
                log.debug("lab poll: %s", e)
            time.sleep(60)


GREEN, AMBER, RED, GREY = "#1e8e3e", "#c77700", "#c5221f", "#3c4043"


# ---------------------------------------------------------------- url_status tiles (is this web service up?)
class UrlWatch:
    def __init__(self, daemon):
        self.daemon = daemon
        self.st = {}   # url -> (ok, ms)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            urls = {k.get("url") for p in self.daemon.cfg.get("pages", []) for k in p.get("keys", [])
                    if k and k.get("tile") == "url_status" and k.get("url")}
            for u in urls:
                t0 = time.time()
                try:
                    with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "deckd"}), timeout=6) as r:
                        ok = r.status < 500
                except urllib.error.HTTPError as e:
                    ok = e.code < 500
                except Exception:
                    ok = False
                self.st[u] = (ok, int((time.time() - t0) * 1000))
            time.sleep(60)

DYNAMIC_TILES = ["np_art", "np_title", "np_like", "np_shuffle", "np_repeat", "lab_links", "lab_containers",
                 "lab_jobs", "lab_systems", "lab_pihole", "url_status", "clock"]


# ---------------------------------------------------------------- the daemon
class Daemon:
    def __init__(self):
        self.cfg = self.load_cfg()
        self.page = self.page_index(self.cfg.get("home_page"))
        self.deck = None
        self.audio = Audio()
        self.media = Media()
        self.lab = Lab((self.cfg.get("homelab") or {}).get("status_url"))
        self.urlwatch = UrlWatch(self)
        self.sent = {}       # key -> hash of jpeg sent
        self.png = {}        # key -> png bytes (for web preview)
        self.sig = {}        # key -> render signature
        self.down = {}       # control id -> (t_down, hold_fired)
        self.toast = None    # (img, until)
        self.reload = False
        self.pending = []    # actions queued from web thread
        self.brightness = self.cfg.get("brightness", 80)
        self._last_track = 0
        sc = self.cfg.get("spotify") or {}
        self.spotify = Spotify(sc["client_id"], sc["redirect_uri"], sc["playlist_id"], log) if sc.get("client_id") else None
        if self.spotify:
            threading.Thread(target=self.spotify_loop, daemon=True).start()

    # ---- config
    def load_cfg(self):
        with open(CFG_PATH, encoding="utf-8") as f:
            return json.load(f)

    def page_index(self, name):
        for i, p in enumerate(self.cfg["pages"]):
            if p["name"] == name:
                return i
        return 0

    def keys(self):
        ks = self.cfg["pages"][self.page].get("keys", [])
        return (ks + [None] * 6)[:6]

    # ---- rendering
    def tile_for(self, i, kc):
        if self.toast and i == 5 and time.time() < self.toast[1]:
            return ("toast", self.toast[2]), self.toast[0]
        if not kc:
            return ("blank",), lambda: render(bg="#000000")
        t = kc.get("tile")
        bg, fg, label = kc.get("bg", "#202020"), kc.get("fg", "white"), kc.get("label", "")
        ms, ld = self.media.state, self.lab.d
        if t == "np_art":
            badge = "pause" if ms["status"] == 5 else None
            if ms["art"] is not None:
                return (t, id(ms["art"]), badge), lambda: render(image=ms["art"], badge=badge)
            return (t, ms["status"]), lambda: render(bg="#191414", fg="#1db954", glyph="music", label="Spotify")
        if t == "np_title":
            txt = f"{ms['title']}\n{ms['artist']}" if ms["title"] else "Nothing playing"
            return (t, txt), lambda: render(bg="#191414", fg="white", text=txt)
        if t == "np_like":
            sp = self.spotify
            if not sp:
                return (t, "nocfg"), lambda: render(bg=GREY, glyph="heart", label="Set up")
            if not sp.signed_in:
                return (t, "signin"), lambda: render(bg=GREY, glyph="heart", label="Sign in")
            cur = sp.current
            liked = sp.in_playlist()
            name = cur["name"] if cur else "Nothing"
            return (t, name, liked), lambda: render(bg="#191414", fg="#e22134" if liked else "white",
                                                    glyph="heartfill" if liked else "heart", label=name)
        if t == "np_shuffle":
            on = ms["shuffle"]
            return (t, on), lambda: render(bg="#1db954" if on else GREY, glyph="shuffle", label=label)
        if t == "np_repeat":
            r = ms["repeat"]
            return (t, r), lambda: render(bg="#1db954" if r else GREY, glyph="repeat1" if r == 1 else "repeat",
                                          label={0: "Repeat off", 1: "Repeat 1", 2: "Repeat all"}.get(r, label))
        if t and t.startswith("lab_"):
            return self.lab_tile(t, ld, label)
        if t == "url_status":
            st = self.urlwatch.st.get(kc.get("url"))
            if st is None:
                return (t, "wait", label), lambda: render(bg=GREY, big="…", label=label)
            ok, ms = st
            return (t, ok, ms // 100, label), lambda: render(bg=GREEN if ok else RED, big="UP" if ok else "DOWN",
                                                              sub=f"{ms} ms" if ok else "offline", label=label)
        if t == "clock":
            now = time.strftime("%H:%M")
            return (t, now), lambda: render(bg=bg, fg=fg, big=now, sub=time.strftime("%a %d"))
        sig = ("static", json.dumps(kc, sort_keys=True))
        return sig, lambda: render(bg=bg, fg=fg, label=label, icon=kc.get("icon"), glyph=kc.get("glyph"),
                                   big=kc.get("big"))

    def lab_tile(self, t, d, label):
        if not self.lab.ok:
            return (t, "off"), lambda: render(bg=GREY, big="?", sub="no data", label=label)
        if t == "lab_links" and "links" in d:
            a, b = d["links"]
            sub = d.get("links_off", "")[:14] if a < b else "online"
            return (t, a, b), lambda: render(bg=GREEN if a == b else AMBER, big=f"{a}/{b}", sub=sub, label=label)
        if t == "lab_containers" and "containers" in d:
            a, b = d["containers"]
            u = d.get("updates", 0)
            return (t, a, b, u), lambda: render(bg=GREEN if a == b else AMBER, big=f"{a}/{b}",
                                                sub=f"{u} updates" if u else "running", label=label)
        if t == "lab_jobs":
            n = d.get("jobs_failed", 0)
            return (t, n), lambda: render(bg=RED if n else GREEN, big=str(n), sub="failed" if n else "all ok", label=label)
        if t == "lab_systems" and "systems" in d:
            a, b = d["systems"]
            att = d.get("attention", 0)
            return (t, a, b, att), lambda: render(bg=RED if att else GREEN if a == b else AMBER, big=f"{a}/{b}",
                                                  sub=f"{att} need attn" if att else "ok", label=label)
        if t == "lab_pihole" and "pihole" in d:
            q, pct = d["pihole"]
            return (t, q, pct), lambda: render(bg="#9b1c31", big=f"{pct:.0f}%", sub="blocked", label=label)
        return (t, "na"), lambda: render(bg=GREY, big="–", label=label)

    def refresh(self, force=False):
        changed = []
        for i, kc in enumerate(self.keys()):
            sig, fn = self.tile_for(i, kc)
            if not force and self.sig.get(i) == sig:
                continue
            img = fn()
            self.sig[i] = sig
            b = io.BytesIO()
            img.save(b, "PNG")
            self.png[i] = b.getvalue()
            changed.append((i, img))
        if changed and self.deck:
            for i, img in changed:
                self.deck.set_key(i, img, commit=False)
            self.deck.commit()

    def show_toast(self, title, value=None, glyph="volume"):
        sig = (title, None if value is None else round(value, 2))
        img = render(bg="#0b57d0", big=title if value is None else f"{round(value * 100)}%",
                     bar=value, label=title if value is not None else "")
        self.toast = (lambda: img, time.time() + 1.5, sig)

    # ---- actions
    def do(self, a):
        if not a:
            return
        t = a.get("type")
        try:
            if t == "hotkey":
                send_hotkey(a["keys"])
            elif t == "sequence":
                for step in a.get("steps", []):
                    if step.startswith("pause:"):
                        time.sleep(int(step[6:]) / 1000)
                    else:
                        send_hotkey(step)
                        time.sleep(0.05)
            elif t == "run":
                tgt = a["target"]
                if tgt.lower().startswith("shell:"):
                    subprocess.Popen(["explorer.exe", tgt])
                elif a.get("args"):
                    subprocess.Popen([tgt] + a["args"].split(), cwd=os.path.dirname(tgt) or None)
                else:
                    os.startfile(tgt)
            elif t == "url":
                webbrowser.open(a["url"])
            elif t == "media":
                self.media.command(a["cmd"])
            elif t == "page":
                to, n = a.get("to", "next"), len(self.cfg["pages"])
                self.page = {"next": (self.page + 1) % n, "prev": (self.page - 1) % n,
                             "home": self.page_index(self.cfg.get("home_page"))}.get(to, None) \
                    if to in ("next", "prev", "home") else self.page_index(to)
                self.toast = None
                self.refresh(force=True)
            elif t == "like":
                threading.Thread(target=self.like, daemon=True).start()
            elif t == "brightness":
                cyc = a.get("cycle", [20, 50, 80, 100])
                nxt = [c for c in cyc if c > self.brightness]
                self.brightness = nxt[0] if nxt else cyc[0]
                self.deck and self.deck.brightness(self.brightness)
            elif t == "volume":
                v = self.audio.change(a.get("target", "master"), a.get("step", 2) * a.get("_dir", 1))
                if v is not None:
                    name = "Volume" if a.get("target", "master") == "master" else a["target"].split(".")[0]
                    self.show_toast(name, v)
            elif t == "mute":
                m = self.audio.toggle_mute(a.get("target", "master"))
                if m is not None:
                    self.show_toast("MUTED" if m else "ON")
            elif t == "track":
                now = time.time()
                if now - self._last_track > 0.5:
                    self._last_track = now
                    self.media.command("next" if a.get("_dir", 1) > 0 else "prev")
            else:
                log.warning("unknown action %s", a)
        except Exception:
            log.error("action %s failed:\n%s", a, traceback.format_exc())

    def control_cfg(self, ctl):
        kind, idx = ctl[0], int(ctl[1:])
        if kind == "k":
            return self.keys()[idx] or {}
        if kind == "b":
            return (self.cfg.get("buttons", []) + [{}] * 3)[idx] or {}
        return (self.cfg.get("knobs", []) + [{}] * 3)[idx] or {}

    def on_event(self, ev):
        kind, idx, val = ev
        if kind == "key":
            ctl = f"k{idx}" if idx < 6 else f"b{idx - 6}"
            c = self.control_cfg(ctl)
        elif kind == "knob_press":
            ctl = f"n{idx}"
            c = dict(self.control_cfg(ctl))
            c = {"action": c.get("press"), "hold": c.get("hold")}
        elif kind == "knob_turn":
            turn = self.control_cfg(f"n{idx}").get("turn")
            if turn:
                self.do(dict(turn, _dir=val))
            return
        else:
            return
        if val:  # down
            if c.get("hold"):
                self.down[ctl] = [time.time(), False, c]
            else:
                self.do(c.get("action"))
        else:  # up
            st = self.down.pop(ctl, None)
            if st and not st[1]:
                self.do(st[2].get("action"))

    def check_holds(self):
        hold_s = self.cfg.get("hold_ms", 700) / 1000
        for ctl, st in list(self.down.items()):
            if not st[1] and time.time() - st[0] >= hold_s:
                st[1] = True
                self.do(st[2].get("hold"))

    # ---- spotify web api
    def spotify_loop(self):
        last_title, last_check = None, 0
        while True:
            try:
                if self.spotify.signed_in:
                    t = self.media.state["title"]
                    if t != last_title or time.time() - last_check > 30:
                        last_title, last_check = t, time.time()
                        self.spotify.refresh_current()
                        self.spotify.refresh_playlist()
                        self.spotify.error = None
            except Exception as e:
                self.spotify.error = str(e)[:60]
                log.warning("spotify: %s", e)
                last_check = time.time()
            time.sleep(2)

    def like(self):
        sp = self.spotify
        if not sp or not sp.signed_in:
            self.show_toast("Sign in")
            webbrowser.open(f"http://127.0.0.1:{PORT}/spotify/login")
            return
        try:
            status, text = sp.add_current()
        except Exception as e:
            log.error("like failed: %s", e)
            status, text = "error", "Failed"
        self.show_toast(text)
        log.info("like: %s %s", status, sp.current and sp.current.get("name"))

    # ---- main loop
    def run(self):
        threading.Thread(target=serve_web, args=(self,), daemon=True).start()
        while True:
            try:
                self.deck = akp03e.AKP03E()
                log.info("deck connected, firmware %s", self.deck.firmware)
                self.deck.brightness(self.brightness)
                self.deck.clear_all()
                self.deck.start_keepalive()
                self.sig.clear()
                self.refresh(force=True)
                last = 0
                while True:
                    ev = self.deck.read_event(40)
                    if ev:
                        self.on_event(ev)
                    self.check_holds()
                    while self.pending:
                        self.do(self.pending.pop(0))
                    if self.reload:
                        self.reload = False
                        self.cfg = self.load_cfg()
                        self.page = min(self.page, len(self.cfg["pages"]) - 1)
                        self.refresh(force=True)
                    if time.time() - last > 0.25:
                        last = time.time()
                        if self.toast and time.time() >= self.toast[1]:
                            self.toast = None
                        self.refresh()
            except Exception as e:
                log.warning("deck loop: %s", e)
                try:
                    self.deck and self.deck.close()
                except Exception:
                    pass
                self.deck = None
                time.sleep(2)


# ---------------------------------------------------------------- web editor
def serve_web(daemon):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                with open(os.path.join(BASE, "editor.html"), encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/config":
                return self._send(200, daemon.cfg)
            if u.path == "/api/state":
                return self._send(200, {"page": daemon.page, "connected": daemon.deck is not None,
                                        "tiles": DYNAMIC_TILES, "glyphs": sorted(GLYPHS),
                                        "icons": sorted(f for f in os.listdir(ICON_DIR) if f.lower().endswith(".png"))})
            if u.path.startswith("/img/"):
                i = int(u.path[5:].split(".")[0])
                return self._send(200, daemon.png.get(i, b""), "image/png")
            if u.path.startswith("/icons/"):
                fn = os.path.basename(u.path[7:])
                p = os.path.join(ICON_DIR, fn)
                if os.path.isfile(p):
                    with open(p, "rb") as f:
                        return self._send(200, f.read(), "image/png")
            if u.path == "/spotify/login" and daemon.spotify:
                self.send_response(302)
                self.send_header("Location", daemon.spotify.login_url())
                self.end_headers()
                return
            if u.path == "/spotify/callback" and daemon.spotify:
                ok, text = daemon.spotify.callback({k: v[0] for k, v in q.items()})
                return self._send(200 if ok else 400, f"<p style='font:18px sans-serif'>{html.escape(text)}</p>",
                                  "text/html; charset=utf-8")
            if u.path == "/api/page":
                daemon.pending.append({"type": "page", "to": q.get("name", ["home"])[0]})
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if u.path == "/api/config":
                try:
                    cfg = json.loads(body)
                    assert isinstance(cfg.get("pages"), list) and cfg["pages"]
                except Exception as e:
                    return self._send(400, {"error": f"bad config: {e}"})
                if os.path.exists(CFG_PATH):
                    os.replace(CFG_PATH, CFG_PATH + ".bak")
                with open(CFG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
                daemon.reload = True
                return self._send(200, {"ok": True})
            if u.path == "/api/icon":
                name = re.sub(r"[^a-zA-Z0-9_.-]", "_", q.get("name", ["icon"])[0])
                name = os.path.splitext(name)[0] + ".png"
                try:
                    im = Image.open(io.BytesIO(body)).convert("RGBA")
                    im.thumbnail((256, 256))
                    im.save(os.path.join(ICON_DIR, name))
                except Exception as e:
                    return self._send(400, {"error": f"not an image: {e}"})
                return self._send(200, {"ok": True, "name": name})
            if u.path == "/api/test":
                try:
                    daemon.pending.append(json.loads(body))
                except Exception:
                    return self._send(400, {"error": "bad action"})
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


def main():
    setup_logging()
    # single instance: hold a lock port
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", PORT + 1))
    except OSError:
        log.error("deckd already running")
        return
    log.info("deckd starting")
    os.makedirs(ICON_DIR, exist_ok=True)
    example = os.path.join(BASE, "config.example.json")
    if not os.path.exists(CFG_PATH) and os.path.exists(example):
        import shutil
        shutil.copyfile(example, CFG_PATH)
        log.info("created config.json from config.example.json")
    Daemon().run()


if __name__ == "__main__":
    main()
