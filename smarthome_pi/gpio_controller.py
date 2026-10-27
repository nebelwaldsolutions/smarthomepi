"""GPIO: 8 Heiz-Relais (zeitproportional), 2× Schlafzimmer-Relais, LED per PWM.

LED bevorzugt **pigpio** (Hardware-PWM auf BCM 18). Ohne laufenden **pigpiod** optional
**RPi.GPIO Software-PWM** auf demselben Pin (Standard an, siehe ``SMH_LED_FALLBACK_RPI_GPIO``).

Relais: RPi.GPIO — LED-Pin wird nur von pigpio ODER von RPi.GPIO-PWM genutzt, nie doppelt.

pigpiod: ``sudo apt install pigpio`` (sofern in den Repos) oder siehe README (Build aus Quellen).
"""

from __future__ import annotations

import logging
import os
import sys
import time

from smarthome_pi.config import PinConfig, load_pin_config, led_pwm_invert, mock_gpio

_LOG = logging.getLogger(__name__)


def _led_fallback_rpi_gpio() -> bool:
    """Wenn pigpiod fehlt: RPi.GPIO-PWM auf dem LED-Pin (kann flimmern)."""
    return os.environ.get("SMH_LED_FALLBACK_RPI_GPIO", "true").lower() in (
        "1",
        "true",
        "yes",
    )


class GpioController:
    def __init__(self, pins: PinConfig | None = None) -> None:
        self._pins = pins or load_pin_config()
        self._use_real = sys.platform == "linux" and not mock_gpio()
        self._GPIO = None
        self._pi = None  # pigpio.pi() für LED
        self._pwm = None  # RPi.GPIO.PWM Fallback
        self._led_pwm_range = 10_000
        self._led_duty = 0.0
        self._outlet_on = False
        self._sz_light_on = False
        self._valve_on = [False] * 8
        self._relay_on_level = (
            0 if self._pins.relays_active_low else 1
        )
        self._relay_off_level = (
            1 if self._pins.relays_active_low else 0
        )

        if not self._use_real:
            _LOG.info("GPIO Mock: keine echte Hardware")
            return

        try:
            import RPi.GPIO as GPIO  # type: ignore

            self._GPIO = GPIO
            GPIO.setmode(GPIO.BCM)
            for p in self._pins.valve_pins:
                GPIO.setup(p, GPIO.OUT, initial=self._relay_off_level)
            GPIO.setup(
                self._pins.outlet_pin,
                GPIO.OUT,
                initial=self._relay_off_level,
            )
            GPIO.setup(
                self._pins.schlafzimmer_light_relay_pin,
                GPIO.OUT,
                initial=self._relay_off_level,
            )
        except Exception as e:
            _LOG.warning("RPi.GPIO Relais nicht verfügbar (%s), Mock-Modus", e)
            self._use_real = False
            self._GPIO = None
            return

        self._init_led_output()

    def _init_led_output(self) -> None:
        """pigpio (HW-PWM) oder Fallback RPi.GPIO Software-PWM."""
        assert self._GPIO is not None
        g = self._pins.led_pwm_pin
        try:
            import pigpio  # type: ignore

            host = os.environ.get("SMH_PIGPIO_HOST", "").strip()
            max_wait = float(os.environ.get("SMH_PIGPIO_CONNECT_SEC", "45"))
            step = float(os.environ.get("SMH_PIGPIO_CONNECT_STEP_SEC", "0.5"))
            max_wait = max(1.0, min(120.0, max_wait))
            step = max(0.1, min(2.0, step))

            self._pi = None
            deadline = time.monotonic() + max_wait
            last_err: Exception | None = None
            while time.monotonic() < deadline:
                try:
                    pi_try = pigpio.pi(host) if host else pigpio.pi()
                    if pi_try.connected:
                        self._pi = pi_try
                        break
                    pi_try.stop()
                except Exception as e:
                    last_err = e
                time.sleep(step)

            if self._pi is None:
                raise RuntimeError(
                    f"pigpiod nach {max_wait:.0f}s nicht erreichbar"
                    + (f": {last_err}" if last_err else "")
                )

            self._pi.set_mode(g, pigpio.OUTPUT)
            freq = int(max(1, round(self._pins.led_pwm_frequency_hz)))
            actual = self._pi.set_PWM_frequency(g, freq)
            self._pi.set_PWM_range(g, self._led_pwm_range)
            off_dc = (
                self._led_pwm_range if led_pwm_invert() else 0
            )
            self._pi.set_PWM_dutycycle(g, off_dc)
            _LOG.info(
                "LED Hardware-PWM (pigpio): BCM %s, angefragt %s Hz, gesetzt %s Hz%s",
                g,
                freq,
                actual,
                ", invertiert (aktiv-low)" if led_pwm_invert() else "",
            )
            return
        except Exception as e:
            self._pi = None
            if not _led_fallback_rpi_gpio():
                _LOG.error(
                    "LED aus (pigpio fehlt, Fallback deaktiviert). Relais aktiv. %s",
                    e,
                )
                return
            try:
                self._GPIO.setup(g, self._GPIO.OUT)
                self._pwm = self._GPIO.PWM(
                    g,
                    float(self._pins.led_pwm_frequency_hz),
                )
                self._pwm.start(100.0 if led_pwm_invert() else 0.0)
                _LOG.warning(
                    "LED Fallback: Software-PWM (RPi.GPIO) BCM %s @ %.0f Hz — "
                    "für stabiles Bild pigpiod installieren/starten. pigpio-Fehler: %s",
                    g,
                    self._pins.led_pwm_frequency_hz,
                    e,
                )
            except Exception as e2:
                _LOG.error(
                    "LED weder pigpio noch RPi.GPIO-PWM: %s | %s",
                    e,
                    e2,
                )

    def set_valve(self, index: int, on: bool) -> None:
        if not 0 <= index < 8:
            return
        self._valve_on[index] = on
        if self._use_real and self._GPIO:
            pin = self._pins.valve_pins[index]
            self._GPIO.output(
                pin, self._relay_on_level if on else self._relay_off_level
            )

    def set_outlet(self, on: bool) -> None:
        self._outlet_on = on
        if self._use_real and self._GPIO:
            self._GPIO.output(
                self._pins.outlet_pin,
                self._relay_on_level if on else self._relay_off_level,
            )

    def set_schlafzimmer_room_light(self, on: bool) -> None:
        """Relais Raumlicht Schlafzimmer (an/aus)."""
        self._sz_light_on = on
        if self._use_real and self._GPIO:
            self._GPIO.output(
                self._pins.schlafzimmer_light_relay_pin,
                self._relay_on_level if on else self._relay_off_level,
            )

    def set_led_brightness(self, percent: float) -> None:
        """0–100 Prozent."""
        self._led_duty = max(0.0, min(100.0, percent))
        if self._pi is not None:
            dc = int(
                round(self._led_duty / 100.0 * float(self._led_pwm_range))
            )
            dc = max(0, min(self._led_pwm_range, dc))
            if led_pwm_invert():
                dc = self._led_pwm_range - dc
            self._pi.set_PWM_dutycycle(self._pins.led_pwm_pin, dc)
        elif self._pwm is not None:
            rpi_dc = self._led_duty
            if led_pwm_invert():
                rpi_dc = 100.0 - self._led_duty
            self._pwm.ChangeDutyCycle(rpi_dc)

    def cleanup(self) -> None:
        if self._pi is not None:
            try:
                off_dc = (
                    self._led_pwm_range if led_pwm_invert() else 0
                )
                self._pi.set_PWM_dutycycle(self._pins.led_pwm_pin, off_dc)
                self._pi.stop()
            except Exception as e:
                _LOG.debug("pigpio cleanup: %s", e)
            self._pi = None

        if self._pwm is not None:
            try:
                off_dc = 100.0 if led_pwm_invert() else 0.0
                self._pwm.ChangeDutyCycle(off_dc)
                self._pwm.stop()
            except Exception as e:
                _LOG.debug("PWM stop: %s", e)
            self._pwm = None

        if self._use_real and self._GPIO:
            # Relais auf AUS-Pegel setzen und Pins als OUTPUT stehen lassen.
            # KEIN GPIO.cleanup() für die Relais-Pins: cleanup() würde sie zu
            # Inputs machen → interne Pull-ups/-downs ziehen active-low-Relais
            # wieder AN (z. B. Raumlicht BCM 16, Ventile). So bleiben sie beim
            # Dienst-Stop sicher aus, bis der Dienst neu startet.
            for pin in self._pins.valve_pins:
                self._GPIO.output(pin, self._relay_off_level)
            self._GPIO.output(self._pins.outlet_pin, self._relay_off_level)
            self._GPIO.output(
                self._pins.schlafzimmer_light_relay_pin,
                self._relay_off_level,
            )
            self._GPIO = None

    def valve_states(self) -> list[bool]:
        return list(self._valve_on)
