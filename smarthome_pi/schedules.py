"""Raum-Zeitpläne: Heiz-Faktor in Europa/Berlin, Ablauf-Bereinigung.

Ein Zeitplan liefert einen **Multiplikator** (``factorPercent``) auf den
Öffnungsgrad eines Raums, z. B. Nachtabsenkung 50 % oder Boost 125 %. Mehrere
gleichzeitig aktive Regeln werden multipliziert. Fenster über Mitternacht
(``startHour`` > ``endHour``) werden korrekt dem jeweiligen Kalendertag
zugeordnet (Abendteil = heutiger Wochentag, Morgenteil = Wochentag des Vortags).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
# Sicherheitsgrenzen für den resultierenden Multiplikator (Produkt aller Regeln).
FACTOR_MIN = 0.0
FACTOR_MAX = 3.0


def _parse_ymd(s: Any) -> date | None:
    if s is None or s == "" or not isinstance(s, str):
        return None
    try:
        parts = s.strip().split("-", 2)
        if len(parts) != 3:
            return None
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, TypeError):
        return None


def now_berlin(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(BERLIN)
    if now.tzinfo is None:
        return now.replace(tzinfo=BERLIN)
    return now.astimezone(BERLIN)


def _weekday_dart_style(dt: datetime) -> int:
    """1 = Montag … 7 = Sonntag (wie Dart DateTime.weekday)."""
    return dt.weekday() + 1


def _weekday_set(rule: dict[str, Any]) -> set[int]:
    wdays = rule.get("weekDays")
    if not isinstance(wdays, list):
        return set()
    out: set[int] = set()
    for x in wdays:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _rule_factor(rule: dict[str, Any]) -> float:
    """Multiplikator aus ``factorPercent`` (Rückfall: 100 + boostPercent)."""
    raw = rule.get("factorPercent")
    if raw is None:
        boost = rule.get("boostPercent")
        if boost is None:
            return 1.0
        try:
            return max(0.0, (100.0 + float(boost)) / 100.0)
        except (TypeError, ValueError):
            return 1.0
    try:
        return max(0.0, float(raw) / 100.0)
    except (TypeError, ValueError):
        return 1.0


def _rule_active_at(rule: dict[str, Any], dt_berlin: datetime) -> bool:
    """True, wenn die Regel zum Zeitpunkt dt_berlin greift (inkl. Mitternacht)."""
    if not bool(rule.get("enabled", True)):
        return False
    wd_set = _weekday_set(rule)
    if not wd_set:
        return False

    sh = int(rule.get("startHour", 0) or 0)
    sm = int(rule.get("startMinute", 0) or 0)
    eh = int(rule.get("endHour", 0) or 0)
    em = int(rule.get("endMinute", 0) or 0)
    start = max(0, min(23, sh)) * 60 + max(0, min(59, sm))
    end = max(0, min(23, eh)) * 60 + max(0, min(59, em))
    if start == end:
        return False

    cur = dt_berlin.hour * 60 + dt_berlin.minute
    today_wd = _weekday_dart_style(dt_berlin)

    if start < end:
        if not (start <= cur < end):
            return False
        if today_wd not in wd_set:
            return False
        ref_date = dt_berlin.date()
    else:
        # Fenster über Mitternacht.
        if cur >= start:
            # Abendteil gehört zum heutigen Wochentag.
            if today_wd not in wd_set:
                return False
            ref_date = dt_berlin.date()
        elif cur < end:
            # Morgenteil gehört zum Wochentag des Vortags.
            yesterday = dt_berlin - timedelta(days=1)
            if _weekday_dart_style(yesterday) not in wd_set:
                return False
            ref_date = yesterday.date()
        else:
            return False

    if not bool(rule.get("permanent", False)):
        vf = _parse_ymd(rule.get("validFromYmd"))
        vt = _parse_ymd(rule.get("validToYmd"))
        if vf is not None and ref_date < vf:
            return False
        if vt is not None and ref_date > vt:
            return False
    return True


def active_factor_for_room(
    room: dict[str, Any],
    *,
    now: datetime | None = None,
) -> float:
    """Produkt der Multiplikatoren aller aktiven Regeln (1.0 = keine Änderung)."""
    raw = room.get("temperatureSchedules")
    if not isinstance(raw, list) or not raw:
        return 1.0
    dt = now_berlin(now)
    factor = 1.0
    for rule in raw:
        if not isinstance(rule, dict):
            continue
        if _rule_active_at(rule, dt):
            factor *= _rule_factor(rule)
    return max(FACTOR_MIN, min(FACTOR_MAX, factor))


def cleanup_expired_schedules_for_rooms(
    rooms: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Entfernt nicht-dauerhafte Regeln nach ``validToYmd``.

    Returns: roomId -> neue Liste (nur für geänderte Räume).
    """
    dt = now_berlin(now)
    today = dt.date()
    updates: dict[str, list[dict[str, Any]]] = {}
    for room in rooms:
        rid = str(room.get("id", ""))
        if not rid:
            continue
        raw = room.get("temperatureSchedules")
        if not isinstance(raw, list) or not raw:
            continue
        new_list: list[dict[str, Any]] = []
        changed = False
        for rule in raw:
            if not isinstance(rule, dict):
                changed = True
                continue
            if bool(rule.get("permanent", False)):
                new_list.append(rule)
                continue
            vt = _parse_ymd(rule.get("validToYmd"))
            if vt is not None and today > vt:
                changed = True
                continue
            new_list.append(rule)
        if changed and len(new_list) != len(raw):
            updates[rid] = new_list
    return updates
