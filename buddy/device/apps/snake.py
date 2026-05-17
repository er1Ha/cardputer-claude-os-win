"""Snake, adapted for the Cardputer-Adv.

### Port notes vs. the M5Stack Basic original (`snake_app.py`)

- Screen is 240x135 (was 320x240). Rework the grid and drop the
  pulsing starburst on game over — the animation lived in the big
  shared `ui_theme` / `burst_frames` modules and we want this app
  to be a single self-contained file so it can be installed with
  one upload and doesn't drag the buddy stack's peer modules onto
  a device that might only want the game.
- Controls use the Cardputer's QWERTY keyboard instead of BtnA/B/C.
  Two overlapping schemes, whichever is more comfortable:
    WASD        — W up, S down, A left, D right (gamer default)
    Arrow cluster — ; up, . down, , left, / right
  The arrow keys on Cardputer-Adv are unshifted printable ASCII
  (not special key codes) — the legend painted on them (arrows)
  is just physical labeling; the MatrixKeyboard driver reports
  the underlying glyph. So we bind both the letters *and* the
  glyphs to the same gameplay intent.
  The Basic's "B flips vertical" single-axis mapping was clever for
  three physical buttons but unnecessary here — with two 4-way
  schemes we do classic 4-directional Snake.
- `Q` exits back to UIFlow's App List. UIFlow 2.0 has no
  return-to-launcher API (same constraint claude_buddy hits), so
  exit is a soft `machine.reset()` inside a `finally` block.
- Per-poll input model mirrors claude_buddy's loop: `kb.tick()` on
  every 40 ms pass, read via `kb.get_key()`. MatrixKeyboard's tick
  debounces internally; the classic direction-latching trick
  (`pending_dir` applied at the next move tick) still works to
  prevent an A-then-D double-press from queuing a 180.

### Layout math

    Header:        y=0..19    (20 px, DARK bg, ORANGE hairline at y=20)
    Play area:    y=22..131   (110 px tall, 11 rows × 10 px)
                  x=0..239    (240 wide, 24 cols × 10 px)
    Bottom edge:  y=132..134  (3 px padding, black)

11 rows × 24 cols = 264 cells, comfortably playable.
"""

import random
import time

import M5
import machine
from hardware import MatrixKeyboard


# ---- palette (inlined from ui_theme; matches claude_buddy's cut)
_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_RED = 0xFF0000

_LCD = M5.Lcd

_W = 240
_H = 135

_CELL = 10
_GRID_W = 24        # 240 / 10
_GRID_H = 11        # 110 / 10
_PLAY_X = 0
_PLAY_Y = 22        # just below the header hairline at y=20

_SNAKE = _ORANGE
_FOOD = _CREAM

_LEFT = (-1, 0)
_RIGHT = (1, 0)
_UP = (0, -1)
_DOWN = (0, 1)

# Poll every 40 ms; the snake advances every _MOVE_TICKS polls.
# 4 * 40 = 160 ms per step is the classic comfortable pace.
_MOVE_TICKS = 4


def _set_font():
    """Match claude_buddy's font choice so chrome looks consistent
    across the app suite. DejaVu9 is 10 px tall, so it fits the
    20 px header with room for top/bottom padding."""
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("snake: setFont fallback:", e)


def _draw_cell(cx, cy, color):
    _LCD.fillRect(_PLAY_X + cx * _CELL, _PLAY_Y + cy * _CELL, _CELL, _CELL, color)


def _draw_chrome(score):
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString("Snake", 6, 5)
    _update_score(score)


def _update_score(score):
    # Only repaint the right portion of the header so we don't flash
    # the title text on every score bump.
    _LCD.fillRect(100, 0, _W - 100, 20, _DARK)
    _LCD.setTextColor(_CREAM, _DARK)
    text = "score: {}".format(score)
    x = _W - 6 - _LCD.textWidth(text)
    _LCD.drawString(text, x, 5)


def _intent(k):
    """Collapse raw keys into gameplay intents.

    Mirrors claude_buddy's approach: MatrixKeyboard hands back int
    ASCII codes (0x57='W' etc.), so we convert printables and then
    do one string-match path. Enter as synonym for restart on the
    game-over screen; Escape as synonym for exit everywhere.
    """
    if k is None:
        return None
    if isinstance(k, int):
        if k == 0x1B:
            return "exit"
        # Enter reports as 0x0A on this firmware build (per
        # main.py's launcher); accept 0x0D too in case a future
        # firmware flips it back to CR.
        if k in (0x0A, 0x0D):
            return "restart"
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return None
    if not isinstance(k, str) or not k:
        return None
    ch = k.lower()
    # WASD and the arrow cluster (`;` `.` `,` `/`) both map to the
    # same four gameplay intents. On Cardputer-Adv the arrow-labeled
    # keys report their unshifted glyph, so we just match the glyph
    # here — no special-key decoding needed.
    if ch == "w" or ch == ";":
        return "up"
    if ch == "s" or ch == ".":
        return "down"
    if ch == "a" or ch == ",":
        return "left"
    if ch == "d" or ch == "/":
        return "right"
    if ch == "q":
        return "exit"
    if ch == "r":
        return "restart"
    return None


def _random_food(snake):
    occupied = set(snake)
    # 264-cell grid means even a long snake only spins a few times.
    while True:
        cell = (random.randint(0, _GRID_W - 1), random.randint(0, _GRID_H - 1))
        if cell not in occupied:
            return cell


def _game_over(kb, score):
    """Blocking end-of-round screen. Returns 'restart' or 'exit'."""
    _LCD.fillRect(0, 21, _W, _H - 21, _BLACK)
    _LCD.setTextSize(1)
    # Three lines of centered text: big red "Game over", the score,
    # and a help line. All size 1 since size 2+ on DejaVu9 looks
    # chunky and the 135-px panel doesn't have room to really show
    # off size-3 anyway.
    _LCD.setTextColor(_RED, _BLACK)
    t = "Game over"
    _LCD.drawString(t, (_W - _LCD.textWidth(t)) // 2, 36)
    _LCD.setTextColor(_CREAM, _BLACK)
    s = "score: {}".format(score)
    _LCD.drawString(s, (_W - _LCD.textWidth(s)) // 2, 60)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    h = "R again   Q exit"
    _LCD.drawString(h, (_W - _LCD.textWidth(h)) // 2, 90)

    while True:
        kb.tick()
        i = _intent(kb.get_key())
        if i == "restart":
            return "restart"
        if i == "exit":
            return "exit"
        time.sleep_ms(40)


def _play_round(kb):
    # Start centered, facing right, with a 3-segment body.
    head = (_GRID_W // 2, _GRID_H // 2)
    snake = [head, (head[0] - 1, head[1]), (head[0] - 2, head[1])]
    direction = _RIGHT
    pending_dir = direction
    score = 0

    _draw_chrome(score)
    for cell in snake:
        _draw_cell(cell[0], cell[1], _SNAKE)
    food = _random_food(snake)
    _draw_cell(food[0], food[1], _FOOD)

    tick = 0
    while True:
        kb.tick()
        i = _intent(kb.get_key())
        # One input wins per step — we latch into pending_dir and
        # apply at the next move tick so a rapid key sequence can't
        # queue a 180 through an intermediate direction.
        if i == "up" and direction != _DOWN:
            pending_dir = _UP
        elif i == "down" and direction != _UP:
            pending_dir = _DOWN
        elif i == "left" and direction != _RIGHT:
            pending_dir = _LEFT
        elif i == "right" and direction != _LEFT:
            pending_dir = _RIGHT
        elif i == "exit":
            # Second tuple element signals "don't go to game-over";
            # caller short-circuits to the finally-reset path.
            return score, True

        tick += 1
        if tick < _MOVE_TICKS:
            time.sleep_ms(40)
            continue
        tick = 0

        direction = pending_dir
        new_head = (snake[0][0] + direction[0], snake[0][1] + direction[1])

        # Wall collision.
        if (new_head[0] < 0 or new_head[0] >= _GRID_W
                or new_head[1] < 0 or new_head[1] >= _GRID_H):
            return score, False

        # Self-collision: tail cell is about to vacate, so exempt
        # from the occupied set unless we're eating this tick.
        ate = new_head == food
        body = snake if ate else snake[:-1]
        if new_head in body:
            return score, False

        snake.insert(0, new_head)
        _draw_cell(new_head[0], new_head[1], _SNAKE)

        if ate:
            score += 1
            _update_score(score)
            food = _random_food(snake)
            _draw_cell(food[0], food[1], _FOOD)
        else:
            tail = snake.pop()
            _draw_cell(tail[0], tail[1], _BLACK)

        time.sleep_ms(40)


def run():
    _set_font()
    kb = MatrixKeyboard()
    # Debounce the keypress that launched us from App List so it
    # doesn't register as an instant-direction-change.
    time.sleep_ms(400)
    try:
        while True:
            score, early_exit = _play_round(kb)
            if early_exit:
                return
            if _game_over(kb, score) == "exit":
                return
    finally:
        # Mirror claude_buddy's exit protocol: clear the screen
        # before the soft reset so the launcher doesn't briefly
        # flash the last frame of the prior app.
        try:
            _LCD.fillScreen(_BLACK)
        except Exception as e:
            print("snake: clear warning:", e)
        time.sleep_ms(200)
        machine.reset()


# UIFlow's App List has been observed to invoke apps both as
# __main__ and via import. The previous if/else with both arms
# calling run() was self-cancelling; the empirical behavior is
# always-run, so just call run() bare.
run()
