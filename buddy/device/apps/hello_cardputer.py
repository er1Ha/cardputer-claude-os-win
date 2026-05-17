"""Hello, Cardputer-Adv — the smoke-test-that-became-an-app.

This is the first thing we run on a freshly-provisioned Cardputer-Adv.
It draws a welcome banner, echoes each keypress so the user can
confirm the QWERTY matrix is wired up, and exits on Q / ESC back to
the UIFlow App List. No BLE, no I2C, no network — if the LCD, the
keyboard, and MicroPython's event loop are working, this app runs.
That's useful right after provisioning to reassure the user that the
board came up cleanly before they try anything more involved.

### Port notes

- **Screen.** 240x135 landscape. We use the same three-zone chrome as
  claude_buddy / snake: a 20 px DARK header with an ORANGE hairline
  at y=20, a play / content area, and a hint strip at the bottom.
  Consistency across the app suite matters more here than visual
  ambition — the apps feel like parts of the same device when they
  share a visual vocabulary.

- **Keyboard.** MatrixKeyboard polled via `kb.tick()` + `kb.get_key()`
  at ~40 ms. This is the same loop shape the other apps use, so a
  user who learns one app's rhythm knows all three. Keys echo into a
  scrolling line underneath the banner so the user can see each
  press land.

- **Exit.** Q or ESC triggers a `machine.reset()` in the `finally`
  block. UIFlow 2.0 has no return-to-launcher API; a soft reboot
  drops the user back at App List, which is the whole reason the
  other two apps end the same way. Clearing the screen to black
  first avoids the "last frame briefly flashes behind the launcher"
  visual glitch.

- **Font.** DejaVu9, size 1 for body / hints, size 2 for the banner.
  Sizes are measured with `_LCD.textWidth(...)` for centering because
  proportional-width glyphs don't honor the naive `CHAR_W * len(text)`
  estimate on this build (see the extensive comment in buddy_ui_cp
  for the exhaustive measurement table).
"""

import time

import M5
import machine
from hardware import MatrixKeyboard


# Palette inlined from ui_theme — same five colors the rest of the
# bundle uses, so the apps feel visually coherent.
_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777

_LCD = M5.Lcd

_W = 240
_H = 135

# How many characters of key-echo history we keep on screen. The
# echo row is 220 px wide (240 minus 10 px padding each side) and
# DejaVu9 averages ~6 px/char, so 32 fits comfortably with margin
# for the occasional wide glyph.
_ECHO_MAX = 32


def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        # Fall back silently — on a build without FONTS we still
        # render in whatever the default is rather than crashing.
        print("hello: setFont fallback:", e)


def _draw_chrome():
    """Header, hairline, hint strip. Called once at startup."""
    _LCD.fillScreen(_BLACK)

    # Header band.
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Hello, Cardputer", 6, 5)

    # Banner — size 2 for visual weight. Centered because we have
    # the horizontal room and the greeting is the whole point of
    # the app. y=46 puts it roughly a third of the way down the
    # content area, which feels balanced with the echo row below.
    _LCD.setTextSize(2)
    _LCD.setTextColor(_CREAM, _BLACK)
    greet = "Hi there."
    _LCD.drawString(greet, (_W - _LCD.textWidth(greet)) // 2, 46)

    # Sub-greeting.
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    sub = "type anything -- i'm listening"
    _LCD.drawString(sub, (_W - _LCD.textWidth(sub)) // 2, 74)

    # Hint strip along the bottom — matches the visual rhythm of the
    # other apps, though we only have one intent here.
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    hint = "Q/ESC  back to menu"
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _draw_echo(echo):
    """Repaint the echo row with the current keystroke history.

    We blank the row each redraw rather than trying to diff — the
    row is small enough that a full repaint at 40 ms cadence is
    imperceptible, and a diff-based approach adds failure modes
    (stale trailing glyphs, cursor-position drift) with no visible
    upside at this scale.
    """
    row_y = 96
    row_h = 14
    _LCD.fillRect(0, row_y, _W, row_h, _BLACK)
    if not echo:
        return
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _BLACK)
    # Right-align so the *latest* key is always at a stable position
    # against the right margin — the eye tracks there naturally.
    text = "".join(echo)
    x = _W - 10 - _LCD.textWidth(text)
    if x < 10:
        x = 10
    _LCD.drawString(text, x, row_y + 2)


def _keychar(k):
    """Normalize a MatrixKeyboard return value to a single display char.

    MatrixKeyboard.get_key() on this UIFlow 2.0 build returns ints
    (ASCII codes) for printable keys and special codes for Enter /
    Escape. We surface printables as themselves, Enter as the literal
    '\\n' glyph (rendered as a small placeholder), and drop everything
    else so the echo row doesn't fill with noise. Exit keys are
    handled by the caller before we get here so we don't need to
    surface them.
    """
    if k is None:
        return None
    if isinstance(k, int):
        if k == 0x0D:
            return "\\n"
        if 0x20 <= k <= 0x7E:
            return chr(k)
        return None
    if isinstance(k, str) and k and 0x20 <= ord(k[0]) <= 0x7E:
        return k[0]
    return None


def _is_exit(k):
    if k is None:
        return False
    if isinstance(k, int):
        if k == 0x1B:
            return True
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return False
    if isinstance(k, str) and k:
        return k.lower() == "q"
    return False


def run():
    _set_font()
    _draw_chrome()

    kb = MatrixKeyboard()
    # Debounce the launch keypress — same 400 ms window the other
    # apps use — so selecting "hello_cardputer" from App List doesn't
    # immediately land as the first echoed character.
    time.sleep_ms(400)

    echo = []
    try:
        while True:
            kb.tick()
            k = kb.get_key()

            if _is_exit(k):
                return

            ch = _keychar(k)
            if ch is not None:
                echo.append(ch)
                # Cap the history so long typing sessions don't
                # eat the horizontal space — `_ECHO_MAX` tuned to
                # the row width above.
                if len(echo) > _ECHO_MAX:
                    echo = echo[-_ECHO_MAX:]
                _draw_echo(echo)

            time.sleep_ms(40)
    finally:
        # Mirror the other apps' exit protocol so the three feel
        # like one suite: clear, brief pause, soft-reboot back to
        # the launcher.
        try:
            _LCD.fillScreen(_BLACK)
        except Exception as e:
            print("hello: clear warning:", e)
        time.sleep_ms(200)
        machine.reset()


# UIFlow's App List has been observed to invoke apps both as
# __main__ and via import. The previous if/else with both arms
# calling run() was self-cancelling; the empirical behavior is
# always-run, so just call run() bare.
run()
