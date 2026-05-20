"""Tiny text chat overlay for the quota display.

Y from the quota screens opens this module. It uses the existing
Push-to-Claude Worker `/ask-text` endpoint, then returns to the quota
screens when the user presses Q or ESC.
"""

import json
import time

import M5
import urequests
from hardware import MatrixKeyboard

import config


_LCD = M5.Lcd
_W = 240
_H = 135

_BLACK = 0x000000
_CREAM = 0xF0EEE6
_ORANGE = 0xCC785C
_DARK = 0x1F1F1F
_GRAY = 0x777777
_RED = 0xFF4040


def _worker_base():
    return str(getattr(config, "CHAT_WORKER_BASE", "") or "").rstrip("/")


def _device_secret():
    return str(getattr(config, "CHAT_DEVICE_SECRET", "") or "")


def _key_text(k):
    if k is None:
        return None
    if isinstance(k, str):
        return k
    if isinstance(k, int) and 0 <= k <= 0x7F:
        try:
            return chr(k)
        except Exception:
            return None
    return None


def _is_exit(k):
    if isinstance(k, int) and k == 0x1B:
        return True
    ch = _key_text(k)
    return ch is not None and ch.lower() == "q"


def _is_enter(k):
    return k in ("\n", "\r", 10, 13)


def _is_backspace(k):
    return k in ("\b", 8, 0x7F)


def _printable(k):
    ch = _key_text(k)
    if ch is None or len(ch) != 1:
        return None
    code = ord(ch)
    if 32 <= code <= 126:
        return ch
    return None


def _draw_header(title, hint="ENTER send  Q back"):
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 18, _DARK)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString(title, 6, 5)
    _LCD.fillRect(0, _H - 16, _W, 16, _DARK)
    _LCD.setTextColor(_GRAY, _DARK)
    _LCD.drawString(hint[:34], 6, _H - 12)


def _wrap(text, max_chars):
    words = str(text or "").replace("\r", " ").split()
    lines = []
    line = ""
    for word in words:
        if len(word) > max_chars:
            if line:
                lines.append(line)
                line = ""
            while len(word) > max_chars:
                lines.append(word[:max_chars])
                word = word[max_chars:]
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= max_chars:
            line += " " + word
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines or [""]


def _draw_lines(title, lines, color=_CREAM, hint="ENTER send  Q back"):
    _draw_header(title, hint)
    _LCD.setTextSize(1)
    _LCD.setTextColor(color, _BLACK)
    y = 24
    for line in lines[:9]:
        _LCD.drawString(line, 6, y)
        y += 11


def _draw_input(buf, provider):
    title = "{} CHAT".format(str(provider or "AI").upper())
    lines = ["Ask:"] + _wrap(buf + "_", 32)
    _draw_lines(title, lines, _CREAM, "ENTER send  Q back")


def _draw_reply(prompt, reply, provider):
    title = "{} REPLY".format(str(provider or "AI").upper())
    lines = []
    lines += _wrap("You: " + str(prompt or ""), 32)[:2]
    lines.append("")
    lines += _wrap("AI: " + str(reply or ""), 32)
    _draw_lines(title, lines, _CREAM, "Y new  Q back")


def _draw_error(msg):
    _draw_lines("CHAT ERROR", _wrap(str(msg or "error"), 32), _RED, "Q back")


def _post_text(prompt):
    base = _worker_base()
    secret = _device_secret()
    if not base or not secret:
        raise RuntimeError("Set CHAT_WORKER_BASE and CHAT_DEVICE_SECRET in config.py")
    body = json.dumps({"prompt": prompt})
    headers = {
        "content-type": "application/json",
        "x-device-secret": secret,
    }
    r = urequests.post(base + "/ask-text", data=body, headers=headers, timeout=45)
    try:
        if r.status_code != 200:
            raise RuntimeError("worker {}".format(r.status_code))
        data = r.json()
    finally:
        try:
            r.close()
        except Exception:
            pass
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(data.get("error"))
    return data.get("response", "") if isinstance(data, dict) else ""


def run(provider="claude"):
    kb = MatrixKeyboard()
    time.sleep_ms(250)
    state = "typing"
    buf = ""
    last_prompt = ""
    last_reply = ""
    _draw_input(buf, provider)

    while True:
        try:
            kb.tick()
        except Exception:
            pass
        k = kb.get_key()
        if k is None:
            time.sleep_ms(60)
            continue

        if _is_exit(k):
            return

        if state == "showing":
            ch = _key_text(k)
            if ch is not None and ch.lower() == "y":
                state = "typing"
                buf = ""
                _draw_input(buf, provider)
            time.sleep_ms(80)
            continue

        if _is_enter(k):
            prompt = buf.strip()
            if prompt:
                last_prompt = prompt
                _draw_lines("SENDING", ["Talking to AI..."], _ORANGE, "please wait")
                try:
                    last_reply = _post_text(prompt)
                    state = "showing"
                    _draw_reply(last_prompt, last_reply, provider)
                except Exception as e:
                    state = "error"
                    _draw_error(str(e)[:180])
            continue

        if state == "error":
            time.sleep_ms(80)
            continue

        if _is_backspace(k):
            buf = buf[:-1]
            _draw_input(buf, provider)
            continue

        ch = _printable(k)
        if ch is not None and len(buf) < 180:
            buf += ch
            _draw_input(buf, provider)
        time.sleep_ms(80)
