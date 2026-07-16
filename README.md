<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/cholo-studio-logo-white.png">
  <img src="assets/cholo-studio-logo.png" alt="CHOLO STUDIO" width="300">
</picture>

<br><br>

# AutomatIQ

**Der smarte Betriebsagent für Verkaufsautomaten**

Dekodiert Verkäufe, überwacht Umsatz & Technik, meldet per Telegram — passiv am MDB-Bus.

[![License: MIT](https://img.shields.io/badge/License-MIT-000000.svg)](LICENSE)
&nbsp;[![Python 3](https://img.shields.io/badge/Python-3-000000.svg?logo=python&logoColor=white)](https://www.python.org/)
&nbsp;[![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi-4%20%2F%205-000000.svg?logo=raspberrypi&logoColor=white)](https://www.raspberrypi.com/)
&nbsp;[![by CHOLO STUDIO](https://img.shields.io/badge/by-CHOLO%20STUDIO-000000.svg)](https://github.com/cholo-studio)

</div>

---

Liest **passiv** den MDB-Bus eines Sielaff-SUe2020-Verkaufsautomaten mit (über das
[Qibixx MDB Pi Hat Plus](https://www.qibixx.com/)), dekodiert Verkäufe/Münzen/Scheine/Fehler
und zeigt Umsatz- und Betriebs-Kennzahlen in einem **login-geschützten Web-Dashboard**.
Läuft auf einem Raspberry Pi und meldet Anomalien direkt per **Telegram**.

> **Passiv** heißt: der Automat wird nur mitgelesen, nicht gesteuert — kein Eingriff in den Verkaufsbetrieb.

## Features

- **MDB-Sniffer** — dekodiert den Bus-Verkehr (Verkäufe, Münzwechsler, Scheinleser, Cashless, Fehler) und speichert alles in SQLite.
- **Web-Dashboard** (Flask) — Live-Umsatz, Verkaufs-Historie, Produkt-Ranking, Tages-/Monatsauswertung, CSV-Export. **Passwort-Login** (Session-Cookie), zusätzlich abschottbar über Tailscale.
- **CEO-Agent** — regelbasierte Überwachung (Anomalien, Füllstände, Fehler) mit optionaler LLM-Analyse (Ollama). Tages-/Wochenberichte automatisch.
- **Telegram-Alarme** — Verkäufe, Störungen, „Automat verkauft nichts", Pi-Temperatur, Berichte.
- **System-Monitoring** — CPU-Last, SoC-Temperatur, Drossel-Status, RAM, Disk (`/api/system` + Dashboard-Kachel).
- **Produkt- & Bestandsverwaltung**, Wetter-Korrelation, Werbe-Display-Steuerung.
- **Log-Rotation** und nächtliches DB-Backup out of the box.

## Hardware

| Teil | Empfehlung |
|------|------------|
| Rechner | Raspberry Pi 4 oder 5 (40-Pin-Header) — **aktive Kühlung empfohlen** |
| MDB-Interface | Qibixx MDB Pi Hat Plus |
| Automat | Sielaff SUe2020 (bzw. MDB-fähiger Automat) |

Das Pi Hat Plus versorgt den Pi i. d. R. direkt über den MDB-Bus mit Strom (5 V/3 A).

## Stack

Python 3 · Flask · SQLite (WAL) · pyserial · systemd · Tailscale (empfohlen für Fernzugriff)

## Setup (Kurz)

```bash
git clone https://github.com/cholo-studio/mdb-automat.git
cd mdb-automat
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

# Konfiguration anlegen (Secrets NICHT ins Repo!)
cp config.example.py config.py
# config.py ausfüllen: Telegram-Token, Wetter-Key, Standort …

./venv/bin/python web_server.py    # Dashboard auf http://localhost:5000
```

Für den Dauerbetrieb als `systemd`-Dienst einrichten (siehe `setup.sh`).
Beim ersten Start werden `secret.key` und ein zufälliges Dashboard-Passwort
(`dashboard_password.txt`) automatisch erzeugt (`chmod 600`).

## Sicherheit

- **Secrets** (Telegram-Token, API-Keys, Passwort) gehören in `config.py` bzw. Umgebungsvariablen — **niemals** ins Repo. `config.py`, `*.key` und `dashboard_password.txt` sind in `.gitignore`.
- Das Dashboard ist per Login geschützt (`AUTH_ENABLED`). Für Fernzugriff **Tailscale** statt offener Port-Weiterleitung.
- Notausstieg bei Aussperrung: Dienst mit `MDB_AUTH_ENABLED=false` starten.

## Architektur (Module)

| Modul | Aufgabe |
|-------|---------|
| `mdb_sniffer.py` / `mdb_protocol.py` | Serielles Mitlesen & MDB-Dekodierung |
| `web_server.py` / `auth.py` | Flask-Dashboard + Login |
| `agent.py` / `rules.py` / `llm.py` | CEO-Agent (Regeln + LLM) |
| `notifier.py` / `telegram_bot.py` | Telegram & Webhooks |
| `analytics.py` / `reporter.py` | Auswertungen & Berichte |
| `product_manager.py` / `stock_manager.py` | Produkte & Bestand |
| `weather_manager.py` / `ad_manager.py` | Wetter-Korrelation & Werbe-Display |

## Status

Aktiver Eigenbetrieb an einem realen Automaten. Die Zahlungsart-Aufschlüsselung
(Münze/Schein/Cashless) ist Work-in-Progress — Verkäufe & Umsatz werden zuverlässig erfasst.

---

## Von CHOLO STUDIO

**CHOLO STUDIO** baut pragmatische Software für Gastronomie und Kleinbetrieb —
von Automaten-Telemetrie über digitale Speisekarten bis zur Buchhaltung.
Dieses Projekt ist Teil dieses Ökosystems: eigene Werkzeuge für echte Betriebe,
sauber gebaut und offen geteilt.

→ **[github.com/cholo-studio](https://github.com/cholo-studio)**

## Lizenz

[MIT](LICENSE) — frei nutz-, änder- und teilbar. © 2026 CHOLO STUDIO

<div align="center">
<br>
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/cholo-studio-logo-white.png">
  <img src="assets/cholo-studio-logo.png" alt="CHOLO STUDIO" width="150">
</picture>
<br><br>
<sub>Gebaut von <a href="https://github.com/cholo-studio"><b>CHOLO STUDIO</b></a></sub>
</div>
