"""Exercise the on-board hardware on a freshly-flashed M5Stack.

Useful both as a functional test and as model identification: the
I2C bus scan is the cleanest way to distinguish a Basic (only 0x75
IP5306) from a Gray (adds 0x68/0x69 IMU) from a FIRE (also 0x34 AXP).
Core2 and CoreS3 have different chipsets still — see
references/hardware_signatures.md.
"""

from __future__ import annotations

import argparse
import time

import mpy_repl


SMOKE_SCRIPT = """
import M5
import time
from machine import I2C, Pin

M5.begin()

# Internal I2C bus — SDA=21, SCL=22 on classic Core.
try:
    i2c = I2C(0, sda=Pin(21), scl=Pin(22), freq=100000)
    devs = i2c.scan()
    print('I2C', [hex(d) for d in devs])
except Exception as e:
    print('I2C-FAIL', e)

# LCD test — fill 3 colors briefly.
try:
    M5.Lcd.fillScreen(0xF800)  # red
    time.sleep_ms(250)
    M5.Lcd.fillScreen(0x07E0)  # green
    time.sleep_ms(250)
    M5.Lcd.fillScreen(0x001F)  # blue
    time.sleep_ms(250)
    M5.Lcd.fillScreen(0x0000)
    M5.Lcd.setCursor(10, 10)
    M5.Lcd.print('M5 SMOKE TEST OK')
    print('LCD-OK')
except Exception as e:
    print('LCD-FAIL', e)

# Speaker beep.
try:
    M5.Speaker.tone(1000, 150)
    print('SPK-OK')
except Exception as e:
    print('SPK-FAIL', e)

# Buttons — just read state, don't wait.
try:
    a = M5.BtnA.isPressed()
    b = M5.BtnB.isPressed()
    c = M5.BtnC.isPressed() if hasattr(M5, 'BtnC') else None
    print('BTN', a, b, c)
except Exception as e:
    print('BTN-FAIL', e)

print('SMOKE-DONE')
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke-test an M5Stack.")
    ap.add_argument("--port", required=True)
    args = ap.parse_args()

    s = mpy_repl.open_port(args.port)
    try:
        mpy_repl.interrupt_to_repl(s)
        out = mpy_repl.exec_and_capture(s, SMOKE_SCRIPT, settle=3.0)
        out = mpy_repl.collect_until(s, out, "SMOKE-DONE", timeout=15.0)
    finally:
        s.close()
    print(mpy_repl.strip_paste_echo(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
