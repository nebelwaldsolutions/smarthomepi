"""Konsole: die beiden Schlafzimmer-Relais (Steckdose, Raumlicht) direkt testen.

Nutzt dieselbe Pin- und active-low-Logik wie der Dienst (``load_pin_config``).

**Wichtig:** ``smarthome-pi`` stoppen, sonst kämpfen zwei Prozesse um dieselben GPIOs::

    sudo systemctl stop smarthome-pi

Danach z. B.::

    python -m smarthome_pi.outlet_test steckdose on
    python -m smarthome_pi.outlet_test licht off
    python -m smarthome_pi.outlet_test all-off
    python -m smarthome_pi.outlet_test status

Nur auf dem Raspberry Pi (Linux), nicht unter Windows.
"""

from __future__ import annotations

import argparse
import sys

from smarthome_pi.config import load_pin_config, mock_gpio


def _relay_levels(active_low: bool) -> tuple[int, int]:
    on = 0 if active_low else 1
    off = 1 if active_low else 0
    return on, off


def _setup_gpio():
    if sys.platform != "linux" or mock_gpio():
        print("Nur auf dem Pi ohne SMH_MOCK_GPIO=1.")
        sys.exit(1)
    import RPi.GPIO as GPIO  # type: ignore

    pins = load_pin_config()
    GPIO.setmode(GPIO.BCM)
    on, off = _relay_levels(pins.relays_active_low)
    GPIO.setup(pins.outlet_pin, GPIO.OUT, initial=off)
    GPIO.setup(pins.schlafzimmer_light_relay_pin, GPIO.OUT, initial=off)
    return GPIO, pins, on, off


def _cmd_steckdose(on: bool) -> None:
    GPIO, pins, rel_on, rel_off = _setup_gpio()
    try:
        GPIO.output(pins.outlet_pin, rel_on if on else rel_off)
        print(
            f"Steckdose (BCM {pins.outlet_pin}): "
            f"{'AN' if on else 'AUS'}"
        )
    finally:
        GPIO.cleanup()


def _cmd_licht(on: bool) -> None:
    GPIO, pins, rel_on, rel_off = _setup_gpio()
    try:
        GPIO.output(
            pins.schlafzimmer_light_relay_pin,
            rel_on if on else rel_off,
        )
        print(
            f"Raumlicht (BCM {pins.schlafzimmer_light_relay_pin}): "
            f"{'AN' if on else 'AUS'}"
        )
    finally:
        GPIO.cleanup()


def _cmd_all_off() -> None:
    GPIO, pins, rel_on, rel_off = _setup_gpio()
    try:
        GPIO.output(pins.outlet_pin, rel_off)
        GPIO.output(pins.schlafzimmer_light_relay_pin, rel_off)
        print("Beide Kanäle AUS.")
    finally:
        GPIO.cleanup()


def _cmd_status() -> None:
    GPIO, pins, rel_on, rel_off = _setup_gpio()
    try:
        o = GPIO.input(pins.outlet_pin)
        l = GPIO.input(pins.schlafzimmer_light_relay_pin)
        print(
            f"BCM {pins.outlet_pin} Steckdose: GPIO={o} "
            f"({'Relais EIN' if o == rel_on else 'Relais AUS'})"
        )
        print(
            f"BCM {pins.schlafzimmer_light_relay_pin} Licht: GPIO={l} "
            f"({'Relais EIN' if l == rel_on else 'Relais AUS'})"
        )
        print(f"(active_low={load_pin_config().relays_active_low})")
    finally:
        GPIO.cleanup()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        description="Schlafzimmer-Relais: Steckdose & Raumlicht testen."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("steckdose", help="Steckdose-Kanal")
    sp.add_argument("state", choices=["on", "off"])

    lp = sub.add_parser("licht", help="Raumlicht-Kanal")
    lp.add_argument("state", choices=["on", "off"])

    sub.add_parser("all-off", help="Beide AUS")
    sub.add_parser("status", help="GPIO-Pegel anzeigen")

    args = p.parse_args(argv)
    if args.cmd == "steckdose":
        _cmd_steckdose(args.state == "on")
    elif args.cmd == "licht":
        _cmd_licht(args.state == "on")
    elif args.cmd == "all-off":
        _cmd_all_off()
    elif args.cmd == "status":
        _cmd_status()


if __name__ == "__main__":
    main()
