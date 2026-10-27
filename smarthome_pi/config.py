"""GPIO-Pinbelegung und Umgebungskonfiguration (BCM-Nummern).

Anpassen an deine Verdrahtung; siehe README Pin-Mapping.
"""

import os
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class PinConfig:
    """BCM-Pins für Relais (Stellmotoren), Steckdose, Schlafzimmer-Licht, LED-PWM."""

    valve_pins: tuple[int, ...]  # 8 Kreise
    outlet_pin: int
    schlafzimmer_light_relay_pin: int
    led_pwm_pin: int
    led_pwm_frequency_hz: float = 1000.0
    relays_active_low: bool = True


# Ventile: wie früher Relaistest (BOARD 7,13,15,31,33,35,37) → BCM 4,27,22,6,13,19,26;
# Kreis 1 = BCM 23 (Pin 16), damit BCM 18 frei für LED-Hardware-PWM bleibt.
DEFAULT_PINS = PinConfig(
    valve_pins=(4, 23, 27, 22, 6, 13, 19, 26),
    outlet_pin=5,
    schlafzimmer_light_relay_pin=16,
    led_pwm_pin=18,
    led_pwm_frequency_hz=1000.0,
    relays_active_low=True,
)


def load_pin_config() -> PinConfig:
    """Optional per ENV:
    SMH_VALVE_PINS=4,23,27,22,6,13,19,26
    SMH_OUTLET=5
    SMH_SZ_LIGHT_RELAY=16
    SMH_LED_PWM=18
    SMH_LED_PWM_HZ=1000
    SMH_PIGPIO_HOST   # optional, leer = localhost (pigpiod)
    SMH_LED_FALLBACK_RPI_GPIO   # true = RPi.GPIO-PWM wenn pigpiod fehlt
    SMH_RELAYS_ACTIVE_LOW=true|false

    SMH_LED_PWM_HZ / SMH_LED_PWM gelten auch ohne SMH_VALVE_PINS (Feintuning LED).
    SMH_LED_PWM_INVERT=true|false  # aktiv-low-Treiber (PWM umkehren)
    """
    if os.environ.get("SMH_VALVE_PINS"):
        parts = [int(x.strip()) for x in os.environ["SMH_VALVE_PINS"].split(",")]
        if len(parts) != 8:
            raise ValueError("SMH_VALVE_PINS muss 8 BCM-Pins enthalten")
        pins = PinConfig(
            valve_pins=tuple(parts),
            outlet_pin=int(os.environ.get("SMH_OUTLET", "5")),
            schlafzimmer_light_relay_pin=int(
                os.environ.get("SMH_SZ_LIGHT_RELAY", "16")
            ),
            led_pwm_pin=int(os.environ.get("SMH_LED_PWM", "18")),
            led_pwm_frequency_hz=float(
                os.environ.get("SMH_LED_PWM_HZ", "1000")
            ),
            relays_active_low=os.environ.get(
                "SMH_RELAYS_ACTIVE_LOW", "true"
            ).lower()
            in ("1", "true", "yes"),
        )
    else:
        pins = DEFAULT_PINS

    if os.environ.get("SMH_LED_PWM"):
        pins = replace(pins, led_pwm_pin=int(os.environ["SMH_LED_PWM"]))
    if os.environ.get("SMH_LED_PWM_HZ"):
        pins = replace(
            pins, led_pwm_frequency_hz=float(os.environ["SMH_LED_PWM_HZ"])
        )
    if os.environ.get("SMH_OUTLET"):
        pins = replace(pins, outlet_pin=int(os.environ["SMH_OUTLET"]))
    if os.environ.get("SMH_SZ_LIGHT_RELAY"):
        pins = replace(
            pins,
            schlafzimmer_light_relay_pin=int(
                os.environ["SMH_SZ_LIGHT_RELAY"]
            ),
        )
    if os.environ.get("SMH_RELAYS_ACTIVE_LOW"):
        pins = replace(
            pins,
            relays_active_low=os.environ["SMH_RELAYS_ACTIVE_LOW"].lower()
            in ("1", "true", "yes"),
        )
    return pins


def credentials_path() -> str | None:
    return os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")


def mock_gpio() -> bool:
    return os.environ.get("SMH_MOCK_GPIO", "").lower() in ("1", "true", "yes")


def led_pwm_invert() -> bool:
    """PWM logisch invertieren (aktiv-low / P-Kanal: hell = weniger High-Zeit)."""
    return os.environ.get("SMH_LED_PWM_INVERT", "").lower() in (
        "1",
        "true",
        "yes",
    )


def handmode_flag_path() -> str:
    """Pfad zur Flag-Datei für Wartungs-Handmodus (alle Ventile AN)."""
    return os.environ.get("SMH_HANDMODE_FLAG", "/tmp/smarthome_handmode.flag")
