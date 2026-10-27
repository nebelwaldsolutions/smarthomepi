# GPIO-Belegung (Raspberry Pi 3B+, BCM + phys. Pin)

Alle Relais-Module sind **active-low** (`relays_active_low: true`): GPIO **LOW** = Relais **ein** (Stellmotor/Stromkreis an), GPIO **HIGH** = aus.

**LED-Dimmer:** Zuerst **pigpio** (Hardware-PWM auf BCM 18). Ohne laufenden **pigpiod** nutzt der Dienst standardmäßig **RPi.GPIO Software-PWM** auf demselben Pin (`SMH_LED_FALLBACK_RPI_GPIO=true`).

## Übersicht (Standard `config.py`)

| Funktion | BCM | Phys. Pin (40-pol.) | Firestore / App |
|----------|-----|---------------------|-----------------|
| LED / MOSFet (PWM, dimmbar) | **18** | **12** | `devices/led` |
| FBH Ventil Kreis 0 | 4 | 7 | `rooms[].valveIndex` 0 |
| FBH Ventil Kreis 1 | **23** | **16** | `valveIndex` 1 |
| FBH Ventil Kreis 2 | 27 | 13 | 2 |
| FBH Ventil Kreis 3 | 22 | 15 | 3 |
| FBH Ventil Kreis 4 | 6 | 31 | 4 |
| FBH Ventil Kreis 5 | 13 | 33 | 5 |
| FBH Ventil Kreis 6 | 19 | 35 | 6 |
| FBH Ventil Kreis 7 | 26 | 37 | 7 |
| Relais Steckdose Schlafzimmer | 5 | 29 | `devices/outlets.schlafzimmer` |
| Relais Raumlicht Schlafzimmer | 16 | 36 | `devices/outlets.schlafzimmerLight` |

**Stromversorgung Relais-/MOSFet-Platinen:** je **5 V** (z. B. phys. Pin **2** oder **4**) und **GND** (z. B. **6**, **9**, **14** …). Mehrere Boards am selben 5 V/GND sind üblich; Leitung und Pi-USB-Netzteil müssen die **Gesamtlast** (Relais-Spulen) verkraften.

## Umgebungsvariablen (optional)

| Variable | Standard | Bedeutung |
|----------|----------|-----------|
| `SMH_LED_PWM` | 18 | BCM-Pin LED (Hardware-PWM mit pigpio) |
| `SMH_LED_PWM_HZ` | 1000 | PWM-Frequenz LED |
| `SMH_PIGPIO_HOST` | (leer) | Hostname/IP des pigpiod, leer = localhost |
| `SMH_OUTLET` | 5 | Steckdose Schlafzimmer |
| `SMH_SZ_LIGHT_RELAY` | 16 | Raumlicht Schlafzimmer |
| `SMH_VALVE_PINS` | `4,23,…` | 8 Ventil-BCMs, kommagetrennt |
| `SMH_LED_FALLBACK_RPI_GPIO` | true | bei fehlendem pigpiod: RPi.GPIO-PWM |

## Jalousien (Shelly)

Nicht am Pi; pro Raum `blindsShellyHost` in App/Firestore.
