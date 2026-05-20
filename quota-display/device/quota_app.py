"""Cardputer quota display.

Connects to Wi-Fi, polls the host server every POLL_INTERVAL_S seconds,
and feeds the response straight into BuddyUI.update_heartbeat() — the
existing renderer already knows how to draw `claude_*_pct` /
`claude_*_reset_s` fields, so this app is mostly transport glue.
"""

import time
import network
import urequests
import M5
from hardware import MatrixKeyboard

import config
from buddy_ui_cp import BuddyUI


def _connect_wifi(ssid: str, password: str, timeout_s: int = 20) -> bool:
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    if sta.isconnected():
        return True
    sta.connect(ssid, password)
    deadline = time.ticks_add(time.ticks_ms(), timeout_s * 1000)
    while not sta.isconnected():
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            return False
        time.sleep_ms(200)
    return True


def _fetch_heartbeat(url: str):
    try:
        r = urequests.get(url, timeout=10)
    except Exception as e:
        print("fetch error:", e)
        return None
    try:
        if r.status_code != 200:
            print("http", r.status_code)
            return None
        return r.json()
    finally:
        try:
            r.close()
        except Exception:
            pass


def _key_text(k):
    if k is None:
        return None
    if isinstance(k, str):
        return k.lower()
    if isinstance(k, int) and 0 <= k <= 0x7F:
        try:
            return chr(k).lower()
        except Exception:
            return None
    return None


def _view_for_key(k, current: str):
    """Map Cardputer keys to quota panels.

    Cardputer-Adv arrow-labeled keys report printable glyphs:
    comma is left, slash is right. Tab toggles when present; C/X are
    easy fallbacks on the QWERTY.
    """
    ch = _key_text(k)
    if ch is None:
        return None
    if ch in ("\t", " "):
        return "codex" if current == "claude" else "claude"
    if ch in (",", "a", "c"):
        return "claude"
    if ch in ("/", "d", "x"):
        return "codex"
    return None


def main():
    M5.begin()
    ui = BuddyUI()
    ui.set_connection("advertising")  # idle splash until first poll
    kb = MatrixKeyboard()

    ok = _connect_wifi(config.WIFI_SSID, config.WIFI_PASS)
    if not ok:
        ui.flash_toast("Wi-Fi failed", 0xFF0000)
        # Stay on the failure toast — power cycle to retry.
        while True:
            time.sleep(60)

    ui.set_connection("connected")
    ui.update_identity("Quota", "")

    interval = max(5, int(getattr(config, "POLL_INTERVAL_S", 30)))
    next_poll = 0
    view = "claude"
    last_hb = None
    last_toggle = 0

    while True:
        try:
            kb.tick()
        except Exception:
            pass
        k = kb.get_key()
        next_view = _view_for_key(k, view)
        now = time.ticks_ms()
        if next_view is not None and next_view != view and time.ticks_diff(now, last_toggle) > 250:
            view = next_view
            last_toggle = now
            if last_hb is not None:
                hb = dict(last_hb)
                hb["usage_view"] = view
                ui.update_heartbeat(hb)

        if next_poll == 0 or time.ticks_diff(now, next_poll) >= 0:
            hb = _fetch_heartbeat(config.SERVER_URL)
            if hb is not None:
                hb["usage_view"] = view
                last_hb = hb
                ui.update_heartbeat(hb)
            next_poll = time.ticks_add(now, interval * 1000)
        time.sleep_ms(200)


if __name__ == "__main__":
    main()
