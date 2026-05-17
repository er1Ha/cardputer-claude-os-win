"""Render the buddy's state to the 240x135 Cardputer-Adv LCD.

### Rendering API choice

We use **`M5.Lcd.drawString(text, x, y)`** and **`M5.Lcd.textWidth(text)`**
everywhere instead of `setCursor()+print()` + char-count estimates.
Two reasons:

1. **Cursor origin on this build is baseline, not top-left.**
   `setCursor(x, y)` sets (x, baseline_y). DejaVu9 has ~8 px of ascender
   above the baseline, so text we thought was landing at y=118 was
   actually rendering around y=110..120 — half above the hint strip's
   DARK background. `drawString` uses the driver's text datum, which
   defaults to TL_DATUM (top-left), so (x, y) is the top-left corner
   of the glyph cell. That matches how the rest of the layout math
   thinks about rectangles.

2. **Proportional font widths are not `_CHAR_W * len(text)`.**
   Measured on hardware with DejaVu9 at size 1:

        "Y once"            = 38 px  (not 6*6=36)
        "Q = back to menu"  = 103 px
        "100%"              = 31 px  (not 4*6=24 — this is why the
                                      '%' was wrapping to a second
                                      line when we did x = 240-6-24)
        "Claude Buddy"      = 79 px
        "Settings > Buddy"  = 101 px

   So we call `_LCD.textWidth(...)` for every centering or right-
   alignment calculation. It's a cheap call (pure lookup over the
   font's advance table) and eliminates the off-by-a-few-pixels
   rendering glitches that show up when a layout guesses wrong.

### Font selection

`M5.Lcd.FONTS.DejaVu9` — the smallest DejaVu variant (10 px tall).
The default font is ~16 px and too bulky; DejaVu12 fits body text
but pushes the 3-column hint strip to within 7 px of the right edge
and fits the idle help awkwardly. DejaVu9 gives us ~17 px of strip
right margin and enough vertical room that we can bump the passkey
to size 4 for cross-room readability.

### State-specific layouts

  Idle (advertising / disconnected) — DejaVu9, all size 1:
    y=0..20    header "Claude Buddy" + status badge
    y=28       "Waiting to pair..."
    y=48       "Open Claude, go to"
    y=66       "Settings > Buddy"
    y=84       "and pick this one"
    y=112..134 hint strip "Q = Exit" (centered)

  Connected with heartbeat:
    y=0..20    header
    y=26       identity band (name + owner)
    y=42       queue line  ("Q: Nrun Nwait Ntot")
    y=58       tokens line ("Today: N,NNN tok")
    y=74       status msg (if hb["msg"] is set)
    y=90..108  prompt box (when a permission is pending)
    y=112..134 hint strip (Y once / N deny / Q exit columns)

  Passkey overlay (during BLE pairing, layered over main):
    y=28       "Pairing passkey:"
    y=44..84   6-digit code at setTextSize(4)
    y=96       "type it into Claude"
"""

import M5

# Anthropic palette, inlined — byte-for-byte matches ui_theme.py.
ORANGE = 0xCC785C
CREAM = 0xF0EEE6
DARK = 0x1F1F1F
BLACK = 0x000000
WHITE = 0xFFFFFF
GRAY_DIM = 0x333333
GRAY_MID = 0x777777
GREEN = 0x00FF00
CYAN = 0x00FFFF
YELLOW = 0xFFFF00
RED = 0xFF0000

_LCD = M5.Lcd

_W = 240
_H = 135


def _right(y: int, pad: int, text: str) -> int:
    """Cursor X so `text` ends `pad` px from the right edge."""
    return _W - pad - _LCD.textWidth(text)


def _center(text: str) -> int:
    """Cursor X to horizontally center `text` in the viewport."""
    return (_W - _LCD.textWidth(text)) // 2


class BuddyUI:
    """240x135 view. Mirrors the Basic's BuddyUI API so the protocol
    and app layers don't care which display is underneath."""

    def __init__(self):
        self._last = {}
        self._passkey = None
        # Unpair confirmation overlay: True while the host has asked
        # us to unpair and we're waiting for an on-device Y/N press.
        # See the threat model in buddy_ble.py — the BLE link is
        # unauthenticated, so destructive commands need an in-person
        # confirmation that an in-range BLE attacker can't fake.
        self._unpair_prompt = False
        self._connection_state = "advertising"
        self._prompt = None
        self._identity_name = "Buddy"
        self._identity_owner = ""
        _LCD.fillScreen(BLACK)
        # setFont is sticky across setTextSize calls, so we pick
        # DejaVu9 once at init. Wrapped in try/except so a future
        # UIFlow build that drops the font still loads us (falls back
        # to the default at an uglier size).
        try:
            _LCD.setFont(_LCD.FONTS.DejaVu9)
        except Exception as e:
            print("buddy_ui_cp: setFont fallback:", e)
        # No setRotation — Cardputer-Adv boots in landscape already.
        self._redraw_chrome()

    # ---- public setters (shape matches Basic's BuddyUI)

    def set_connection(self, state: str):
        if state == self._connection_state:
            return
        self._connection_state = state
        self._draw_header()
        if state in ("advertising", "disconnected"):
            self._prompt = None
            self._last = {}
        self._draw_main()
        self.restore_button_hints()

    def show_passkey(self, pk: int):
        self._passkey = pk
        self._draw_passkey_overlay()
        self.restore_button_hints()

    def clear_passkey(self):
        if self._passkey is None:
            return
        self._passkey = None
        self._draw_main()

    def show_unpair_prompt(self):
        """Show the destructive-action confirmation overlay.

        Repaints the main panel and the hint strip so the only useful
        keys are Y (confirm) and N (cancel) — Q stays an exit even
        here, mirroring the passkey overlay's escape hatch.
        """
        self._unpair_prompt = True
        self._draw_unpair_overlay()
        self.restore_button_hints()

    def clear_unpair_prompt(self):
        if not self._unpair_prompt:
            return
        self._unpair_prompt = False
        self._draw_main()
        self.restore_button_hints()

    def update_heartbeat(self, hb: dict):
        prev_pending = bool(self._prompt)
        self._last = hb
        self._prompt = hb.get("prompt")
        self._draw_main()
        # Hint strip content depends on prompt-pending state; repaint
        # when it flips so the Y/N keys surface only when useful.
        if bool(self._prompt) != prev_pending:
            self.restore_button_hints()

    def update_identity(self, name: str, owner: str):
        self._identity_name = name or "Buddy"
        self._identity_owner = owner or ""
        if self._connection_state not in ("advertising", "disconnected"):
            self._draw_identity()

    def update_footer(self, stats: dict, battery: dict):
        # Stats footer only appears during the connected layout.
        if self._connection_state not in ("advertising", "disconnected"):
            self._draw_footer(stats, battery)

    def flash_decision(self, decision: str):
        color = GREEN if decision == "once" else RED
        self.flash_toast(decision.upper() + " sent", color)

    def flash_toast(self, text: str, color: int = CYAN):
        """Overwrite the hint strip with a one-line colored status."""
        _LCD.fillRect(0, 112, _W, _H - 112, color)
        _LCD.setTextColor(WHITE, color)
        _LCD.setTextSize(1)
        # Clip to whatever fits on the strip; in practice callers
        # keep text short.
        t = text
        while _LCD.textWidth(t) > _W - 12 and len(t) > 1:
            t = t[:-1]
        _LCD.drawString(t, 6, 117)

    def restore_button_hints(self):
        """Paint the hint strip. Shows the keyboard-command menu.

        Two modes:
          - Passkey on screen: Q only — Y/N are no-ops during pairing
            and showing them would be misleading.
          - Otherwise: full Y / N / Q menu, regardless of whether a
            prompt is currently pending. The earlier "only show what
            does something right now" version hid Y/N until a prompt
            arrived, which meant the operator couldn't learn the
            bindings just by looking at the device — the whole
            keyboard menu was invisible except during the ~1s windows
            of active prompts. When Y/N are pressed without a prompt,
            the main loop flashes a "no prompt" toast so the user
            still gets feedback; the menu staying visible is what
            makes the toast's meaning obvious.
        """
        # Thin orange hairline above the strip + DARK fill.
        _LCD.fillRect(0, 111, _W, 1, ORANGE)
        _LCD.fillRect(0, 112, _W, _H - 112, DARK)
        _LCD.setTextColor(CREAM, DARK)
        _LCD.setTextSize(1)
        if self._unpair_prompt:
            # Only Y and N during a destructive-action confirmation;
            # showing Q here invites a thumb-fumble exit that leaves
            # the host hanging on a pending ack.
            _LCD.drawString("Y confirm", 8, 117)
            n = "N cancel"
            _LCD.drawString(n, _right(117, 8, n), 117)
            return
        if self._passkey is not None:
            # During pairing only Q makes sense — Y and N don't
            # actually do anything until the encrypted state fires.
            label = "Q = Exit"
            _LCD.drawString(label, _center(label), 117)
            return
        # 3-column layout. Measured widths on DejaVu9: 38/39/34 px.
        # Left-aligned columns at x=8/96/right-aligned-8 give the
        # eye a clear "approve / deny / back" reading order.
        _LCD.drawString("Y once", 8, 117)
        _LCD.drawString("N deny", 96, 117)
        q = "Q exit"
        _LCD.drawString(q, _right(117, 8, q), 117)

    def is_idle(self) -> bool:
        return (
            self._connection_state in ("advertising", "disconnected")
            and self._passkey is None
            and self._prompt is None
            and not self._unpair_prompt
        )

    def tick_idle_burst(self, frame, last_tick):
        # No burst animation on Cardputer-Adv — kept for API shape
        # so buddy_app's main loop can call unconditionally.
        return frame, last_tick

    # ---- drawing primitives

    def _draw_header(self):
        _LCD.fillRect(0, 0, _W, 20, DARK)
        _LCD.fillRect(0, 20, _W, 1, ORANGE)
        _LCD.setTextSize(1)
        _LCD.setTextColor(ORANGE, DARK)
        _LCD.drawString("Claude Buddy", 6, 5)
        icon, color = self._connection_icon()
        _LCD.setTextColor(color, DARK)
        _LCD.drawString(icon, _right(5, 6, icon), 5)

    def _connection_icon(self):
        s = self._connection_state
        if s == "encrypted":
            return ("LINKED", GREEN)
        if s == "connected":
            return ("PAIR..", YELLOW)
        if s == "disconnected":
            return ("OFF", RED)
        return ("ADV", CYAN)

    def _draw_identity(self):
        name = (self._identity_name or "Buddy")[:22]
        owner = self._identity_owner or ""
        _LCD.fillRect(0, 24, _W, 14, BLACK)
        _LCD.setTextSize(1)
        _LCD.setTextColor(ORANGE, BLACK)
        _LCD.drawString(name, 6, 26)
        if owner:
            _LCD.setTextColor(GRAY_MID, BLACK)
            # Place owner text just after name with an 8 px gutter.
            x = 6 + _LCD.textWidth(name) + 8
            suffix = "<- " + owner
            # Clip the owner suffix to whatever fits before the right
            # margin (the status icon is in the header, not here).
            while x + _LCD.textWidth(suffix) > _W - 6 and len(suffix) > 1:
                suffix = suffix[:-1]
            _LCD.drawString(suffix, x, 26)

    def _draw_main(self):
        # Clear from just under the header hairline down to just above
        # the hint strip hairline — leaves those dividers intact.
        _LCD.fillRect(0, 21, _W, 90, BLACK)
        # Overlays take precedence over the layout under them. The
        # unpair prompt outranks the passkey because they should never
        # both be live at once (passkey only fires during a real
        # pairing flow, which doesn't exist on this build), but
        # ordering it first means future builds with both don't
        # accidentally render the wrong thing.
        if self._unpair_prompt:
            self._draw_unpair_overlay()
            return
        if self._passkey is not None:
            self._draw_passkey_overlay()
            return
        if self._connection_state in ("advertising", "disconnected"):
            self._draw_idle_main()
            return
        self._draw_connected_main()

    def _draw_idle_main(self):
        # Four short lines at size 1. y stride is 18 px which leaves
        # ~8 px of whitespace between 10-px-tall glyphs.
        _LCD.setTextSize(1)
        _LCD.setTextColor(CREAM, BLACK)
        _LCD.drawString("Waiting to pair...", 6, 28)
        _LCD.setTextColor(GRAY_MID, BLACK)
        _LCD.drawString("Open Claude, go to", 6, 48)
        _LCD.drawString("Settings > Buddy", 6, 66)
        _LCD.drawString("and pick this one", 6, 84)

    def _draw_connected_main(self):
        self._draw_identity()
        hb = self._last
        _LCD.setTextSize(1)
        running = hb.get("running", 0)
        waiting = hb.get("waiting", 0)
        total = hb.get("total", 0)
        _LCD.setTextColor(WHITE, BLACK)
        queue = "Q: {}run {}wait {}tot".format(running, waiting, total)
        # Clip to screen width so large numbers don't wrap.
        while _LCD.textWidth(queue) > _W - 12 and len(queue) > 1:
            queue = queue[:-1]
        _LCD.drawString(queue, 6, 42)
        tokens_today = hb.get("tokens_today", 0)
        _LCD.setTextColor(CYAN, BLACK)
        tok = "{:,}".format(tokens_today).replace(",", "'")
        tok_line = "Today: " + tok + " tok"
        while _LCD.textWidth(tok_line) > _W - 12 and len(tok_line) > 1:
            tok_line = tok_line[:-1]
        _LCD.drawString(tok_line, 6, 58)
        # When a prompt is active the prompt box takes over the lower
        # third of the panel — skip the generic msg line so they don't
        # overlap. When no prompt is pending, msg fills the same row.
        if self._prompt:
            self._draw_prompt_box(self._prompt)
        else:
            msg = hb.get("msg", "")
            if msg:
                _LCD.setTextColor(GRAY_MID, BLACK)
                while _LCD.textWidth(msg) > _W - 12 and len(msg) > 1:
                    msg = msg[:-1]
                _LCD.drawString(msg, 6, 74)

    def _draw_prompt_box(self, prompt: dict):
        # Orange-bordered box for the pending permission. y=74..109
        # gives us 35 px of height — two 10-px text rows with a 4-px
        # top gap, 2-px inter-row gap, and 2-px bottom gap. That's
        # enough breathing room to render cleanly without touching
        # either the tokens line at y=58..68 or the hint strip
        # hairline at y=111.
        _LCD.drawRect(3, 74, _W - 6, 35, ORANGE)
        _LCD.setTextSize(1)
        _LCD.setTextColor(ORANGE, BLACK)
        tool_line = "PERM: " + prompt.get("tool", "?")
        while _LCD.textWidth(tool_line) > _W - 14 and len(tool_line) > 1:
            tool_line = tool_line[:-1]
        _LCD.drawString(tool_line, 7, 78)
        hint = prompt.get("hint", "")
        _LCD.setTextColor(CREAM, BLACK)
        while _LCD.textWidth(hint) > _W - 14 and len(hint) > 1:
            hint = hint[:-1]
        _LCD.drawString(hint, 7, 94)

    def _draw_unpair_overlay(self):
        if not self._unpair_prompt:
            return
        _LCD.fillRect(0, 21, _W, 90, BLACK)
        _LCD.setTextSize(1)
        _LCD.setTextColor(RED, BLACK)
        # Two-line attention header so the destructive nature is clear
        # at a glance — this is the only path that wipes user state.
        _LCD.drawString("UNPAIR REQUEST", 6, 28)
        _LCD.setTextColor(CREAM, BLACK)
        _LCD.drawString("from connected host.", 6, 46)
        _LCD.drawString("Wipes name, owner, stats", 6, 64)
        _LCD.drawString("and disconnects.", 6, 78)
        _LCD.setTextColor(GRAY_MID, BLACK)
        _LCD.drawString("Y confirm   N cancel", 6, 96)

    def _draw_passkey_overlay(self):
        if self._passkey is None:
            return
        _LCD.fillRect(0, 21, _W, 90, BLACK)
        _LCD.setTextSize(1)
        _LCD.setTextColor(ORANGE, BLACK)
        _LCD.drawString("Pairing passkey:", 6, 28)
        # Size 4 passkey on DejaVu9 = 40 px tall, ~6 digits wide.
        # Centered with textWidth so size-4 doesn't throw off the math.
        pk_str = "{:06d}".format(self._passkey)
        _LCD.setTextColor(CREAM, BLACK)
        _LCD.setTextSize(4)
        pk_w = _LCD.textWidth(pk_str)
        _LCD.drawString(pk_str, (_W - pk_w) // 2, 44)
        _LCD.setTextSize(1)
        _LCD.setTextColor(GRAY_MID, BLACK)
        _LCD.drawString("type it into Claude", 6, 96)

    def _draw_footer(self, stats: dict, battery: dict):
        # Thin stats line between main panel and hint strip, only in
        # the connected layout. y=96..110 (14 tall) holds one 10-px row.
        _LCD.fillRect(0, 96, _W, 15, BLACK)
        _LCD.setTextSize(1)
        _LCD.setTextColor(GRAY_MID, BLACK)
        left = "Lv.{} a:{} d:{}".format(
            stats.get("lvl", 0),
            stats.get("appr", 0),
            stats.get("deny", 0),
        )
        _LCD.drawString(left, 6, 98)
        pct = max(0, min(100, battery.get("pct", 0)))
        label = "{}%".format(pct)
        _LCD.setTextColor(CREAM, BLACK)
        # Right-aligned with 6 px of padding — and critically,
        # computed from textWidth, not a char-count estimate, so
        # proportional-font surprises (e.g. '%' being 8 px wide)
        # don't push the label off-screen and trigger a line wrap.
        _LCD.drawString(label, _right(98, 6, label), 98)

    def _redraw_chrome(self):
        _LCD.fillScreen(BLACK)
        self._draw_header()
        self._draw_main()
        self.restore_button_hints()
