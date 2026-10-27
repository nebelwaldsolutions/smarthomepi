"""SmartHome Pi: Firestore ↔ GPIO über uhrzeit-synchrone Blockplanung.

Architektur:
- ``FirestoreSync`` hält per on_snapshot einen Cache (rooms, config, devices).
- ``WeatherPoller`` liefert die Außentemperatur im Hintergrund (optional).
- Die GPIO-Schleife (1 s) macht **kein** Netzwerk-I/O: Sie berechnet beim
  Blockwechsel den ``BlockPlan`` und wendet die Einschaltfenster an.
- Nutzervorgaben wirken ab dem nächsten Block; „Aus" (Heizung aus, Raum
  deaktiviert, openPercent 0, Handmodus) wirkt sofort.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore import SERVER_TIMESTAMP

from smarthome_pi.config import credentials_path, handmode_flag_path, load_pin_config
from smarthome_pi.firestore_sync import FirestoreSync
from smarthome_pi.gpio_controller import GpioController
from smarthome_pi.planner import (
    NUM_VALVES,
    BlockPlan,
    SystemConfig,
    block_bounds,
    manual_fireplace_active,
    parse_system_config,
    plan_block,
)
from smarthome_pi.schedules import cleanup_expired_schedules_for_rooms, now_berlin
from smarthome_pi.weather import WeatherPoller

_lvl = getattr(
    logging, os.environ.get("SMH_LOG_LEVEL", "INFO").upper(), logging.INFO
)
logging.basicConfig(level=_lvl, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_SCHED_CLEANUP_INTERVAL_SEC = 3600.0
_STATE_HEARTBEAT_SEC = 300.0
_WATCHDOG_WARN_SEC = 900.0


def _init_firebase() -> firestore.Client:
    path = credentials_path()
    if path and os.path.isfile(path):
        cred = credentials.Certificate(path)
        firebase_admin.initialize_app(cred)
    else:
        logger.warning(
            "GOOGLE_APPLICATION_CREDENTIALS nicht gesetzt — nutze Application "
            "Default Credentials (lokal ggf. nicht vorhanden)."
        )
        firebase_admin.initialize_app()
    return firestore.client()


def _valve_state_payload(plan: BlockPlan, states: list[bool]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    by_index = {vp.valve_index: vp for vp in plan.valves}
    for i in range(NUM_VALVES):
        vp = by_index.get(i)
        if vp is None:
            out.append(
                {
                    "roomId": None,
                    "percent": 0.0,
                    "basePercent": 0.0,
                    "scheduleFactor": 1.0,
                    "outdoorFactor": 1.0,
                    "kaminFactor": 1.0,
                    "onFromSec": None,
                    "onToSec": None,
                    "isOn": bool(states[i]),
                    "isPumpProtection": False,
                }
            )
            continue
        first = vp.on_windows[0] if vp.on_windows else None
        out.append(
            {
                "roomId": vp.room_id or None,
                "percent": round(vp.percent, 1),
                "basePercent": round(vp.base_percent, 1),
                "scheduleFactor": round(vp.schedule_factor, 3),
                "outdoorFactor": round(vp.outdoor_factor, 3),
                "kaminFactor": round(vp.kamin_factor, 3),
                "onFromSec": round(first[0], 1) if first else None,
                "onToSec": round(first[1], 1) if first else None,
                "isOn": bool(states[i]),
                "isPumpProtection": vp.is_pump_protection,
            }
        )
    return out


def _live_force_off(
    rooms: list[dict[str, Any]],
    sys_cfg: SystemConfig,
    desired: list[bool],
    handmode: bool,
) -> list[bool]:
    """Sofort-Aus überlagert den Blockplan (Aus wirkt ohne auf den Block zu warten)."""
    if handmode:
        return [True] * NUM_VALVES
    if not sys_cfg.heating_enabled:
        return [False] * NUM_VALVES
    out = list(desired)
    for room in rooms:
        v = room.get("valveIndex")
        try:
            vi = int(v) if v is not None else -1
        except (TypeError, ValueError):
            vi = -1
        if not (0 <= vi < NUM_VALVES):
            continue
        enabled = bool(room.get("enabled", True))
        try:
            base = float(room.get("openPercent", 0.0))
        except (TypeError, ValueError):
            base = 0.0
        if not enabled or base <= 0.0:
            out[vi] = False
    return out


def run_loop() -> None:
    logger.info("SmartHome Pi startet (Blockplanung)")
    db = _init_firebase()
    gpio = GpioController(load_pin_config())

    sync = FirestoreSync(db, fallback_poll_sec=600.0)
    sync.start()
    if not sync.wait_until_ready(timeout=30.0):
        logger.warning("Firestore noch nicht bereit — fahre mit Defaults fort.")

    rooms, cfg_doc, led_doc, out_doc = sync.snapshot()
    sys_cfg = parse_system_config(cfg_doc)

    weather = WeatherPoller(sys_cfg.weather_lat, sys_cfg.weather_lon)
    weather.start()

    tick = float(os.environ.get("SMH_TICK_SEC", "1.0"))

    plan: BlockPlan | None = None
    prev_block_epoch: float | None = None
    prev_fireplace = False
    prev_states = [False] * NUM_VALVES
    last_relay_switch_ts = 0.0
    last_sched_cleanup = 0.0
    last_state_write = 0.0
    prev_handmode = False
    last_watchdog_warn = 0.0

    try:
        while True:
            try:
                now_m = time.monotonic()
                berlin = now_berlin()

                rooms, cfg_doc, led_doc, out_doc = sync.snapshot()
                sys_cfg = parse_system_config(cfg_doc)
                weather.set_coords(sys_cfg.weather_lat, sys_cfg.weather_lon)

                cycle_minutes = max(1, int(round(sys_cfg.cycle_sec / 60.0)))
                block_start, block_end, block_index = block_bounds(
                    berlin, cycle_minutes
                )
                block_epoch = block_start.timestamp()
                fireplace_active = manual_fireplace_active(sys_cfg)
                outdoor_for_plan = weather.temperature_c

                # Zeitplan-Bereinigung (seltener Firestore-Write).
                if now_m - last_sched_cleanup >= _SCHED_CLEANUP_INTERVAL_SEC:
                    last_sched_cleanup = now_m
                    try:
                        upd = cleanup_expired_schedules_for_rooms(rooms)
                        for rid, new_sched in upd.items():
                            db.collection("rooms").document(rid).set(
                                {"temperatureSchedules": new_sched}, merge=True
                            )
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Zeitplan-Bereinigung: %s", e)

                # Blockplan nur beim Blockwechsel / Start / Kamin-Umschaltung.
                need_plan = (
                    plan is None
                    or prev_block_epoch != block_epoch
                    or prev_fireplace != fireplace_active
                )
                if need_plan:
                    plan = plan_block(
                        rooms,
                        sys_cfg,
                        block_start,
                        block_index,
                        outdoor_for_plan,
                        fireplace_active,
                    )
                    prev_block_epoch = block_epoch
                    prev_fireplace = fireplace_active
                    logger.info(
                        "Neuer Block %s (Index %d, Außen=%s, Kamin=%s)",
                        block_start.strftime("%H:%M"),
                        block_index,
                        outdoor_for_plan,
                        fireplace_active,
                    )

                handmode = os.path.isfile(handmode_flag_path())
                if handmode != prev_handmode:
                    logger.warning(
                        "Handmodus %s", "AKTIV (alle Kreise AN)" if handmode else "beendet"
                    )
                    prev_handmode = handmode

                elapsed = (berlin - block_start).total_seconds()
                elapsed = max(0.0, min(sys_cfg.cycle_sec, elapsed))
                desired = plan.desired_states(elapsed)
                desired = _live_force_off(rooms, sys_cfg, desired, handmode)

                # Relais schalten (ratenbegrenzt).
                current_states = gpio.valve_states()
                to_switch = [
                    i
                    for i, (cur, des) in enumerate(zip(current_states, desired))
                    if cur != des
                ]
                now_mono = time.monotonic()
                min_delay = max(0.05, sys_cfg.relay_switch_delay_sec)
                max_switches = max(1, sys_cfg.max_relay_switches_per_tick)
                if to_switch and (now_mono - last_relay_switch_ts) >= min_delay:
                    for idx in to_switch[:max_switches]:
                        gpio.set_valve(idx, desired[idx])
                        last_relay_switch_ts = now_mono

                # LED (nur bei explizitem on:false aus).
                raw_led = float(led_doc.get("brightness", 0.0) or 0.0)
                led_pct = (
                    0.0
                    if led_doc.get("on") is False
                    else max(0.0, min(100.0, raw_led))
                )
                gpio.set_led_brightness(led_pct)

                gpio.set_outlet(
                    bool(out_doc.get("schlafzimmer", out_doc.get("main", False)))
                )
                gpio.set_schlafzimmer_room_light(
                    bool(out_doc.get("schlafzimmerLight", False))
                )

                # Watchdog: warnt, wenn der Sync lange still ist (Plan läuft lokal weiter).
                if sync.seconds_since_update > _WATCHDOG_WARN_SEC:
                    if now_m - last_watchdog_warn >= _WATCHDOG_WARN_SEC:
                        last_watchdog_warn = now_m
                        logger.warning(
                            "Firestore seit %.0f s ohne Update — nutze letzten Cache.",
                            sync.seconds_since_update,
                        )

                # State schreiben: bei Ventil-/Blockwechsel oder als Heartbeat.
                states_changed = desired != prev_states
                if (
                    states_changed
                    or need_plan
                    or (now_m - last_state_write >= _STATE_HEARTBEAT_SEC)
                ):
                    last_state_write = now_m
                    prev_states = list(desired)
                    payload: dict[str, Any] = {
                        "blockStart": block_start.isoformat(),
                        "blockEnd": block_end.isoformat(),
                        "blockIndex": block_index,
                        "cycleSec": sys_cfg.cycle_sec,
                        "outdoorTemp": weather.last_value_c,
                        "outdoorStale": weather.stale,
                        "outdoorUpdatedAt": SERVER_TIMESTAMP,
                        "heatingEnabled": sys_cfg.heating_enabled,
                        "fireplaceActive": fireplace_active,
                        "fireplaceManualActive": manual_fireplace_active(sys_cfg),
                        "handModeActive": handmode,
                        "valves": _valve_state_payload(plan, desired),
                        "lastGpioTick": SERVER_TIMESTAMP,
                        "piUptimeSec": int(time.monotonic()),
                    }
                    try:
                        db.collection("system").document("state").set(
                            payload, merge=True
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning("State-Write: %s", e)

                time.sleep(tick)
            except Exception as e:  # noqa: BLE001
                logger.exception("Tick-Fehler: %s", e)
                time.sleep(max(tick, 5.0))
    except KeyboardInterrupt:
        logger.info("Beende …")
    finally:
        weather.stop()
        sync.stop()
        gpio.cleanup()


if __name__ == "__main__":
    run_loop()
