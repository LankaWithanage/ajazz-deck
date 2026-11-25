"""Test: numbers each LCD key, then prints every key/knob event (Ctrl+C to stop).
Run with the Stream Dock AJAZZ app fully closed."""
import sys
import time

from PIL import Image, ImageDraw, ImageFont

from akp03e import AKP03E

COLORS = ["#c0392b", "#d35400", "#27ae60", "#2980b9", "#8e44ad", "#2c3e50"]


def tile(text, bg):
    img = Image.new("RGB", (64, 64), bg)
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("arialbd.ttf", 36)
    except OSError:
        f = ImageFont.load_default()
    d.text((32, 30), text, fill="white", font=f, anchor="mm")
    d.polygon([(4, 4), (16, 4), (4, 16)], fill="white")  # top-left marker for orientation
    return img


def main():
    deck = AKP03E()
    deck.brightness(80)
    for k in range(6):
        deck.set_key(k, tile(str(k), COLORS[k]), commit=False)
    deck.commit()
    deck.start_keepalive()
    print("Keys numbered 0-5. Press keys / turn knobs. Ctrl+C to quit.", flush=True)
    raw = "--raw" in sys.argv
    end = time.time() + float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1][0].isdigit() else None
    try:
        while end is None or time.time() < end:
            if raw:
                data = deck.read_raw(200)
                if data:
                    print(data[:16].hex(" "), flush=True)
            else:
                ev = deck.read_event(200)
                if ev:
                    print(ev, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        deck.close()


if __name__ == "__main__":
    main()
