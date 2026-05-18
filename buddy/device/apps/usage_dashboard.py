"""Usage dashboard — live CLAUDE / CODEX 5h + 7d quotas on the LCD.

Polls ``GET <WORKER_BASE>/usage`` on the local_relay (or any compatible
backend) and renders the two-card design from
``buddy/references/ui_preview_live.html``. Two screens — CLAUDE and
CODEX — toggled with the left / right arrow keys.

Layout (240x135 px)::

    +------------------------------------------------+
    | (*)        CLAUDE                        [###] |  header (15 px)
    +------------------------------------------------+
    | 5H                              55% USED       |
    | [####################----------------------]   |  upper block
    | RESETS IN 4 HR 1 MIN                           |
    |                                                |
    | 7D                              12% USED       |
    | [####------------------------------------]     |  lower block
    | RESETS TUE 9:00 AM                             |
    +------------------------------------------------+
    |  <-  CLAUDE / CODEX  ->            Q/ESC back  |  hint strip
    +------------------------------------------------+

Exit: Q or ESC, same as the rest of the app suite.
"""

import time

import M5
import machine
from hardware import MatrixKeyboard

try:
    from apps.config import WORKER_BASE, DEVICE_SECRET
except Exception:
    WORKER_BASE = ""
    DEVICE_SECRET = ""


_LCD = M5.Lcd

_W = 240
_H = 135

# Palette — same as the rest of the bundle.
_BLACK    = 0x000000
_CREAM    = 0xF0EEE6
_ORANGE   = 0xCC785C
_DARK     = 0x1F1F1F
_GRAY_DIM = 0x333333
_GRAY_MID = 0x777777
_BAR_BG   = 0xD9D6CC

# Refresh every 30 s; long enough to be polite to the relay, short
# enough that you see numbers tick up while you watch.
_REFRESH_INTERVAL_MS = 30_000


def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("usage: setFont fallback:", e)


def _clip(s, max_w):
    while _LCD.textWidth(s) > max_w and len(s) > 1:
        s = s[:-1]
    return s


# ---------- Network ---------------------------------------------------------

def _fetch_usage():
    """Return parsed /usage JSON dict, or None on failure."""
    if not WORKER_BASE:
        return None
    try:
        import requests
    except ImportError:
        try:
            import urequests as requests
        except ImportError:
            print("usage: no requests module")
            return None
    headers = {"x-device-secret": DEVICE_SECRET} if DEVICE_SECRET else {}
    try:
        r = requests.get(WORKER_BASE.rstrip("/") + "/usage", headers=headers, timeout=8)
    except Exception as e:
        print("usage: fetch err:", e)
        return None
    try:
        if r.status_code != 200:
            return None
        return r.json()
    finally:
        try:
            r.close()
        except Exception:
            pass


# ---------- Rendering -------------------------------------------------------

def _draw_chrome(screen_name):
    """Clear the screen and draw header + hint strip for a side."""
    _LCD.fillRect(0, 0, _W, _H, _CREAM)

    # Header band (15 px) — colored differently than the rest of the
    # app suite because the dashboard's whole frame is cream not black.
    # We use a 1 px hairline below the header instead of a tinted band
    # so the design stays tight at 240x135.
    _LCD.setTextSize(1)

    # Logo placeholder — a 9-px asterisk in the upper left for CLAUDE,
    # an OpenAI knot glyph would be nicer but the LCD font has no good
    # equivalent, so we use the same star for CODEX in a darker color.
    cx, cy = 8, 7
    color = _ORANGE if screen_name == "claude" else _DARK
    for dx, dy in ((-3, 0), (3, 0), (0, -3), (0, 3),
                   (-2, -2), (2, -2), (-2, 2), (2, 2)):
        _LCD.fillRect(cx + dx, cy + dy, 2, 2, color)

    title = screen_name.upper()
    _LCD.setTextColor(_BLACK, _CREAM)
    _LCD.drawString(title, (_W - _LCD.textWidth(title)) // 2, 4)

    # Battery icon (right-aligned). We don't have a battery reading
    # exposed here yet — draw a generic full-bar so the layout matches
    # the preview.
    bx, by = _W - 26, 4
    _LCD.drawRect(bx, by, 18, 8, _BLACK)
    _LCD.fillRect(bx + 19, by + 2, 2, 4, _BLACK)
    _LCD.fillRect(bx + 2, by + 2, 14, 4, _BLACK)

    # Hairline under header.
    _LCD.fillRect(0, 15, _W, 1, _GRAY_DIM)


def _draw_hint(screen_name):
    """Bottom hint strip — also lives on cream, so use a tinted band
    so the user notices it."""
    _LCD.fillRect(0, _H - 12, _W, 12, _DARK)
    _LCD.setTextColor(_CREAM, _DARK)
    _LCD.setTextSize(1)
    nav = "<-  CLAUDE / CODEX  ->"
    _LCD.drawString(nav, 6, _H - 10)
    back = "Q/ESC back"
    _LCD.drawString(back, _W - _LCD.textWidth(back) - 6, _H - 10)


def _draw_row(y, label, pct, reset_str, accent):
    """One usage row.

    Layout within the row::

        label                                  pct% USED
        [bar bar bar bar---------------------------]
        RESETS <when>
    """
    _LCD.setTextSize(1)
    _LCD.setTextColor(_BLACK, _CREAM)
    _LCD.drawString(label, 6, y)

    pct_text = "{}% USED".format(int(pct))
    _LCD.drawString(pct_text, _W - _LCD.textWidth(pct_text) - 6, y)

    # Bar.
    bar_y = y + 14
    bar_w = _W - 12
    _LCD.fillRect(6, bar_y, bar_w, 6, _BAR_BG)
    fill_w = max(0, min(bar_w, int(bar_w * int(pct) / 100)))
    if fill_w > 0:
        _LCD.fillRect(6, bar_y, fill_w, 6, accent)

    # Footer.
    _LCD.setTextColor(_GRAY_MID, _CREAM)
    foot = "RESETS " + (reset_str or "").upper()
    foot = _clip(foot, _W - 12)
    _LCD.drawString(foot, 6, bar_y + 10)


def _draw_screen(screen_name, data):
    """Render the whole 240x135 frame for one side."""
    _draw_chrome(screen_name)
    accent = _ORANGE if screen_name == "claude" else _BLACK

    block = (data or {}).get(screen_name) or {}
    h5 = block.get("h5") or {}
    d7 = block.get("d7") or {}

    if not block:
        # No data yet — show a placeholder so the dashboard isn't blank.
        _LCD.setTextColor(_GRAY_MID, _CREAM)
        _LCD.drawString("(no data)", 6, 40)
    else:
        _draw_row(
            y=22,
            label="5H",
            pct=h5.get("pct", 0),
            reset_str=h5.get("reset", ""),
            accent=accent,
        )
        _draw_row(
            y=64,
            label="7D",
            pct=d7.get("pct", 0),
            reset_str=d7.get("reset", ""),
            accent=accent,
        )

    _draw_hint(screen_name)


def _draw_loading():
    _LCD.fillRect(0, 0, _W, _H, _CREAM)
    _LCD.setTextColor(_BLACK, _CREAM)
    _LCD.setTextSize(1)
    msg = "loading usage..."
    _LCD.drawString(msg, (_W - _LCD.textWidth(msg)) // 2, (_H - 9) // 2)
    if not WORKER_BASE:
        hint = "set WORKER_BASE in apps/config.py"
        _LCD.setTextColor(_GRAY_MID, _CREAM)
        _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H // 2 + 14)


# ---------- Main loop -------------------------------------------------------

def main():
    M5.begin()
    _set_font()
    _draw_loading()

    kb = MatrixKeyboard()

    side = "claude"           # which card is on screen
    data = None               # last successful /usage response
    last_fetch = 0            # ticks_ms when we last fetched

    while True:
        now = time.ticks_ms()

        # Refresh data when due — also on first iteration.
        if data is None or time.ticks_diff(now, last_fetch) >= _REFRESH_INTERVAL_MS:
            fresh = _fetch_usage()
            if fresh is not None:
                data = fresh
                _draw_screen(side, data)
            elif data is None:
                _draw_loading()
            last_fetch = now

        kb.tick()
        key = kb.get_key()
        if key:
            k = key.lower() if isinstance(key, str) else key
            if k in ("q", "\x1b", 0x1b):  # Q or ESC
                _LCD.fillScreen(_BLACK)
                time.sleep_ms(60)
                machine.reset()
                return
            elif k in ("a", "h", "left", 0x44, 0x80, 0x82):
                if side != "claude":
                    side = "claude"
                    _draw_screen(side, data)
            elif k in ("d", "l", "right", 0x43, 0x81, 0x83):
                if side != "codex":
                    side = "codex"
                    _draw_screen(side, data)
            elif k in ("r", "R"):
                last_fetch = 0  # force refresh next loop

        time.sleep_ms(40)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import sys
        sys.print_exception(e)
        time.sleep(2)
        machine.reset()
