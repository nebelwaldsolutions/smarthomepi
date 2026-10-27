# SmartHome Pi (Python)

Liest `rooms`, `system/config`, `devices/led`, `devices/outlets` (`schlafzimmer`, `schlafzimmerLight`, …) aus Firestore **per `on_snapshot`-Listener** (event-getrieben, kein Polling mehr) und steuert GPIO. Schreibt `system/state` (Admin SDK). Pin-Übersicht: [HARDWARE_PINS.md](HARDWARE_PINS.md).

## Regelungsmodell (ab v2)

Keine Selbstlernen-/Sensor-/Solltemperatur-Regelung mehr. Stattdessen:

- Jeder Raum hat einen **Öffnungsgrad** `openPercent` (0–100 %) — in der App als Thermostat-Stufe 1–10 dargestellt, intern 0–100.
- Der Tag wird in **96 uhrzeit-synchrone 15-min-Blöcke** geteilt (bzw. `cycleMinutes` aus `system/config`). Beim Eintritt in einen Block wird der Plan einmal berechnet und bleibt für den Block stabil.
- Effektiver Prozentwert je Raum = `openPercent × Zeitplan-Faktor × Außenkurven-Faktor × Kamin-Faktor`.
- **Zeitpläne** (`temperatureSchedules`) liefern einen **Faktor** (`factorPercent`, 100 = unverändert), Mitternacht-übergreifend korrekt ausgewertet (Europe/Berlin).
- **Außenkurve** je Raum (`outdoorFactorPercent`, 6 Stützstellen -5/0/5/10/15/20 °C) bildet die Raumdämmung als Erfahrungswert ab; linear interpoliert, bei fehlender/veralteter Außentemperatur Faktor 1,0.
- **Pumpenschutz**: füllt Lücken mit einem **rotierenden** Schutzventil (`pumpProtectionEligible`), sodass nicht immer derselbe Raum offen bleibt.
- **Mindest-Einschaltzeit** (`valveMinOnSec`) schützt die Stellantriebe vor zu kurzen Takten.

Details/Simulation: `scripts/debug_simulation.py`, Tests: `tests/test_planner.py`.

## Voraussetzungen

- Raspberry Pi OS, Python 3.10+
- Service Account JSON mit Firestore-Zugriff (Projekt `smarthomepi-9b569`)
- **pigpio** (empfohlen, LED Hardware-PWM): Daemon **pigpiod** — siehe Abschnitt *pigpio / pigpiod* unten. Fehlt er, wird standardmäßig **RPi.GPIO Software-PWM** für die LED verwendet.

## Installation auf dem Pi

```bash
cd /home/pi/smarthome/pi-controller
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# Zusätzlich (nur Pi):
pip install RPi.GPIO
```

`requirements.txt` enthält **`pigpio`** (Python-Client). Für Hardware-PWM muss der Daemon **`pigpiod`** laufen — Installation je nach Distribution:

```bash
sudo apt update
sudo apt install pigpio
sudo systemctl enable --now pigpiod
```

### Paket `pigpio` nicht verfügbar („kein Installationskandidat“)

Typische Ursachen: **kein** Raspberry-Pi-OS, veraltete `sources.list`, oder minimales Image ohne Haupt-Repo.

1. **`sudo apt update`** und erneut `sudo apt install pigpio` probieren.
2. Auf **Raspberry Pi OS** prüfen, ob in `/etc/apt/sources.list` bzw. `sources.list.d/` die offiziellen **bookworm/trixie**-Zeilen für `deb …/raspbian` bzw. `deb …/debian` aktiv sind.
3. **Daemon manuell starten** (wenn das Binary woanders her kommt): `sudo pigpiod`
4. **Aus Quellen bauen** (funktioniert auf den meisten ARM-Linux-Systemen mit Build-Tools):

```bash
sudo apt install build-essential unzip wget
cd /tmp
wget -O pigpio.zip https://github.com/joan2937/pigpio/archive/master.zip
unzip pigpio.zip
cd pigpio-master
make
sudo make install
sudo pigpiod
```

Danach sollte `python3 -c "import pigpio; print(pigpio.pi().connected)"` **`True`** ausgeben.

**Kein `pigpiod.service` vom System?** (typisch nach Build aus Quellen) — eigene Unit aus diesem Repo:

```bash
which pigpiod
# Pfad in der Datei prüfen (Standard: /usr/local/bin/pigpiod)

sudo cp deploy/pigpiod.service /etc/systemd/system/
# Falls pigpiod woanders liegt, vorher editieren:
#   sudo nano /etc/systemd/system/pigpiod.service
#   ExecStart= auf den Ausgang von `which pigpiod` setzen, z. B. /usr/bin/pigpiod -f

sudo systemctl daemon-reload
sudo systemctl enable --now pigpiod
sudo systemctl status pigpiod
```

**Hinweis:** Die Unit nutzt **`Type=forking`** und **`pigpiod` ohne `-f`**. Der frühere Vordergrundmodus (`-f` + `Type=simple`) führte auf manchen Systemen zu sofortigem Ende mit Meldung **`sigHandler: Unhandled signal 18`** (SIGCONT von systemd/Cgroup) — dann war der Dienst „inactive (dead)“.

### pigpiod startet nicht: „Can't lock /var/run/pigpio.pid“

Meist läuft noch ein **alter pigpiod** (z. B. von einem früheren manuellen `sudo pigpiod`) oder `/var/run/pigpio.pid` ist **verwaist**.

```bash
sudo systemctl stop pigpiod
sudo pkill -x pigpiod
sudo rm -f /var/run/pigpio.pid
sudo systemctl start pigpiod
sudo systemctl status pigpiod
```

Die mitgelieferte `deploy/pigpiod.service` führt `pkill`/`rm` vor jedem Start automatisch aus (`ExecStartPre`) — nach `daemon-reload` die Unit erneut kopieren oder die Zeilen in `/etc/systemd/system/pigpiod.service` ergänzen.

### Ohne pigpiod

Der Dienst startet trotzdem. Die LED nutzt dann **Software-PWM** über RPi.GPIO (kann flimmern). Abschalten des Fallbacks: `SMH_LED_FALLBACK_RPI_GPIO=false` — dann bleibt die LED aus, bis pigpiod läuft.

Service-Account-Datei (muss vom **selben Linux-Benutzer** lesbar sein, der `python -m smarthome_pi.main` ausführt — z. B. `pi` oder `mathi1651`):

```bash
sudo mkdir -p /etc/smarthome-pi
sudo cp service-account.json /etc/smarthome-pi/
sudo chown "$USER:$USER" /etc/smarthome-pi/service-account.json
sudo chmod 600 /etc/smarthome-pi/service-account.json
export GOOGLE_APPLICATION_CREDENTIALS=/etc/smarthome-pi/service-account.json
```

Falls die Datei **root** gehört (`Permission denied`): `sudo chown mathi1651:mathi1651 /etc/smarthome-pi/service-account.json` (Benutzername anpassen — muss zu `User=` in der systemd-Unit passen).

Alternative ohne `/etc`: JSON nach `~/.config/smarthome-pi/service-account.json` legen, `chmod 600`, und `GOOGLE_APPLICATION_CREDENTIALS` darauf setzen.

Entwicklung ohne Hardware:

```bash
set SMH_MOCK_GPIO=1   # Windows
python -m smarthome_pi.main
```

## systemd

### 1) pigpiod (LED Hardware-PWM)

Siehe Abschnitt *pigpio* oben (`deploy/pigpiod.service`).

### 2) SmartHome-Pi (`smarthome-pi.service`)

Die Unit liegt im Repo unter **`deploy/smarthome-pi.service`**. Solange du sie **nicht** nach `/etc/systemd/system/` kopiert hast, gibt es **`Unit smarthome-pi.service not found`**.

```bash
cd ~/smarthome/pi-controller
sudo cp deploy/smarthome-pi.service /etc/systemd/system/
sudo nano /etc/systemd/system/smarthome-pi.service
```

Anpassen: **`User=`** (z. B. `mathi1651`), **`WorkingDirectory=`**, **`ExecStart=`** (Pfad zu `venv/bin/python`), **`GOOGLE_APPLICATION_CREDENTIALS=`**.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now smarthome-pi
sudo systemctl status smarthome-pi
```

**`pigpiod` ist optional** (`Wants=` in `smarthome-pi.service`): Ohne Daemon nutzt der Code **RPi.GPIO-Software-PWM** für die LED. Mit laufendem pigpiod: stabilere Hardware-PWM.

## Umgebungsvariablen

| Variable | Bedeutung |
|----------|-----------|
| `GOOGLE_APPLICATION_CREDENTIALS` | Pfad zur Service-Account-JSON |
| `SMH_MOCK_GPIO` | `1` = kein echtes GPIO |
| `SMH_TICK_SEC` | Hauptschleife (Standard 1) |
| `SMH_WEATHER_INTERVAL_SEC` | Open-Meteo Abstand (Standard 900) |
| `SMH_VALVE_PINS` | `4,23,27,22,6,13,19,26` (8 BCM, kein Konflikt mit LED) |
| `SMH_OUTLET` | Steckdosen-Relais BCM (Standard 5) |
| `SMH_SZ_LIGHT_RELAY` | Raumlicht-Relais BCM (Standard 16) |
| `SMH_LED_PWM` | LED BCM (Standard **18** = phys. Pin **12**, Hardware-PWM via pigpio) |
| `SMH_LED_PWM_HZ` | PWM-Frequenz LED (Standard 1000) |
| `SMH_PIGPIO_HOST` | optional: pigpiod-Host, leer = localhost |
| `SMH_LED_FALLBACK_RPI_GPIO` | `true`/`false` — wenn pigpiod fehlt: Software-PWM (Standard **true**) |
| `SMH_HANDMODE_FLAG` | Pfad zur Handmodus-Flag-Datei (Standard `/tmp/smarthome_handmode.flag`) |
| `SMH_FIRESTORE_BACKOFF_SEC` | Bei **429 / Quota exceeded**: Wartezeit in Sekunden (Standard **90**, min 30 / max 600) |
| `SMH_SCHED_CLEANUP_INTERVAL_SEC` | Zeitplan-Bereinigung in Firestore, Standard **3600** s |
| `SMH_RELAYS_ACTIVE_LOW` | `true/false` (Standard `true`) |

Zusätzlich per Firestore `system/config`:
- `relaySwitchDelaySec` (default `1.0`) — Mindestabstand zwischen Relaiswechseln
- `maxRelaySwitchesPerTick` (default `1`) — max. Anzahl Relaiswechsel pro Zyklustick

### Wartung: Handmodus (alle 8 Heizkreise dauernd öffnen)

Für Spülung/Wartung: alle **Stellmotoren/FBH-Kreise** bleiben **offen** (Relais ein), unabhängig von der App-Regelung. Firestore/App können weiter genutzt werden (Werte werden gespeichert); die **Ventilsteuerung** ignoriert die Regelung bis zum Ende des Handmodus.

```bash
cd ~/smarthome/pi-controller && source venv/bin/activate
python -m smarthome_pi.handmode activate    # oder: touch /tmp/smarthome_handmode.flag
python -m smarthome_pi.handmode status
python -m smarthome_pi.handmode deactivate   # oder: rm /tmp/smarthome_handmode.flag
```

Der Dienst muss laufen und dieselbe **`SMH_HANDMODE_FLAG`**-Umgebung nutzen (Standardpfad). In `system/state` wird **`handModeActive=true`** geschrieben; alle Ventile stehen auf „offen“.

### Schlafzimmer-Relais am Pi testen (Steckdose + Raumlicht)

Direkt auf die GPIO-Pins (wie `config.py`), **ohne Firestore**. Zuerst Dienst stoppen:

```bash
sudo systemctl stop smarthome-pi
cd ~/smarthome/pi-controller && source venv/bin/activate
python -m smarthome_pi.outlet_test steckdose on
python -m smarthome_pi.outlet_test steckdose off
python -m smarthome_pi.outlet_test licht on
python -m smarthome_pi.outlet_test licht off
python -m smarthome_pi.outlet_test all-off
python -m smarthome_pi.outlet_test status
sudo systemctl start smarthome-pi
```

## Pin-Mapping (Standard)

Siehe `smarthome_pi/config.py` — **Vor Inbetriebnahme mit deiner Verdrahtung abgleichen.**

- Ventile 0–7: BCM 4, **23**, 27, 22, 6, 13, 19, 26
- Steckdose: BCM 5 · Raumlicht: BCM 16
- LED: BCM **18** — bevorzugt **pigpio** (Hardware-PWM); ohne **pigpiod** automatisch **RPi.GPIO Software-PWM** (siehe `SMH_LED_FALLBACK_RPI_GPIO`).

Ohne pigpiod: Relais bleiben aktiv; LED dimmt per Fallback, sofern nicht mit `SMH_LED_FALLBACK_RPI_GPIO=false` unterbunden.

Stellmotoren: Zykluslänge `cycleMinutes` in `system/config` (Firestore), typ. 15 min (96 Blöcke/Tag).

### Firestore: `429 Quota exceeded`

Tritt auf, wenn das **Tages-/Minutenkontingent** (Spark/Blaze) erreicht ist oder durch **zu viele Neustarts** (systemd `RestartSec`) kurzzeitig zu viele Anfragen entstehen.

- In der **Google Cloud Console** → **Firestore** / **APIs & Dienste** → Kontingente und Abrechnung prüfen; ggf. **Blaze** aktivieren oder Limits erhöhen.
- Der Dienst **wartet jetzt bei 429** (`SMH_FIRESTORE_BACKOFF_SEC`, Standard 90 s) und beendet den Prozess nicht mehr sofort — weniger Neustart-Spirale.
- `deploy/smarthome-pi.service` nutzt **`RestartSec=60`**, damit systemd bei echten Fehlern seltener gegen Firestore „hämmert“.

### Firestore Spark (~50k Reads/Tag): `on_snapshot` statt Polling

- Der Pi hält **`on_snapshot`-Listener** auf `rooms`, `system/config`, `devices/led`, `devices/outlets` (`firestore_sync.py`). Reads entstehen nur bei **echten Änderungen**, nicht im festen Takt.
- Ein **Fallback-Poll** (Standard alle 600 s) gleicht verpasste Events ab, falls ein Listener getrennt war.
- Die 1-s-Hauptschleife in `main.py` macht **kein Netzwerk-I/O** mehr, sondern arbeitet auf dem thread-sicheren Cache.
- `system/state` wird **event-getrieben** geschrieben (bei Plan-/Zustandswechsel) plus Heartbeat alle 300 s.

Räume, Zeitpläne, Kamin, LED und Steckdosen kommen über `on_snapshot` praktisch sofort an; der Fallback-Poll ist nur Sicherheitsnetz.
