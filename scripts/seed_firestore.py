r"""Initiale Firestore-Dokumente fuer SmartHomePi anlegen (Prozent-Modell).

Nutzung:
  .venv\Scripts\python scripts/seed_firestore.py
"""

from __future__ import annotations

import os
from typing import Any

import firebase_admin
from firebase_admin import credentials, firestore

PROJECT_ID = "smarthomepi-9b569"

# Neutrale Aussenkurve: 6 Stuetzstellen (-5..20 C), 100 % = keine Aenderung.
_NEUTRAL_OUTDOOR = [100, 100, 100, 100, 100, 100]


def init_client() -> firestore.Client:
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if cred_path and os.path.isfile(cred_path):
        firebase_admin.initialize_app(
            credentials.Certificate(cred_path), {"projectId": PROJECT_ID}
        )
    else:
        firebase_admin.initialize_app(options={"projectId": PROJECT_ID})
    return firestore.client()


def seed_system(db: firestore.Client) -> None:
    config: dict[str, Any] = {
        "heatingEnabled": True,
        "cycleMinutes": 15,
        "valveMinOnSec": 180,
        "pumpProtectionEnabled": True,
        "outdoorCurveEnabled": True,
        "weatherLat": 52.52,
        "weatherLon": 13.41,
        "scaleTempAtLevel1C": 16.0,
        "scaleTempAtLevel10C": 26.0,
        "relaySwitchDelaySec": 1.0,
        "maxRelaySwitchesPerTick": 1,
        "manualFireplaceMode": False,
        "manualFireplaceUntil": None,
        "fireplaceModeSource": "auto",
    }
    db.collection("system").document("config").set(config, merge=True)
    print("system/config gesetzt")


def seed_devices(db: firestore.Client) -> None:
    db.collection("devices").document("led").set(
        {"on": False, "brightness": 0.0}, merge=True
    )
    db.collection("devices").document("outlets").set(
        {"schlafzimmer": False, "schlafzimmerLight": False}, merge=True
    )
    print("devices/led und devices/outlets gesetzt")


def _kamin_factor_percent(room_id: str) -> int:
    rid = room_id.lower()
    if rid in ("wohnzimmer", "kueche"):
        return 0
    if rid == "schlafzimmer":
        return 50
    return 80


def seed_rooms(db: firestore.Client) -> None:
    rooms = [
        {
            "id": "wohnzimmer",
            "name": "Wohnzimmer",
            "valveIndex": 0,
            "openPercent": 60,
            "lightIsDimmer": True,
            "controlsPiLed": True,
        },
        {
            "id": "schlafzimmer",
            "name": "Schlafzimmer",
            "valveIndex": 1,
            "openPercent": 50,
            "controlsPiOutlet": True,
            "controlsPiRoomLight": True,
        },
        {
            "id": "badezimmer",
            "name": "Bad",
            "valveIndex": 2,
            "openPercent": 70,
        },
        {
            "id": "buero",
            "name": "Büro",
            "valveIndex": 3,
            "openPercent": 55,
            "blindsShellyHost": "192.168.178.220",
        },
        {
            "id": "flur",
            "name": "Flur",
            "valveIndex": 4,
            "openPercent": 40,
            "blindsShellyHost": "192.168.178.221",
        },
        {
            "id": "kueche",
            "name": "Küche",
            "valveIndex": 5,
            "openPercent": 50,
            "blindsShellyHost": "192.168.178.222",
        },
        {
            "id": "wellnesszimmer",
            "name": "Wellnesszimmer",
            "valveIndex": 6,
            "openPercent": 60,
        },
        {
            "id": "spielezimmer",
            "name": "Spielezimmer",
            "valveIndex": 7,
            "openPercent": 55,
            "blindsShellyHost": "192.168.178.223",
        },
    ]
    for room in rooms:
        rid = room.pop("id")
        doc: dict[str, Any] = {
            "name": room["name"],
            "valveIndex": room["valveIndex"],
            "enabled": True,
            "openPercent": room.get("openPercent", 50),
            "kaminFactorPercent": _kamin_factor_percent(rid),
            "pumpProtectionEligible": rid != "schlafzimmer",
            "outdoorFactorPercent": list(_NEUTRAL_OUTDOOR),
            "temperatureSchedules": [],
            "blindsShellyHost": room.get("blindsShellyHost", ""),
            "blindsRollerId": 0,
            "lightShellyHost": "",
            "lightRelayIndex": 0,
            "lightIsDimmer": room.get("lightIsDimmer", False),
            "controlsPiLed": room.get("controlsPiLed", False),
            "controlsPiOutlet": room.get("controlsPiOutlet", False),
            "controlsPiRoomLight": room.get("controlsPiRoomLight", False),
        }
        db.collection("rooms").document(rid).set(doc, merge=True)
    print(f"{len(rooms)} Beispielraeume gesetzt")


def main() -> None:
    db = init_client()
    seed_system(db)
    seed_devices(db)
    seed_rooms(db)
    print("Seed abgeschlossen.")


if __name__ == "__main__":
    main()
