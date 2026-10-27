r"""Lokale Simulation der Blockplanung ohne GPIO/Firestore.

Zeigt die Tagestabelle (96 Blöcke bei 15 min) mit effektivem Prozentsatz je
Ventil und die konkreten Einschaltfenster eines Blocks.

Start:
  .venv\Scripts\python scripts/debug_simulation.py --outdoor 5
  .venv\Scripts\python scripts/debug_simulation.py --block 44 --outdoor -5
  .venv\Scripts\python scripts/debug_simulation.py --schedules-test
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import Any

from zoneinfo import ZoneInfo

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from smarthome_pi.planner import (  # noqa: E402
    block_bounds,
    day_table,
    parse_system_config,
    plan_block,
)
from smarthome_pi.schedules import (  # noqa: E402
    active_factor_for_room,
    cleanup_expired_schedules_for_rooms,
)

BERLIN = ZoneInfo("Europe/Berlin")


def sample_config() -> dict[str, Any]:
    return {
        "heatingEnabled": True,
        "cycleMinutes": 15,
        "valveMinOnSec": 180,
        "pumpProtectionEnabled": True,
        "outdoorCurveEnabled": True,
        "weatherLat": 52.52,
        "weatherLon": 13.41,
    }


def sample_rooms() -> list[dict[str, Any]]:
    names = [
        ("wohnzimmer", "Wohnzimmer", 60, [120, 115, 110, 100, 90, 70]),
        ("schlafzimmer", "Schlafzimmer", 50, [110, 105, 100, 100, 95, 80]),
        ("badezimmer", "Bad", 70, [100, 100, 100, 100, 100, 100]),
        ("buero", "Büro", 55, [115, 110, 105, 100, 95, 80]),
        ("flur", "Flur", 40, [100, 100, 100, 100, 100, 100]),
        ("kueche", "Küche", 50, [110, 105, 100, 100, 95, 85]),
        ("wellnesszimmer", "Wellness", 60, [120, 110, 105, 100, 90, 70]),
        ("spielezimmer", "Spiele", 55, [110, 105, 100, 100, 95, 85]),
    ]
    out: list[dict[str, Any]] = []
    for i, (rid, name, pct, curve) in enumerate(names):
        out.append(
            {
                "id": rid,
                "name": name,
                "valveIndex": i,
                "enabled": True,
                "openPercent": pct,
                "kaminFactorPercent": 0 if rid in ("wohnzimmer", "kueche") else 80,
                "pumpProtectionEligible": rid != "schlafzimmer",
                "outdoorFactorPercent": curve,
                "temperatureSchedules": [],
            }
        )
    return out


def run_day_table(outdoor: float | None, fireplace: bool) -> None:
    cfg = parse_system_config(sample_config())
    rooms = sample_rooms()
    day = datetime.now(BERLIN)
    rows = day_table(rooms, cfg, day, outdoor, fireplace_active=fireplace)
    header = "Zeit  | " + " ".join(f"V{i}" for i in range(8))
    print(f"Tagestabelle (Außen={outdoor} °C, Kamin={fireplace})")
    print(header)
    print("-" * len(header))
    for row in rows:
        cells = " ".join(f"{int(p):2d}" for p in row["percents"])
        print(f"{row['time']} | {cells}")


def run_block(block_index: int, outdoor: float | None, fireplace: bool) -> None:
    cfg = parse_system_config(sample_config())
    rooms = sample_rooms()
    day = datetime.now(BERLIN).replace(hour=0, minute=0, second=0, microsecond=0)
    cm = max(1, int(round(cfg.cycle_sec / 60.0)))
    from datetime import timedelta

    block_start = day + timedelta(minutes=block_index * cm)
    _, _, bi = block_bounds(block_start, cm)
    plan = plan_block(rooms, cfg, block_start, bi, outdoor, fireplace)
    print(
        f"Block {block_index} ({block_start.strftime('%H:%M')}), "
        f"Zyklus {cfg.cycle_sec:.0f}s, Außen={outdoor} °C"
    )
    for vp in plan.valves:
        wins = ", ".join(f"[{a:.0f}-{b:.0f}s]" for a, b in vp.on_windows) or "—"
        flag = " (Pumpenschutz)" if vp.is_pump_protection else ""
        print(
            f"  V{vp.valve_index} {vp.room_id:<14} {vp.percent:5.1f}% "
            f"base={vp.base_percent:.0f} sched={vp.schedule_factor:.2f} "
            f"out={vp.outdoor_factor:.2f} kamin={vp.kamin_factor:.2f} -> {wins}{flag}"
        )


def run_schedules_test() -> None:
    # Samstag 23:00, Regel Sa 22-02 +25 % (factorPercent 125) -> aktiv.
    t_sat = datetime(2026, 1, 10, 23, 0, tzinfo=BERLIN)
    room = {
        "id": "wz",
        "temperatureSchedules": [
            {
                "id": "t1",
                "weekDays": [6],
                "startHour": 22,
                "startMinute": 0,
                "endHour": 2,
                "endMinute": 0,
                "factorPercent": 125,
                "permanent": False,
                "validFromYmd": "2026-01-01",
                "validToYmd": "2026-12-31",
                "enabled": True,
            }
        ],
    }
    f1 = active_factor_for_room(room, now=t_sat)
    print(f"Sa 23:00 Faktor = {f1:.2f} (erwartet 1.25)")
    # Sonntag 01:00 gehört zur Samstag-Regel (Mitternacht).
    t_sun = datetime(2026, 1, 11, 1, 0, tzinfo=BERLIN)
    f2 = active_factor_for_room(room, now=t_sun)
    print(f"So 01:00 Faktor = {f2:.2f} (erwartet 1.25, Mitternachtsfenster)")
    # Dienstag 23:00 nicht aktiv.
    t_tue = datetime(2026, 1, 6, 23, 0, tzinfo=BERLIN)
    f3 = active_factor_for_room(room, now=t_tue)
    print(f"Di 23:00 Faktor = {f3:.2f} (erwartet 1.00)")

    rooms = [
        {
            "id": "x",
            "temperatureSchedules": [
                {
                    "id": "old",
                    "weekDays": [1],
                    "startHour": 0,
                    "startMinute": 0,
                    "endHour": 1,
                    "endMinute": 0,
                    "factorPercent": 50,
                    "permanent": False,
                    "validFromYmd": "2020-01-01",
                    "validToYmd": "2020-01-02",
                    "enabled": True,
                }
            ],
        }
    ]
    upd = cleanup_expired_schedules_for_rooms(
        rooms, now=datetime(2026, 6, 1, 12, 0, tzinfo=BERLIN)
    )
    print("Cleanup abgelaufen:", "x" in upd and len(upd["x"]) == 0, "(erwartet True)")


def main() -> None:
    p = argparse.ArgumentParser(description="SmartHome Blockplanung Debug")
    p.add_argument("--outdoor", type=float, default=5.0, help="Außentemperatur °C")
    p.add_argument("--block", type=int, default=None, help="Einen Block detailliert")
    p.add_argument("--fireplace", action="store_true", help="Kaminmodus aktiv")
    p.add_argument("--schedules-test", action="store_true")
    args = p.parse_args()
    if args.schedules_test:
        run_schedules_test()
    elif args.block is not None:
        run_block(args.block, args.outdoor, args.fireplace)
    else:
        run_day_table(args.outdoor, args.fireplace)


if __name__ == "__main__":
    main()
