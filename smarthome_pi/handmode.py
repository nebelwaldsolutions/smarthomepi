"""Konsole: Wartungs-Handmodus — alle 8 Heizkreise-Ventile dauerhaft öffnen.

Der laufende Dienst prüft eine Flag-Datei (Standard ``/tmp/smarthome_handmode.flag``).
Solange sie existiert, bleiben alle Stellmotoren/Kreise auf AN; Firestore-Sollwerte
werden weiterhin gelesen/geschrieben (App), die Ventilsteuerung ignoriert die Regelung.

Beispiele::

    cd ~/smarthome/pi-controller && source venv/bin/activate
    python -m smarthome_pi.handmode activate
    python -m smarthome_pi.handmode status
    python -m smarthome_pi.handmode deactivate

Alternative::

    touch /tmp/smarthome_handmode.flag
    rm /tmp/smarthome_handmode.flag
"""

from __future__ import annotations

import argparse
import os
import sys

from smarthome_pi.config import handmode_flag_path


def _activate() -> None:
    path = handmode_flag_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "handmode\n"
            "Alle 8 Heizkreise bleiben geöffnet bis diese Datei gelöscht wird "
            "oder 'deactivate' ausgeführt wird.\n"
        )
    print(f"Handmodus AKTIV ({path}) — alle Ventile dauernd AN.")


def _deactivate() -> None:
    path = handmode_flag_path()
    try:
        os.unlink(path)
    except FileNotFoundError:
        print(f"Handmodus war nicht aktiv ({path}).")
        return
    print(f"Handmodus BEENDET ({path} entfernt) — normale Regelung greift wieder.")


def _status() -> None:
    path = handmode_flag_path()
    if os.path.isfile(path):
        print(f"Handmodus: AKTIV ({path})")
        sys.exit(0)
    print(f"Handmodus: inaktiv ({path} fehlt)")
    sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Wartung: alle Heizkreise auf Dauer-AN.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("activate", help="Flag setzen — Ventile ignorieren App-Regelung")
    sub.add_parser("deactivate", help="Flag entfernen — wieder normal")
    sub.add_parser("status", help="Prüfen ob aktiv (Exit 0 = aktiv, 1 = nein)")

    args = p.parse_args(argv)
    if args.cmd == "activate":
        _activate()
    elif args.cmd == "deactivate":
        _deactivate()
    elif args.cmd == "status":
        _status()


if __name__ == "__main__":
    main()
