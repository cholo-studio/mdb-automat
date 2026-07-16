# BEISPIEL-KONFIGURATION — nach config.py kopieren und ausfuellen.
# Die echte config.py enthaelt Secrets und ist in .gitignore ausgeschlossen.

"""
Konfiguration fuer MDB Sielaff SUe2020 Dashboard
=================================================
Passe diese Werte an dein Setup an.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Seriell / MDB Pi Hat Plus ---
SERIAL_PORT = "/dev/ttyAMA0"       # Pi 5: PL011 UART fuer Pi Hat Plus
SERIAL_BAUDRATE = 115200           # Qibixx Standard
SERIAL_TIMEOUT = 0.1               # Sekunden

# Alternativer Port falls USB-Variante:
# SERIAL_PORT = "/dev/ttyUSB0"

# --- MDB Modus ---
# "sniffer"  = passiv mitlesen (empfohlen zum Start)
# "master"   = aktiv Peripherie ansprechen
# "slave"    = als Cashless-Device agieren
MDB_MODE = "sniffer"

# --- Demo-Modus ---
# NUR explizit aktivieren: schreibt SIMULIERTE Verkaeufe/Muenzen in die DB.
# Wird NIE mehr als stiller Fallback bei Serial-Fehlern gesetzt
# (sonst landen Fake-Daten in der Produktions-DB).
DEMO_MODE = False

# --- Webserver ---
WEB_HOST = "0.0.0.0"              # Auf allen Interfaces lauschen (wichtig fuer Tailscale)
WEB_PORT = 5000
SECRET_KEY = ""                  # leer => starker Key wird aus secret.key gelesen/erzeugt

# --- Authentifizierung ---
# Notausstieg bei Aussperrung: im Service MDB_AUTH_ENABLED=false setzen + Neustart.
AUTH_ENABLED = os.environ.get("MDB_AUTH_ENABLED", "true").lower() != "false"
SECRET_KEY_FILE = os.path.join(BASE_DIR, "secret.key")
PASSWORD_FILE = os.path.join(BASE_DIR, "dashboard_password.txt")
DASHBOARD_PASSWORD = os.environ.get("MDB_DASHBOARD_PASSWORD", "")  # leer => aus Datei
SESSION_HOURS = 168              # Login gilt 7 Tage
INGEST_KEY_FILE = os.path.join(BASE_DIR, "ingest.key")
INGEST_KEY = ""                  # leer => aus Datei lesen/generieren

# --- Datenbank ---
DB_PATH = "mdb_data.db"

# --- Logging ---
LOG_FILE = "mdb_sniffer.log"
LOG_LEVEL = "INFO"                 # DEBUG fuer volle MDB-Rohdaten
RAW_LOG_FILE = "mdb_raw.log"      # Rohe MDB-Bytes zur Analyse
# Rotation: verhindert unbegrenztes Wachstum (frueher 23 GB Logs).
LOG_MAX_BYTES = 10 * 1024 * 1024   # 10 MB pro Logdatei
LOG_BACKUP_COUNT = 3               # 3 Archive => max ~40 MB Haupt-Log
RAW_LOG_ENABLED = True             # Roh-MDB-Log fuer spaeteres Reverse-Engineering (Muenz/Schein-Decoding)
RAW_LOG_MAX_BYTES = 20 * 1024 * 1024  # 20 MB pro Roh-Log
RAW_LOG_BACKUP_COUNT = 2           # => max ~60 MB rollierendes Roh-Fenster

# --- Sielaff SUe2020 spezifisch ---
AUTOMAT_NAME = "Sielaff SUe2020"
AUTOMAT_STANDORT = "Mein Standort"

# --- CEO Agent ---
AGENT_ENABLED = True
AGENT_CHECK_INTERVAL = 300          # Alle 5 Min Anomalie-Check (Sekunden)

# --- Temperatur-Ueberwachung (Pi 5 drosselt weich ab ~80 C, hart ab 85 C) ---
TEMP_CHECK_ENABLED = True
TEMP_ALERT_C = 80        # Telegram-Alarm ab dieser SoC-Temperatur
TEMP_RECOVER_C = 72      # Entwarnung wenn wieder darunter (Hysterese gegen Spam)

# --- n8n Webhooks ---
N8N_WEBHOOK_URL = "http://localhost:5678/webhook/sielaff"
N8N_ENABLED = False                   # Telegram ersetzt n8n — bei Bedarf aktivieren

# --- Telegram ---
TELEGRAM_BOT_TOKEN = "DEIN_TELEGRAM_BOT_TOKEN"   # von @BotFather
TELEGRAM_CHAT_ID = "DEINE_CHAT_ID"
TELEGRAM_ENABLED = True

# --- Wetter (OpenWeatherMap) ---
WEATHER_API_KEY = "DEIN_OPENWEATHERMAP_KEY"
WEATHER_CITY = "Meine Stadt"             # Stadt fuer Wetterdaten
WEATHER_ENABLED = True
WEATHER_FETCH_INTERVAL = 7200         # Alle 2 Stunden neue Daten holen (Sekunden)

# --- LLM (Ollama auf Windows-PC mit 3080) ---
OLLAMA_HOST = "http://localhost:11434"   # optional: lokales/entferntes Ollama
OLLAMA_MODEL = "mistral"
LLM_ENABLED = True
LLM_TIMEOUT = 30                    # Sekunden

# --- Schwellwerte ---
ALERT_NO_SALES_AFTER_HOUR = 10     # Alarm wenn nach 10:00 noch kein Verkauf
ALERT_COIN_LOW_PERCENT = 10        # Muenzwechsler fast leer
ALERT_COIN_HIGH_PERCENT = 90       # Muenzwechsler fast voll
ALERT_ERROR_CRITICAL = ["NAK", "ERROR"]  # Kritische MDB-Fehler
ALERT_REVENUE_DEVIATION = 30       # Prozent Abweichung vom 7-Tage-Schnitt
ALERT_STOCK_LOW_THRESHOLD = 3      # Warnung wenn Fuellstand <= 3
ALERT_STOCK_EMPTY_THRESHOLD = 0    # Kritisch wenn Fuellstand = 0

# --- Reports ---
DAILY_REPORT_HOUR = 20             # Tagesbericht um 20:00 Uhr
WEEKLY_REPORT_DAY = 0              # Montag (0=Mo, 6=So)
WEEKLY_REPORT_HOUR = 9             # Wochenbericht um 09:00

# --- MDB Adressen (Standard) ---
MDB_ADDRESSES = {
    0x08: "Muenzwechsler",
    0x10: "Cashless Device 1",
    0x18: "Kommunikationsgateway",
    0x30: "Geldscheinleser",
    0x40: "Universal Satellite Device",
    0x48: "Cashless Device 2",
    0x58: "Age Verification",
    0x60: "Cashless Device 3",
}
