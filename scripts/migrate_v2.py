r"""Einmalige Migration Firestore v1 -> v2 (Prozent-/Blockmodell).

Wandelt bestehende Dokumente um:
- rooms: openPercent (Default 50), outdoorFactorPercent [100]*6, kaminFactorPercent
  (aus kaminReductionFactor oder Default je Raum), pumpProtectionEligible (True),
  temperatureSchedules: boostPercent -> factorPercent (= 100 + boost).
  Entfernt: targetTemp, offsetVsReference, corrByOutdoorBin, curveA/B,
  targetTempUpdatedAt, hasHeating/hasBlinds/..., ambientLightType, kaminReductionFactor.
- system/config: heatingCycleSec -> cycleMinutes; neue Keys ergaenzen;
  Kurven-/Lern-/Referenz-Keys entfernen.
- heating_samples: unveraendert (wird nicht mehr genutzt).

Nutzung:
  .venv\Scripts\python scripts/migrate_v2.py           # Vorschau (dry-run)
  .venv\Scripts\python scripts/migrate_v2.py --apply   # schreiben
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore import DELETE_FIELD

PROJECT_ID = "smarthomepi-9b569"
_NEUTRAL_OUTDOOR = [100, 100, 100, 100, 100, 100]

_ROOM_OBSOLETE = [
    "targetTemp",
    "offsetVsReference",
    "corrByOutdoorBin",
    "curveA",
    "curveB",
    "targetTempUpdatedAt",
    "hasHeating",
    "hasBlinds",
    "hasSwitchedOutlet",
    "hasAmbientLight",
    "ambientLightType",
    "kaminReductionFactor",
]

_CONFIG_OBSOLETE = [
    "minDuty",
    "maxDuty",
    "outdoorTempRefC",
    "outdoorTempNoHeatC",
    "dutyAtColdOutdoor",
    "targetTempRefC",
    "referenceRoomId",
    "referenceKp",
    "referenceFactorMin",
    "referenceFactorMax",
    "referenceSensorEnabled",
    "learnEnabled",
    "learnRate",
    "outdoorBins",
    "corrClampAbs",
    "learnMinMinutesAfterSetpoint",
    "kaminRiseThresholdCPerHour",
    "kaminOverTargetDeltaC",
    "kaminTrendWindowSec",
    "pumpProtectionValveIndex",
    "heatingCycleSec",
]


def init_client() -> firestore.Client:
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path and os.path.isfile(cred_path):
        firebase_admin.initialize_app(
            credentials.Certificate(cred_path), {"projectId": PROJECT_ID}
        )
    else:
        firebase_admin.initialize_app(options={"projectId": PROJECT_ID})
    return firestore.client()


def _kamin_factor_percent(room_id: str, data: dict[str, Any]) -> int:
    old = data.get("kaminReductionFactor")
    if old is not None:
        try:
            return int(round(max(0.0, min(1.0, float(old))) * 100))
        except (TypeError, ValueError):
            pass
    rid = room_id.lower()
    if rid in ("wohnzimmer", "kueche"):
        return 0
    if rid == "schlafzimmer":
        return 50
    return 80


def _migrate_schedules(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for rule in raw:
        if not isinstance(rule, dict):
            continue
        r = dict(rule)
        if "factorPercent" not in r:
            boost = r.pop("boostPercent", 0) or 0
            try:
                r["factorPercent"] = int(round(100 + float(boost)))
            except (TypeError, ValueError):
                r["factorPercent"] = 100
        else:
            r.pop("boostPercent", None)
        out.append(r)
    return out


def migrate_room(rid: str, data: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if "openPercent" not in data:
        updates["openPercent"] = 50
    if not isinstance(data.get("outdoorFactorPercent"), list):
        updates["outdoorFactorPercent"] = list(_NEUTRAL_OUTDOOR)
    if "kaminFactorPercent" not in data:
        updates["kaminFactorPercent"] = _kamin_factor_percent(rid, data)
    if "pumpProtectionEligible" not in data:
        updates["pumpProtectionEligible"] = rid.lower() != "schlafzimmer"
    new_sched = _migrate_schedules(data.get("temperatureSchedules"))
    if new_sched != data.get("temperatureSchedules"):
        updates["temperatureSchedules"] = new_sched
    for key in _ROOM_OBSOLETE:
        if key in data:
            updates[key] = DELETE_FIELD
    return updates


def migrate_config(data: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if "cycleMinutes" not in data:
        cyc = data.get("heatingCycleSec", 900.0)
        try:
            updates["cycleMinutes"] = max(1, int(round(float(cyc) / 60.0)))
        except (TypeError, ValueError):
            updates["cycleMinutes"] = 15
    defaults = {
        "heatingEnabled": True,
        "valveMinOnSec": 180,
        "pumpProtectionEnabled": data.get("pumpProtectionEnabled", True),
        "outdoorCurveEnabled": True,
        "scaleTempAtLevel1C": 16.0,
        "scaleTempAtLevel10C": 26.0,
        "relaySwitchDelaySec": data.get("relaySwitchDelaySec", 1.0),
        "maxRelaySwitchesPerTick": data.get("maxRelaySwitchesPerTick", 1),
    }
    for k, v in defaults.items():
        if k not in data:
            updates[k] = v
    for key in _CONFIG_OBSOLETE:
        if key in data:
            updates[key] = DELETE_FIELD
    return updates


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Änderungen schreiben")
    args = ap.parse_args()
    db = init_client()

    print("== rooms ==")
    for doc in db.collection("rooms").stream():
        data = doc.to_dict() or {}
        updates = migrate_room(doc.id, data)
        if updates:
            keys = sorted(updates.keys())
            print(f"  {doc.id}: {keys}")
            if args.apply:
                db.collection("rooms").document(doc.id).set(updates, merge=True)

    print("== system/config ==")
    cfg_ref = db.collection("system").document("config")
    cfg = cfg_ref.get()
    cfg_data = cfg.to_dict() or {} if cfg.exists else {}
    cfg_updates = migrate_config(cfg_data)
    if cfg_updates:
        print(f"  config: {sorted(cfg_updates.keys())}")
        if args.apply:
            cfg_ref.set(cfg_updates, merge=True)

    print("APPLY" if args.apply else "DRY-RUN (mit --apply schreiben)")


if __name__ == "__main__":
    main()
