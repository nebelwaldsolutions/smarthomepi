"""Open-Meteo: Außentemperatur, als Hintergrund-Poller (kein I/O im Regelkreis).

``WeatherPoller`` ruft in einem eigenen Thread alle ``interval_sec`` die aktuelle
Außentemperatur ab und behält den letzten Wert samt Zeitstempel. Fällt der Abruf
aus, bleibt der letzte Wert gültig, bis er älter als ``stale_after_sec`` ist
(dann ``stale`` = True). Der Planer nutzt bei fehlendem/veraltetem Wert den
neutralen Außenfaktor 1.0 statt eines Pauschalwerts.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

_LOG = logging.getLogger(__name__)

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


def fetch_outdoor_temp_c(lat: float, lon: float) -> float | None:
    try:
        r = requests.get(
            OPEN_METEO,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m",
                "timezone": "auto",
            },
            timeout=15,
        )
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        cur = data.get("current") or {}
        t = cur.get("temperature_2m")
        if t is None:
            return None
        return float(t)
    except Exception as e:  # noqa: BLE001 - Netzwerkfehler sollen den Thread nicht killen
        _LOG.warning("Wetterabruf fehlgeschlagen: %s", e)
        return None


class WeatherPoller:
    """Hintergrund-Thread, der die Außentemperatur periodisch aktualisiert."""

    def __init__(
        self,
        lat: float,
        lon: float,
        interval_sec: float = 900.0,
        stale_after_sec: float = 3 * 3600.0,
    ) -> None:
        self._lat = lat
        self._lon = lon
        self._interval = max(60.0, interval_sec)
        self._stale_after = max(self._interval, stale_after_sec)
        self._lock = threading.Lock()
        self._temp: float | None = None
        self._updated_at: float = 0.0  # time.time() des letzten Erfolgs
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def set_coords(self, lat: float, lon: float) -> None:
        with self._lock:
            self._lat = lat
            self._lon = lon

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="weather-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                lat, lon = self._lat, self._lon
            t = fetch_outdoor_temp_c(lat, lon)
            if t is not None:
                with self._lock:
                    self._temp = t
                    self._updated_at = time.time()
            self._stop.wait(self._interval)

    @property
    def temperature_c(self) -> float | None:
        """Letzter Wert, oder None wenn noch keiner/veraltet (stale)."""
        with self._lock:
            if self._temp is None:
                return None
            if time.time() - self._updated_at > self._stale_after:
                return None
            return self._temp

    @property
    def last_value_c(self) -> float | None:
        """Letzter bekannter Wert, auch wenn veraltet (für Anzeige)."""
        with self._lock:
            return self._temp

    @property
    def updated_at(self) -> float:
        with self._lock:
            return self._updated_at

    @property
    def stale(self) -> bool:
        with self._lock:
            if self._temp is None:
                return True
            return time.time() - self._updated_at > self._stale_after
