"""Firestore-Anbindung über on_snapshot-Listener (statt Polling).

Die Listener laufen in vom Firestore-Client verwalteten Hintergrund-Threads und
halten einen thread-sicheren Cache für ``rooms``, ``system/config``,
``devices/led`` und ``devices/outlets``. Änderungen setzen ein Event, das die
GPIO-Schleife (ohne eigenes Netzwerk-I/O) auslesen kann. Ein langsamer
Fallback-Vollpoll (Standard alle 10 min) heilt abgerissene Listener.

Lesevolumen: on_snapshot kostet nur beim Start und bei echten Änderungen Reads;
der Fallback-Poll fügt ~4 Reads alle 10 min hinzu. Damit weit unter dem
früheren Dauer-Polling (~13 000 Reads/Tag).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from google.api_core import exceptions as google_api_exceptions

_LOG = logging.getLogger(__name__)


class FirestoreSync:
    def __init__(
        self,
        db: Any,
        fallback_poll_sec: float = 600.0,
    ) -> None:
        self._db = db
        self._fallback_poll_sec = max(60.0, fallback_poll_sec)
        self._lock = threading.Lock()
        self._rooms: dict[str, dict[str, Any]] = {}
        self._config: dict[str, Any] = {}
        self._led: dict[str, Any] = {}
        self._outlets: dict[str, Any] = {}
        self._rooms_loaded = False
        self._config_loaded = False
        self._changed = threading.Event()
        self._last_update_ts = 0.0
        self._watches: list[Any] = []
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # Start / Stop
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self._register_listeners()
        self._poll_thread = threading.Thread(
            target=self._fallback_loop, name="firestore-fallback", daemon=True
        )
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop.set()
        for w in self._watches:
            try:
                w.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
        self._watches.clear()

    # ------------------------------------------------------------------ #
    # Listener
    # ------------------------------------------------------------------ #
    def _register_listeners(self) -> None:
        def on_rooms(col_snapshot, changes, read_time):  # noqa: ANN001
            rooms: dict[str, dict[str, Any]] = {}
            for doc in col_snapshot:
                d = doc.to_dict() or {}
                d["id"] = doc.id
                rooms[doc.id] = d
            with self._lock:
                self._rooms = rooms
                self._rooms_loaded = True
                self._last_update_ts = time.time()
            self._changed.set()

        def on_config(doc_snapshot, changes, read_time):  # noqa: ANN001
            data = _first_doc(doc_snapshot)
            with self._lock:
                self._config = data
                self._config_loaded = True
                self._last_update_ts = time.time()
            self._changed.set()

        def on_led(doc_snapshot, changes, read_time):  # noqa: ANN001
            data = _first_doc(doc_snapshot)
            with self._lock:
                self._led = data
                self._last_update_ts = time.time()
            self._changed.set()

        def on_outlets(doc_snapshot, changes, read_time):  # noqa: ANN001
            data = _first_doc(doc_snapshot)
            with self._lock:
                self._outlets = data
                self._last_update_ts = time.time()
            self._changed.set()

        self._watches = [
            self._db.collection("rooms").on_snapshot(on_rooms),
            self._db.collection("system").document("config").on_snapshot(on_config),
            self._db.collection("devices").document("led").on_snapshot(on_led),
            self._db.collection("devices")
            .document("outlets")
            .on_snapshot(on_outlets),
        ]

    # ------------------------------------------------------------------ #
    # Fallback-Vollpoll (Selbstheilung)
    # ------------------------------------------------------------------ #
    def _fallback_loop(self) -> None:
        # Erster Poll sofort, damit auch ohne Listener-Erstcallback Daten da sind.
        while not self._stop.is_set():
            try:
                self._poll_once()
            except google_api_exceptions.ResourceExhausted as e:
                _LOG.warning("Firestore 429 im Fallback-Poll: %s", e)
            except Exception as e:  # noqa: BLE001
                _LOG.debug("Fallback-Poll Fehler: %s", e)
            self._stop.wait(self._fallback_poll_sec)

    def _poll_once(self) -> None:
        rooms: dict[str, dict[str, Any]] = {}
        for doc in self._db.collection("rooms").stream():
            d = doc.to_dict() or {}
            d["id"] = doc.id
            rooms[doc.id] = d
        cfg = self._db.collection("system").document("config").get()
        led = self._db.collection("devices").document("led").get()
        outlets = self._db.collection("devices").document("outlets").get()
        with self._lock:
            if rooms:
                self._rooms = rooms
                self._rooms_loaded = True
            self._config = cfg.to_dict() or {} if cfg.exists else self._config
            self._config_loaded = self._config_loaded or (cfg.exists)
            if led.exists:
                self._led = led.to_dict() or {}
            if outlets.exists:
                self._outlets = outlets.to_dict() or {}
            self._last_update_ts = time.time()
        self._changed.set()

    # ------------------------------------------------------------------ #
    # Zugriff
    # ------------------------------------------------------------------ #
    def wait_until_ready(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._rooms_loaded or self._config_loaded:
                    return True
            time.sleep(0.2)
        return False

    def consume_changed(self) -> bool:
        """True, wenn seit dem letzten Aufruf eine Änderung ankam."""
        if self._changed.is_set():
            self._changed.clear()
            return True
        return False

    def snapshot(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Konsistente Kopie: (rooms_list, config, led, outlets)."""
        with self._lock:
            rooms = [dict(r) for r in self._rooms.values()]
            cfg = dict(self._config)
            led = dict(self._led)
            outlets = dict(self._outlets)
        return rooms, cfg, led, outlets

    @property
    def seconds_since_update(self) -> float:
        with self._lock:
            if self._last_update_ts <= 0.0:
                return float("inf")
            return time.time() - self._last_update_ts


def _first_doc(doc_snapshot: Any) -> dict[str, Any]:
    """on_snapshot auf ein Document liefert eine Liste mit 0/1 Snapshots."""
    try:
        for snap in doc_snapshot:
            if getattr(snap, "exists", True):
                return snap.to_dict() or {}
        return {}
    except TypeError:
        # Falls doch ein einzelner Snapshot übergeben wird.
        return doc_snapshot.to_dict() or {}
