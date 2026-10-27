"""Blockplaner für die Ventilsteuerung (rein funktional, ohne GPIO/Firestore).

Modell:
- Der Tag ist in uhrzeit-synchrone Blöcke à ``cycleMinutes`` geteilt (00:00,
  00:15, … Europe/Berlin). Beim Eintritt in einen Block wird pro Raum der
  effektive Öffnungsgrad berechnet und bleibt für den Block fest.
- ``prozent = openPercent × Zeitplanfaktor × Außenfaktor × Kaminfaktor``.
- Die Einschaltfenster werden im Block lückenlos hintereinander gelegt, sodass
  zu jedem Zeitpunkt etwa ``Summe(prozent)/100`` Ventile offen sind (gleichmäßiger
  Durchfluss). Lücken entstehen nur, wenn die Summe unter 100 % liegt.
- Pumpenschutz füllt eine verbleibende Lücke mit genau einem Schutzventil, das
  pro Block über alle freigegebenen Räume rotiert.

Alle Funktionen sind seiteneffektfrei und damit direkt testbar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from smarthome_pi import schedules as room_schedules

# Feste Stützstellen der raumbezogenen Außenkurve (°C).
OUTDOOR_CURVE_POINTS_C: tuple[float, ...] = (-5.0, 0.0, 5.0, 10.0, 15.0, 20.0)
NUM_VALVES = 8


# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
@dataclass
class SystemConfig:
    cycle_sec: float = 900.0  # 15 min
    valve_min_on_sec: float = 180.0
    pump_protection_enabled: bool = True
    outdoor_curve_enabled: bool = True
    weather_lat: float = 52.52
    weather_lon: float = 13.41
    heating_enabled: bool = True
    relay_switch_delay_sec: float = 1.0
    max_relay_switches_per_tick: int = 1
    manual_fireplace_mode: bool = False
    manual_fireplace_until: Any = None
    fireplace_mode_source: str = "auto"
    # Nur Anzeige in der App (Pi nutzt sie nicht für die Regelung).
    scale_temp_level1_c: float = 16.0
    scale_temp_level10_c: float = 26.0


def _f(doc: dict[str, Any], key: str, default: float) -> float:
    v = doc.get(key)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _b(doc: dict[str, Any], key: str, default: bool) -> bool:
    v = doc.get(key)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return default


def parse_system_config(data: dict[str, Any] | None) -> SystemConfig:
    if not data:
        return SystemConfig()
    cycle_min = _f(data, "cycleMinutes", 15.0)
    cycle_sec = max(60.0, cycle_min * 60.0)
    return SystemConfig(
        cycle_sec=cycle_sec,
        valve_min_on_sec=max(0.0, _f(data, "valveMinOnSec", 180.0)),
        pump_protection_enabled=_b(data, "pumpProtectionEnabled", True),
        outdoor_curve_enabled=_b(data, "outdoorCurveEnabled", True),
        weather_lat=_f(data, "weatherLat", 52.52),
        weather_lon=_f(data, "weatherLon", 13.41),
        heating_enabled=_b(data, "heatingEnabled", True),
        relay_switch_delay_sec=_f(data, "relaySwitchDelaySec", 1.0),
        max_relay_switches_per_tick=max(
            1, int(_f(data, "maxRelaySwitchesPerTick", 1.0))
        ),
        manual_fireplace_mode=_b(data, "manualFireplaceMode", False),
        manual_fireplace_until=data.get("manualFireplaceUntil"),
        fireplace_mode_source=str(data.get("fireplaceModeSource", "auto") or "auto"),
        scale_temp_level1_c=_f(data, "scaleTempAtLevel1C", 16.0),
        scale_temp_level10_c=_f(data, "scaleTempAtLevel10C", 26.0),
    )


# --------------------------------------------------------------------------- #
# Kamin (nur manuell — Auto-Erkennung/Referenzsensor wurden entfernt)
# --------------------------------------------------------------------------- #
def manual_fireplace_active(cfg: SystemConfig) -> bool:
    if cfg.manual_fireplace_mode:
        return True
    until = cfg.manual_fireplace_until
    if until is None:
        return False
    try:
        ts = until.timestamp() if hasattr(until, "timestamp") else float(until)
        return ts > datetime.now(timezone.utc).timestamp()
    except (AttributeError, TypeError, ValueError, OSError):
        return False


def default_kamin_multiplier(room_id: str) -> float:
    rid = room_id.lower()
    if rid in ("wohnzimmer", "kueche"):
        return 0.0
    if rid in ("schlafzimmer",):
        return 0.5
    return 0.8


def kamin_factor(room: dict[str, Any]) -> float:
    stored = room.get("kaminFactorPercent")
    if stored is not None:
        try:
            return max(0.0, min(1.0, float(stored) / 100.0))
        except (TypeError, ValueError):
            pass
    return default_kamin_multiplier(str(room.get("id", "")))


# --------------------------------------------------------------------------- #
# Außenkurve
# --------------------------------------------------------------------------- #
def _interp(points: tuple[float, ...], values: list[float], x: float) -> float:
    if x <= points[0]:
        return values[0]
    if x >= points[-1]:
        return values[-1]
    for i in range(len(points) - 1):
        lo, hi = points[i], points[i + 1]
        if lo <= x <= hi:
            span = hi - lo
            if span <= 1e-9:
                return values[i]
            frac = (x - lo) / span
            return values[i] + (values[i + 1] - values[i]) * frac
    return values[-1]


def outdoor_factor(
    room: dict[str, Any], outdoor_c: float | None, cfg: SystemConfig
) -> float:
    """Multiplikator aus der raumbezogenen Außenkurve (1.0 = neutral)."""
    if not cfg.outdoor_curve_enabled or outdoor_c is None:
        return 1.0
    raw = room.get("outdoorFactorPercent")
    if not isinstance(raw, list) or len(raw) < 2:
        return 1.0
    vals: list[float] = []
    for x in raw[: len(OUTDOOR_CURVE_POINTS_C)]:
        try:
            vals.append(float(x))
        except (TypeError, ValueError):
            vals.append(100.0)
    while len(vals) < len(OUTDOOR_CURVE_POINTS_C):
        vals.append(100.0)
    pct = _interp(OUTDOOR_CURVE_POINTS_C, vals, float(outdoor_c))
    return max(0.0, pct / 100.0)


# --------------------------------------------------------------------------- #
# Effektiver Öffnungsgrad
# --------------------------------------------------------------------------- #
@dataclass
class EffectiveResult:
    percent: float
    base_percent: float
    schedule_factor: float
    outdoor_factor: float
    kamin_factor: float


def effective_percent(
    room: dict[str, Any],
    cfg: SystemConfig,
    block_start: datetime,
    outdoor_c: float | None,
    fireplace_active: bool,
) -> EffectiveResult:
    if not bool(room.get("enabled", True)):
        return EffectiveResult(0.0, 0.0, 1.0, 1.0, 1.0)
    try:
        base = float(room.get("openPercent", 0.0))
    except (TypeError, ValueError):
        base = 0.0
    base = max(0.0, min(100.0, base))
    sched = room_schedules.active_factor_for_room(room, now=block_start)
    outf = outdoor_factor(room, outdoor_c, cfg)
    kf = kamin_factor(room) if fireplace_active else 1.0
    pct = base * sched * outf * kf
    pct = max(0.0, min(100.0, pct))
    return EffectiveResult(pct, base, sched, outf, kf)


# --------------------------------------------------------------------------- #
# Blockgrenzen
# --------------------------------------------------------------------------- #
def block_bounds(
    now_berlin: datetime, cycle_minutes: int
) -> tuple[datetime, datetime, int]:
    """Gibt (block_start, block_end, block_index_im_tag) zurück (Europe/Berlin)."""
    cm = int(max(1, cycle_minutes))
    minutes_since_midnight = now_berlin.hour * 60 + now_berlin.minute
    block_index = minutes_since_midnight // cm
    midnight = now_berlin.replace(hour=0, minute=0, second=0, microsecond=0)
    block_start = midnight + timedelta(minutes=block_index * cm)
    block_end = block_start + timedelta(minutes=cm)
    return block_start, block_end, block_index


# --------------------------------------------------------------------------- #
# Fensterlogik
# --------------------------------------------------------------------------- #
def _round_on_seconds(pct: float, cycle: float, min_on: float) -> float:
    """Einschaltdauer in s mit Stellantriebsschutz (Mindest-/Voll-Rundung)."""
    if pct <= 0.0:
        return 0.0
    if pct >= 90.0:
        return cycle
    on = pct / 100.0 * cycle
    if min_on > 0.0:
        if on < min_on / 2.0:
            return 0.0
        if on < min_on:
            return min(cycle, min_on)
    return min(cycle, on)


def _merge_windows(
    windows: list[tuple[float, float]], cycle: float
) -> list[tuple[float, float]]:
    cleaned = [(max(0.0, a), min(cycle, b)) for a, b in windows if b > a]
    if not cleaned:
        return []
    cleaned.sort()
    out = [cleaned[0]]
    for a, b in cleaned[1:]:
        la, lb = out[-1]
        if a <= lb + 1e-6:
            out[-1] = (la, max(lb, b))
        else:
            out.append((a, b))
    return out


def _find_gaps(
    all_windows: list[tuple[float, float]], cycle: float
) -> list[tuple[float, float]]:
    merged = _merge_windows(all_windows, cycle)
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for a, b in merged:
        if a > cursor + 1e-6:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < cycle - 1e-6:
        gaps.append((cursor, cycle))
    return gaps


# --------------------------------------------------------------------------- #
# Blockplan
# --------------------------------------------------------------------------- #
@dataclass
class ValvePlan:
    valve_index: int
    room_id: str
    percent: float
    base_percent: float
    schedule_factor: float
    outdoor_factor: float
    kamin_factor: float
    on_windows: list[tuple[float, float]] = field(default_factory=list)
    is_pump_protection: bool = False


@dataclass
class BlockPlan:
    block_start_epoch: float
    block_index: int
    cycle_sec: float
    outdoor_temp: float | None
    fireplace_active: bool
    valves: list[ValvePlan]

    def desired_states(self, elapsed_sec: float) -> list[bool]:
        states = [False] * NUM_VALVES
        for vp in self.valves:
            if not (0 <= vp.valve_index < NUM_VALVES):
                continue
            for a, b in vp.on_windows:
                if a <= elapsed_sec < b:
                    states[vp.valve_index] = True
                    break
        return states


def _eligible_protection_valves(rooms: list[dict[str, Any]]) -> list[int]:
    out: list[int] = []
    for room in rooms:
        vi = _valve_index(room)
        if not (0 <= vi < NUM_VALVES):
            continue
        if not bool(room.get("enabled", True)):
            continue
        if not bool(room.get("pumpProtectionEligible", True)):
            continue
        out.append(vi)
    return sorted(set(out))


def _valve_index(room: dict[str, Any]) -> int:
    v = room.get("valveIndex")
    if v is None:
        return -1
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def plan_block(
    rooms: list[dict[str, Any]],
    cfg: SystemConfig,
    block_start: datetime,
    block_index: int,
    outdoor_temp: float | None,
    fireplace_active: bool,
) -> BlockPlan:
    cycle = max(60.0, cfg.cycle_sec)

    # 1) Effektiven Prozentsatz pro Ventil (bei Doppelbelegung: Maximum).
    per_valve: dict[int, ValvePlan] = {}
    for room in rooms:
        vi = _valve_index(room)
        if not (0 <= vi < NUM_VALVES):
            continue
        eff = effective_percent(room, cfg, block_start, outdoor_temp, fireplace_active)
        existing = per_valve.get(vi)
        if existing is None or eff.percent > existing.percent:
            per_valve[vi] = ValvePlan(
                valve_index=vi,
                room_id=str(room.get("id", "")),
                percent=eff.percent,
                base_percent=eff.base_percent,
                schedule_factor=eff.schedule_factor,
                outdoor_factor=eff.outdoor_factor,
                kamin_factor=eff.kamin_factor,
            )

    # 2) Lückenlose Reihung der Einschaltfenster.
    cursor = 0.0
    for vp in sorted(per_valve.values(), key=lambda v: v.valve_index):
        on = _round_on_seconds(vp.percent, cycle, cfg.valve_min_on_sec)
        if on <= 0.0:
            vp.on_windows = []
            continue
        if on >= cycle:
            vp.on_windows = [(0.0, cycle)]
            continue
        start = cursor % cycle
        end = start + on
        if end <= cycle + 1e-6:
            vp.on_windows = [(start, min(cycle, end))]
        else:
            # Fenster würde über das Blockende laufen -> ans Blockende rücken
            # (nicht zerschneiden).
            vp.on_windows = [(cycle - on, cycle)]
        cursor += on

    # 3) Pumpenschutz: verbleibende Lücke mit rotierendem Schutzventil füllen.
    if cfg.pump_protection_enabled and cfg.heating_enabled:
        all_windows: list[tuple[float, float]] = []
        for vp in per_valve.values():
            all_windows.extend(vp.on_windows)
        gaps = _find_gaps(all_windows, cycle)
        if gaps:
            eligible = _eligible_protection_valves(rooms)
            if not eligible:
                eligible = sorted(per_valve.keys())
            if not eligible:
                eligible = [0]
            pvi = eligible[block_index % len(eligible)]
            vp = per_valve.get(pvi)
            if vp is None:
                vp = ValvePlan(
                    valve_index=pvi,
                    room_id="",
                    percent=0.0,
                    base_percent=0.0,
                    schedule_factor=1.0,
                    outdoor_factor=1.0,
                    kamin_factor=1.0,
                )
                per_valve[pvi] = vp
            vp.is_pump_protection = True
            vp.on_windows = _merge_windows(vp.on_windows + gaps, cycle)

    valves = sorted(per_valve.values(), key=lambda v: v.valve_index)
    return BlockPlan(
        block_start_epoch=block_start.timestamp(),
        block_index=block_index,
        cycle_sec=cycle,
        outdoor_temp=outdoor_temp,
        fireplace_active=fireplace_active,
        valves=valves,
    )


# --------------------------------------------------------------------------- #
# Tagestabelle (Debug / Simulation)
# --------------------------------------------------------------------------- #
def day_table(
    rooms: list[dict[str, Any]],
    cfg: SystemConfig,
    day: datetime,
    outdoor_temp: float | None,
    fireplace_active: bool = False,
) -> list[dict[str, Any]]:
    """Liefert pro Block des Tages die effektiven Prozentwerte je Ventil."""
    cm = int(max(1, round(cfg.cycle_sec / 60.0)))
    blocks_per_day = max(1, (24 * 60) // cm)
    midnight = room_schedules.now_berlin(day).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    rows: list[dict[str, Any]] = []
    for bi in range(blocks_per_day):
        block_start = midnight + timedelta(minutes=bi * cm)
        plan = plan_block(
            rooms, cfg, block_start, bi, outdoor_temp, fireplace_active
        )
        percents = [0.0] * NUM_VALVES
        protection = [False] * NUM_VALVES
        for vp in plan.valves:
            if 0 <= vp.valve_index < NUM_VALVES:
                percents[vp.valve_index] = round(vp.percent, 1)
                protection[vp.valve_index] = vp.is_pump_protection
        rows.append(
            {
                "block_index": bi,
                "time": block_start.strftime("%H:%M"),
                "percents": percents,
                "pump_protection": protection,
            }
        )
    return rows
