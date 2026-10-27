"""Konsole: LED-PWM direkt testen (Standard: nur pigpio, kein RPi.GPIO).

**Standard** spricht nur den LED-BCM-Pin über **pigpiod** an — keine Relais-Initialisierung,
daher keine „channel already in use“-Kollision mit laufendem ``smarthome-pi`` (Relais-Pins).

Zum vollen Stack (wie der Dienst) optional ``--with-relays`` — dann **unbedingt**::

    sudo systemctl stop smarthome-pi

Beispiele::

    python3 -m smarthome_pi.led_test 80
    SMH_LED_PWM_INVERT=1 python3 -m smarthome_pi.led_test 80
    python3 -m smarthome_pi.led_test 80 --stay
    python3 -m smarthome_pi.led_test 0

Nur auf dem Raspberry Pi (Linux), nicht unter Windows.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from smarthome_pi.config import led_pwm_invert, load_pin_config, mock_gpio
from smarthome_pi.gpio_controller import GpioController

_LED_PWM_RANGE = 10_000


def _connect_pigpio() -> object:
    import pigpio  # type: ignore

    host = os.environ.get("SMH_PIGPIO_HOST", "").strip()
    max_wait = float(os.environ.get("SMH_PIGPIO_CONNECT_SEC", "45"))
    step = float(os.environ.get("SMH_PIGPIO_CONNECT_STEP_SEC", "0.5"))
    max_wait = max(1.0, min(120.0, max_wait))
    step = max(0.1, min(2.0, step))
    deadline = time.monotonic() + max_wait
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            pi_try = pigpio.pi(host) if host else pigpio.pi()
            if pi_try.connected:
                return pi_try
            pi_try.stop()
        except Exception as e:
            last_err = e
        time.sleep(step)
    raise RuntimeError(
        f"pigpiod nach {max_wait:.0f}s nicht erreichbar"
        + (f": {last_err}" if last_err else "")
    )


def _duty_for_percent(percent: float) -> int:
    pct = max(0.0, min(100.0, percent))
    dc = int(round(pct / 100.0 * float(_LED_PWM_RANGE)))
    dc = max(0, min(_LED_PWM_RANGE, dc))
    if led_pwm_invert():
        dc = _LED_PWM_RANGE - dc
    return dc


def _run_pigpio_only(
    percent: float,
    hold_sec: float,
    stay: bool,
) -> None:
    import pigpio  # type: ignore

    pins = load_pin_config()
    g = pins.led_pwm_pin
    freq = int(max(1, round(pins.led_pwm_frequency_hz)))
    pi = _connect_pigpio()
    try:
        pi.set_mode(g, pigpio.OUTPUT)
        actual = pi.set_PWM_frequency(g, freq)
        pi.set_PWM_range(g, _LED_PWM_RANGE)
        dc = _duty_for_percent(percent)
        pi.set_PWM_dutycycle(g, dc)
        inv = " (SMH_LED_PWM_INVERT=1)" if led_pwm_invert() else ""
        print(
            f"LED pigpio-only: {percent:.0f} % → dutycycle {dc}/{_LED_PWM_RANGE}, "
            f"BCM {g}, {freq} Hz → {actual} Hz{inv}. Kein RPi.GPIO."
        )
        if stay:
            print(
                "Beendet ohne PWM-Aus — Zustand bleibt. "
                "Aus: python3 -m smarthome_pi.led_test 0"
            )
            return
        h = max(0.0, hold_sec)
        if percent <= 0.0:
            h = 0.0
        if h > 0:
            time.sleep(h)
        off_dc = _duty_for_percent(0.0)
        pi.set_PWM_dutycycle(g, off_dc)
        print(f"LED aus (dutycycle {off_dc}) — pigpio-Verbindung zu Ende.")
    finally:
        if not stay:
            try:
                pi.stop()
            except Exception:
                pass


def _run_full_controller(
    percent: float,
    hold_sec: float,
    stay: bool,
) -> None:
    pins = load_pin_config()
    gpio = GpioController(pins)
    try:
        gpio.set_led_brightness(percent)
        print(
            f"LED {percent:.0f} % via GpioController (BCM {pins.led_pwm_pin}). "
            "Stoppe smarthome-pi, sonst Relais-Pin-Warnungen."
        )
        if stay:
            print(
                "Beendet ohne cleanup — Zum Ausschalten: "
                "python3 -m smarthome_pi.led_test 0 --with-relays"
            )
            return
        hold = max(0.0, hold_sec)
        if percent <= 0.0:
            hold = 0.0
        if hold > 0:
            time.sleep(hold)
        gpio.set_led_brightness(0.0)
        print("LED 0 % — cleanup.")
    finally:
        if not stay:
            gpio.cleanup()


def main(argv: list[str] | None = None) -> None:
    if sys.platform != "linux" or mock_gpio():
        print("Nur auf dem Pi ohne SMH_MOCK_GPIO=1.")
        sys.exit(1)
    p = argparse.ArgumentParser(
        description="LED testen: Standard nur pigpio; optional kompletter GPIO-Controller."
    )
    p.add_argument("percent", type=float, help="Helligkeit 0–100")
    p.add_argument(
        "-s",
        "--seconds",
        type=float,
        default=10.0,
        metavar="SEC",
        help="Nach SEC Sekunden aus dimmen (Standard: 10; bei 0 %% sofort aus)",
    )
    p.add_argument(
        "--stay",
        action="store_true",
        help="PWM-Zustand nach Setzen beibehalten (kein Abschalten / kein stop)",
    )
    p.add_argument(
        "--with-relays",
        action="store_true",
        help="GpioController wie smarthome-pi (RPi.GPIO Relais) — Dienst vorher stoppen",
    )
    args = p.parse_args(argv)
    if args.with_relays:
        _run_full_controller(args.percent, args.seconds, args.stay)
    else:
        _run_pigpio_only(args.percent, args.seconds, args.stay)


if __name__ == "__main__":
    main()
