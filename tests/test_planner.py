"""Tests für die Blockplanung (planner.py) und Zeitpläne (schedules.py)."""

from __future__ import annotations

import os
import sys
from datetime import datetime

from zoneinfo import ZoneInfo

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from smarthome_pi.planner import (  # noqa: E402
    SystemConfig,
    block_bounds,
    outdoor_factor,
    parse_system_config,
    plan_block,
)
from smarthome_pi.schedules import active_factor_for_room  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")


def _room(idx, pct, **kw):
    d = {
        "id": kw.get("id", f"r{idx}"),
        "name": f"Raum {idx}",
        "valveIndex": idx,
        "enabled": kw.get("enabled", True),
        "openPercent": pct,
        "kaminFactorPercent": kw.get("kaminFactorPercent", 80),
        "pumpProtectionEligible": kw.get("pumpProtectionEligible", True),
        "outdoorFactorPercent": kw.get("outdoorFactorPercent", [100] * 6),
        "temperatureSchedules": kw.get("temperatureSchedules", []),
    }
    return d


def _cfg(**kw):
    base = {
        "cycleMinutes": 15,
        "valveMinOnSec": 180,
        "pumpProtectionEnabled": True,
        "outdoorCurveEnabled": True,
        "heatingEnabled": True,
    }
    base.update(kw)
    return parse_system_config(base)


def _block_start():
    return datetime(2026, 1, 12, 10, 0, tzinfo=BERLIN)  # Montag 10:00


# --------------------------------------------------------------------------- #
# Blockgrenzen
# --------------------------------------------------------------------------- #
def test_block_bounds_aligned():
    now = datetime(2026, 1, 12, 10, 7, 30, tzinfo=BERLIN)
    start, end, idx = block_bounds(now, 15)
    assert start.hour == 10 and start.minute == 0
    assert end.hour == 10 and end.minute == 15
    assert idx == 40  # 10*60/15


def test_block_index_rotation_differs():
    _, _, i1 = block_bounds(datetime(2026, 1, 12, 10, 0, tzinfo=BERLIN), 15)
    _, _, i2 = block_bounds(datetime(2026, 1, 12, 10, 15, tzinfo=BERLIN), 15)
    assert i2 == i1 + 1


# --------------------------------------------------------------------------- #
# Reihung lückenlos bei Summe >= 100 %
# --------------------------------------------------------------------------- #
def test_sequential_packing_no_gap_when_sum_ge_100():
    cfg = _cfg()
    cycle = cfg.cycle_sec
    rooms = [_room(i, 50) for i in range(4)]  # Summe 200 %
    plan = plan_block(rooms, cfg, _block_start(), 40, 5.0, False)
    # Zu jedem Zeitpunkt sollte mind. 1 Ventil offen sein (keine Lücke).
    for t in range(0, int(cycle), 10):
        states = plan.desired_states(t)
        assert any(states), f"Lücke bei t={t}"


def test_sum_two_valves_half_open_each_time_two_on():
    cfg = _cfg()
    cycle = cfg.cycle_sec
    rooms = [_room(i, 50) for i in range(8)]  # Summe 400 % -> ~4 offen
    plan = plan_block(rooms, cfg, _block_start(), 40, 5.0, False)
    counts = [sum(plan.desired_states(t)) for t in range(0, int(cycle), 30)]
    avg = sum(counts) / len(counts)
    assert 3.0 <= avg <= 5.0


# --------------------------------------------------------------------------- #
# Pumpenschutz-Lückenfüller + Rotation
# --------------------------------------------------------------------------- #
def test_pump_protection_fills_full_gap_when_all_zero():
    cfg = _cfg()
    rooms = [_room(i, 0) for i in range(4)]
    plan = plan_block(rooms, cfg, _block_start(), 40, 5.0, False)
    # Genau ein Schutzventil, das den ganzen Block offen ist.
    prot = [vp for vp in plan.valves if vp.is_pump_protection]
    assert len(prot) == 1
    states = plan.desired_states(0)
    assert sum(states) == 1
    assert plan.desired_states(int(cfg.cycle_sec) - 1)[prot[0].valve_index]


def test_pump_protection_rotates_with_block_index():
    cfg = _cfg()
    rooms = [_room(i, 0) for i in range(4)]
    p0 = plan_block(rooms, cfg, _block_start(), 0, 5.0, False)
    p1 = plan_block(rooms, cfg, _block_start(), 1, 5.0, False)
    v0 = [vp.valve_index for vp in p0.valves if vp.is_pump_protection][0]
    v1 = [vp.valve_index for vp in p1.valves if vp.is_pump_protection][0]
    assert v0 != v1


def test_pump_protection_excludes_ineligible_room():
    cfg = _cfg()
    rooms = [_room(i, 0, pumpProtectionEligible=(i != 1)) for i in range(4)]
    for bi in range(8):
        plan = plan_block(rooms, cfg, _block_start(), bi, 5.0, False)
        prot = [vp.valve_index for vp in plan.valves if vp.is_pump_protection]
        assert 1 not in prot


def test_no_pump_protection_when_disabled():
    cfg = _cfg(pumpProtectionEnabled=False)
    rooms = [_room(i, 0) for i in range(4)]
    plan = plan_block(rooms, cfg, _block_start(), 0, 5.0, False)
    assert all(not vp.is_pump_protection for vp in plan.valves)
    assert sum(plan.desired_states(0)) == 0


# --------------------------------------------------------------------------- #
# Mindest-Einschaltzeit
# --------------------------------------------------------------------------- #
def test_min_on_time_rounds_small_to_zero():
    cfg = _cfg()  # cycle 900 s, min_on 180 s -> halb = 90 s
    # 5 % von 900 s = 45 s < 90 s -> 0
    rooms = [_room(0, 5, pumpProtectionEligible=False)]
    plan = plan_block(rooms, cfg, _block_start(), 0, 5.0, False)
    vp = next(vp for vp in plan.valves if vp.valve_index == 0)
    # Kann Pumpenschutz sein, aber base-Fenster aus 5 % darf es nicht geben,
    # ausser als Lückenfüller. Prüfe: ohne Pumpenschutz kein Fenster.
    cfg2 = _cfg(pumpProtectionEnabled=False)
    plan2 = plan_block([_room(0, 5)], cfg2, _block_start(), 0, 5.0, False)
    vp2 = next(vp for vp in plan2.valves if vp.valve_index == 0)
    assert vp2.on_windows == []


def test_min_on_time_lifts_medium_to_min():
    cfg = _cfg(pumpProtectionEnabled=False)
    # 15 % von 900 = 135 s, zwischen 90 und 180 -> auf 180 s angehoben
    plan = plan_block([_room(0, 15)], cfg, _block_start(), 0, 5.0, False)
    vp = next(vp for vp in plan.valves if vp.valve_index == 0)
    assert vp.on_windows
    a, b = vp.on_windows[0]
    assert abs((b - a) - 180.0) < 1e-6


def test_high_percent_full_cycle():
    cfg = _cfg(pumpProtectionEnabled=False)
    plan = plan_block([_room(0, 95)], cfg, _block_start(), 0, 5.0, False)
    vp = next(vp for vp in plan.valves if vp.valve_index == 0)
    assert vp.on_windows == [(0.0, cfg.cycle_sec)]


# --------------------------------------------------------------------------- #
# Außenkurve
# --------------------------------------------------------------------------- #
def test_outdoor_factor_interpolation():
    cfg = _cfg()
    room = _room(0, 100, outdoorFactorPercent=[130, 120, 110, 100, 90, 70])
    # Stützstellen -5,0,5,10,15,20
    assert abs(outdoor_factor(room, -5.0, cfg) - 1.30) < 1e-6
    assert abs(outdoor_factor(room, 0.0, cfg) - 1.20) < 1e-6
    assert abs(outdoor_factor(room, 2.5, cfg) - 1.15) < 1e-6  # Mitte 0..5
    assert abs(outdoor_factor(room, 20.0, cfg) - 0.70) < 1e-6


def test_outdoor_factor_clamps_outside_range():
    cfg = _cfg()
    room = _room(0, 100, outdoorFactorPercent=[130, 120, 110, 100, 90, 70])
    assert abs(outdoor_factor(room, -20.0, cfg) - 1.30) < 1e-6
    assert abs(outdoor_factor(room, 40.0, cfg) - 0.70) < 1e-6


def test_outdoor_factor_none_and_stale_is_neutral():
    cfg = _cfg()
    room = _room(0, 100, outdoorFactorPercent=[130, 120, 110, 100, 90, 70])
    assert outdoor_factor(room, None, cfg) == 1.0
    cfg_off = _cfg(outdoorCurveEnabled=False)
    assert outdoor_factor(room, -5.0, cfg_off) == 1.0


def test_outdoor_curve_affects_percent():
    cfg = _cfg(pumpProtectionEnabled=False)
    room = _room(0, 50, outdoorFactorPercent=[200, 200, 200, 200, 200, 200])
    plan = plan_block([room], cfg, _block_start(), 0, -5.0, False)
    vp = next(vp for vp in plan.valves if vp.valve_index == 0)
    # 50 % * 2.0 = 100 % -> voller Zyklus
    assert vp.percent == 100.0


# --------------------------------------------------------------------------- #
# Kaminfaktor
# --------------------------------------------------------------------------- #
def test_kamin_factor_reduces_when_active():
    cfg = _cfg(pumpProtectionEnabled=False)
    room = _room(0, 100, kaminFactorPercent=0)
    off = plan_block([room], cfg, _block_start(), 0, 5.0, False)
    on = plan_block([room], cfg, _block_start(), 0, 5.0, True)
    assert next(v for v in off.valves if v.valve_index == 0).percent == 100.0
    assert next(v for v in on.valves if v.valve_index == 0).percent == 0.0


# --------------------------------------------------------------------------- #
# Heizung aus / Raum deaktiviert
# --------------------------------------------------------------------------- #
def test_disabled_room_is_zero():
    cfg = _cfg(pumpProtectionEnabled=False)
    plan = plan_block([_room(0, 80, enabled=False)], cfg, _block_start(), 0, 5.0, False)
    vp = next(vp for vp in plan.valves if vp.valve_index == 0)
    assert vp.percent == 0.0
    assert vp.on_windows == []


# --------------------------------------------------------------------------- #
# Zeitpläne (multiplikativ, Mitternacht)
# --------------------------------------------------------------------------- #
def test_schedule_factor_multiplicative():
    room = {
        "id": "r",
        "temperatureSchedules": [
            _sched(50),  # 0.5
            _sched(150),  # 1.5
        ],
    }
    # Montag 10:00 im Fenster 00:00-23:59
    f = active_factor_for_room(room, now=_block_start())
    assert abs(f - 0.75) < 1e-6


def test_schedule_overnight_belongs_to_previous_day():
    room = {
        "id": "r",
        "temperatureSchedules": [
            {
                "id": "s",
                "weekDays": [6],  # Samstag
                "startHour": 22,
                "startMinute": 0,
                "endHour": 2,
                "endMinute": 0,
                "factorPercent": 50,
                "permanent": True,
                "enabled": True,
            }
        ],
    }
    sat_23 = datetime(2026, 1, 10, 23, 0, tzinfo=BERLIN)
    sun_01 = datetime(2026, 1, 11, 1, 0, tzinfo=BERLIN)
    sun_03 = datetime(2026, 1, 11, 3, 0, tzinfo=BERLIN)
    assert abs(active_factor_for_room(room, now=sat_23) - 0.5) < 1e-6
    assert abs(active_factor_for_room(room, now=sun_01) - 0.5) < 1e-6
    assert abs(active_factor_for_room(room, now=sun_03) - 1.0) < 1e-6


def _sched(factor_percent, **kw):
    return {
        "id": kw.get("id", "s"),
        "weekDays": kw.get("weekDays", [1, 2, 3, 4, 5, 6, 7]),
        "startHour": kw.get("startHour", 0),
        "startMinute": 0,
        "endHour": kw.get("endHour", 23),
        "endMinute": kw.get("endMinute", 59),
        "factorPercent": factor_percent,
        "permanent": True,
        "enabled": True,
    }
