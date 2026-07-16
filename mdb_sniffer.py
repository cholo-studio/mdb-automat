"""
MDB Bus Sniffer fuer Qibixx Pi Hat Plus + Sielaff SUe2020
==========================================================
Liest den MDB-Bus ueber die serielle Schnittstelle des Pi Hat Plus,
decodiert die Nachrichten und speichert alles in SQLite.

Laeuft als Hintergrund-Thread parallel zum Webserver.
"""

import logging
from logging.handlers import RotatingFileHandler
import sqlite3
import threading
import time
import json
from collections import deque
from typing import Optional

try:
    import serial
except ImportError:
    serial = None
    print("WARNUNG: pyserial nicht installiert. Starte im Demo-Modus.")

from config import (
    SERIAL_PORT, SERIAL_BAUDRATE, SERIAL_TIMEOUT,
    MDB_MODE, DB_PATH, LOG_FILE, LOG_LEVEL, RAW_LOG_FILE,
    LOG_MAX_BYTES, LOG_BACKUP_COUNT,
    RAW_LOG_ENABLED, RAW_LOG_MAX_BYTES, RAW_LOG_BACKUP_COUNT,
    DEMO_MODE,
)
from mdb_protocol import MDBParser, MDBMessage, VendEvent, CoinEvent
from product_manager import ProductManager

# Logging einrichten — rotierend, damit Logs nicht unbegrenzt wachsen (frueher 23 GB).
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES,
                            backupCount=LOG_BACKUP_COUNT),
        logging.StreamHandler(),
    ]
)
# Rausch-Bibliotheken daempfen: verhindert Journal-Flut UND das Leaken des
# Telegram-Tokens durch httpx-INFO (loggt die volle getUpdates-URL inkl. Token).
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("mdb_sniffer")


# ============================================================
# SQLite Datenbank
# ============================================================
def init_db(db_path: str = DB_PATH):
    """Erstellt die Datenbank-Tabellen falls sie nicht existieren."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS mdb_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            direction TEXT,
            device TEXT,
            device_addr TEXT,
            command TEXT,
            raw_hex TEXT,
            data_json TEXT,
            description TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS vend_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            product_id INTEGER,
            price_cents INTEGER,
            payment_method TEXT,
            success INTEGER DEFAULT 1
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS coin_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            coin_type INTEGER,
            value_euro REAL,
            routing TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY,
            total_sales INTEGER DEFAULT 0,
            total_revenue_cents INTEGER DEFAULT 0,
            total_coins INTEGER DEFAULT 0,
            total_bills INTEGER DEFAULT 0,
            total_cashless INTEGER DEFAULT 0,
            errors INTEGER DEFAULT 0
        )
    """)

    # Index fuer schnelle Zeitabfragen
    c.execute("CREATE INDEX IF NOT EXISTS idx_msg_time ON mdb_messages(timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_vend_time ON vend_events(timestamp)")

    conn.commit()
    conn.close()
    logger.info("Datenbank initialisiert: %s", db_path)


# ============================================================
# MDB Sniffer Klasse
# ============================================================
class MDBSniffer:
    """Hauptklasse fuer den MDB-Bus-Sniffer."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.parser = MDBParser()
        self.serial_port: Optional[serial.Serial] = None
        self.running = False
        self.thread: Optional[threading.Thread] = None
        # Demo-Modus NUR explizit per Config. Nie automatisch bei fehlendem
        # pyserial oder Serial-Fehler (sonst Fake-Daten in der Produktions-DB).
        self.demo_mode = bool(DEMO_MODE)

        # Live-Daten fuer Webserver (Thread-sicher)
        self.recent_messages: deque = deque(maxlen=200)
        self.recent_vends: deque = deque(maxlen=50)
        self.recent_coins: deque = deque(maxlen=100)
        self.stats_lock = threading.Lock()
        self.stats = {
            "total_messages": 0,
            "total_messages_stored": 0,
            "total_vends": 0,
            "total_revenue_cents": 0,
            "total_coins_inserted": 0,
            "errors": 0,
            "last_activity": None,
            "uptime_start": time.time(),
            "connection_status": "Getrennt",
        }

        # Produkt-Manager
        self.products = ProductManager(db_path)

        # Fuellstand-Manager
        from stock_manager import StockManager
        self.stock = StockManager(db_path)

        # Event-Callbacks (fuer CEO-Agent)
        self.on_vend_callbacks: list = []
        self.on_coin_callbacks: list = []
        self.on_error_callbacks: list = []

        # Raw-Log fuer Debugging — rotierend, und NICHT an den Root-Logger
        # propagieren (sonst landet jede Roh-Zeile zusaetzlich in mdb_sniffer.log
        # => war die Ursache der Log-Verdreifachung / 23 GB).
        self.raw_logger = logging.getLogger("mdb_raw")
        self.raw_logger.propagate = False
        self.raw_logger.handlers.clear()
        if RAW_LOG_ENABLED:
            raw_handler = RotatingFileHandler(
                RAW_LOG_FILE, maxBytes=RAW_LOG_MAX_BYTES,
                backupCount=RAW_LOG_BACKUP_COUNT)
            raw_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self.raw_logger.addHandler(raw_handler)
            self.raw_logger.setLevel(logging.DEBUG)
        else:
            self.raw_logger.addHandler(logging.NullHandler())
            self.raw_logger.setLevel(logging.CRITICAL)

        # DB-Cleanup Timer
        self._last_cleanup = time.time()
        self._cleanup_interval = 3600 * 6  # Alle 6 Stunden

        init_db(db_path)
        self._load_stats_from_db()

    def _load_stats_from_db(self):
        """Laedt heutige Verkaufsstatistiken aus der DB beim Start."""
        try:
            conn = sqlite3.connect(self.db_path)
            today = time.strftime("%Y-%m-%d")

            # Heutige Verkaeufe zaehlen
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(price_cents), 0) "
                "FROM vend_events WHERE success = 1 AND "
                "date(timestamp, 'unixepoch', 'localtime') = ?",
                (today,)
            ).fetchone()

            if row:
                with self.stats_lock:
                    self.stats["total_vends"] = row[0]
                    self.stats["total_revenue_cents"] = row[1]

            # Heutige Muenzen zaehlen
            coin_count = conn.execute(
                "SELECT COUNT(*) FROM coin_events WHERE "
                "date(timestamp, 'unixepoch', 'localtime') = ?",
                (today,)
            ).fetchone()

            if coin_count and coin_count[0]:
                with self.stats_lock:
                    self.stats["total_coins_inserted"] = coin_count[0]

            # Letzte Verkaeufe in Live-Feed laden
            vends = conn.execute(
                "SELECT timestamp, product_id, price_cents, payment_method, success "
                "FROM vend_events ORDER BY timestamp DESC LIMIT 50"
            ).fetchall()
            for v in reversed(vends):
                from mdb_protocol import VendEvent
                vend = VendEvent(
                    timestamp=v[0], product_id=v[1],
                    price_cents=v[2], payment_method=v[3],
                    success=bool(v[4])
                )
                vend_dict = vend.to_dict()
                self.products.enrich_vend(vend_dict)
                self.recent_vends.appendleft(vend_dict)

            conn.close()
            logger.info("Stats aus DB geladen: %d Verkaeufe, %.2f EUR heute",
                        self.stats["total_vends"],
                        self.stats["total_revenue_cents"] / 100)
        except Exception as e:
            logger.error("Stats-Laden Fehler: %s", e)

    def connect(self) -> bool:
        """Verbindung zur seriellen Schnittstelle herstellen."""
        if self.demo_mode:
            logger.info("Demo-Modus aktiv (kein pyserial)")
            with self.stats_lock:
                self.stats["connection_status"] = "Demo-Modus"
            return True

        try:
            # Pruefen ob Port existiert
            import os
            if not os.path.exists(SERIAL_PORT):
                logger.warning("Port %s nicht gefunden - warte auf Reconnect (KEIN Demo)", SERIAL_PORT)
                with self.stats_lock:
                    self.stats["connection_status"] = "Fehler: Port nicht gefunden"
                return False

            self.serial_port = serial.Serial(
                port=SERIAL_PORT,
                baudrate=SERIAL_BAUDRATE,
                timeout=SERIAL_TIMEOUT,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
            )

            # Qibixx Sniffer-Modus aktivieren
            time.sleep(0.5)

            # Firmware-Version abfragen
            self.serial_port.write(b"V\r")
            time.sleep(0.3)
            version = self.serial_port.readline().decode("ascii", errors="replace").strip()
            if version:
                logger.info("Pi Hat Plus Firmware: %s", version)

            if MDB_MODE == "sniffer":
                # Qibixx API-Sniff: X,1 aktiviert Sniffer-Modus
                self.serial_port.write(b"X,1\r")
                logger.info("Sniffer-Modus: X,1 gesendet")
            elif MDB_MODE == "master":
                self.serial_port.write(b"M\r")
                logger.info("Master-Modus aktiviert")

            time.sleep(0.3)
            response = self.serial_port.readline().decode("ascii", errors="replace").strip()
            if response:
                logger.info("Pi Hat Plus Antwort: %s", response)
                if "ACK" in response.upper():
                    logger.info("Sniffer-Modus erfolgreich aktiviert")
                else:
                    logger.warning("Unerwartete Antwort: %s", response)

            with self.stats_lock:
                self.stats["connection_status"] = "Verbunden"

            logger.info("Seriell verbunden: %s @ %d baud", SERIAL_PORT, SERIAL_BAUDRATE)
            return True

        except Exception as e:
            logger.warning("Seriell-Verbindungsfehler: %s - warte auf Reconnect (KEIN Demo)", e)
            try:
                if self.serial_port:
                    self.serial_port.close()
            except Exception:
                pass
            self.serial_port = None
            with self.stats_lock:
                self.stats["connection_status"] = "Fehler: Verbindung getrennt"
            return False

    def start(self):
        """Startet den Sniffer in einem Hintergrund-Thread."""
        if self.running:
            return

        self.running = True
        if self.demo_mode:
            self.thread = threading.Thread(target=self._demo_loop, daemon=True)
        else:
            self.thread = threading.Thread(target=self._sniffer_loop, daemon=True)
        self.thread.start()
        logger.info("Sniffer gestartet")

    def stop(self):
        """Stoppt den Sniffer."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=3)
        if self.serial_port and self.serial_port.is_open:
            try:
                # Qibixx Sniffer-Modus deaktivieren
                self.serial_port.write(b"X,0\r")
                time.sleep(0.2)
                logger.info("Sniffer-Modus deaktiviert (X,0)")
            except Exception:
                pass
            self.serial_port.close()
        with self.stats_lock:
            self.stats["connection_status"] = "Gestoppt"
        logger.info("Sniffer gestoppt")

    def _cleanup_old_data(self):
        """Loescht MDB-Nachrichten aelter als 7 Tage aus der DB."""
        cutoff = time.time() - (7 * 86400)
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute(
                "DELETE FROM mdb_messages WHERE timestamp < ?", (cutoff,)
            )
            deleted = cursor.rowcount
            conn.commit()
            conn.close()
            if deleted > 0:
                logger.info("DB-Cleanup: %d alte Nachrichten geloescht", deleted)
        except Exception as e:
            logger.error("DB-Cleanup Fehler: %s", e)

    def _sniffer_loop(self):
        """Hauptschleife: Liest serielle Daten und parst sie."""
        logger.info("Sniffer-Loop gestartet auf %s", SERIAL_PORT)

        while self.running:
            try:
                # Periodischer DB-Cleanup
                now = time.time()
                if now - self._last_cleanup > self._cleanup_interval:
                    self._last_cleanup = now
                    self._cleanup_old_data()

                if not self.serial_port or not self.serial_port.is_open:
                    logger.warning("Serielle Verbindung verloren, versuche Reconnect...")
                    time.sleep(5)
                    self.connect()
                    continue

                line = self.serial_port.readline().decode("ascii", errors="replace").strip()
                if not line:
                    continue

                # Rohes Logging
                self.raw_logger.debug(line)

                # Parsen
                msg = self.parser.parse_sniffer_line(line)
                if msg:
                    self._process_message(msg)

            except serial.SerialException as e:
                logger.error("Serieller Fehler: %s", e)
                with self.stats_lock:
                    self.stats["errors"] += 1
                    self.stats["connection_status"] = f"Fehler: {e}"
                time.sleep(2)

            except Exception as e:
                logger.error("Unerwarteter Fehler: %s", e)
                with self.stats_lock:
                    self.stats["errors"] += 1
                time.sleep(0.1)

    def _demo_loop(self):
        """Demo-Modus: Generiert simulierte MDB-Daten."""
        import random
        logger.info("Demo-Loop gestartet (simulierte Daten)")

        products = [
            (1, 120, "Snickers"), (2, 150, "Mars"), (3, 100, "Twix"),
            (4, 130, "KitKat"), (5, 80, "Haribo"), (6, 200, "Red Bull"),
            (7, 110, "Bounty"), (8, 90, "M&Ms"), (9, 160, "Chips"),
        ]

        while self.running:
            time.sleep(random.uniform(2, 8))
            if not self.running:
                break

            now = time.time()
            event_type = random.choice(["poll", "poll", "poll", "coin", "vend", "poll", "coin"])

            if event_type == "poll":
                # Simulierter POLL
                device = random.choice(["Muenzwechsler", "Cashless 1", "Geldscheinleser"])
                msg = MDBMessage(
                    timestamp=now, direction="VMC->PER", device=device,
                    device_addr=0x08, command="POLL", raw_bytes=b"\x0b",
                    description=f"{device} pollen"
                )
                self._process_message(msg)

            elif event_type == "coin":
                # Simulierter Muenzeinwurf
                coin_type = random.choice([4, 5, 6, 7, 8])  # 10ct - 2EUR
                from mdb_protocol import EURO_COIN_VALUES
                value = EURO_COIN_VALUES.get(coin_type, 0)
                coin_event = CoinEvent(
                    timestamp=now, coin_type=coin_type,
                    value_euro=value, routing="tubes"
                )
                self._process_coin(coin_event)

                msg = MDBMessage(
                    timestamp=now, direction="PER->VMC", device="Muenzwechsler",
                    device_addr=0x08, command="COIN_IN",
                    raw_bytes=bytes([coin_type]),
                    data={"value_euro": f"{value:.2f}", "coin_type": coin_type},
                    description=f"Muenze eingeworfen: {value:.2f} EUR"
                )
                self._process_message(msg)

            elif event_type == "vend":
                # Simulierter Verkauf
                product = random.choice(products)
                payment = random.choice(["coin", "coin", "cashless", "bill"])
                vend = VendEvent(
                    timestamp=now, product_id=product[0],
                    price_cents=product[1], payment_method=payment,
                    success=random.random() > 0.05
                )
                self._process_vend(vend)

                msg = MDBMessage(
                    timestamp=now, direction="VMC->PER",
                    device="Cashless 1" if payment == "cashless" else "Muenzwechsler",
                    device_addr=0x10 if payment == "cashless" else 0x08,
                    command="VEND_REQUEST" if vend.success else "VEND_FAILURE",
                    raw_bytes=b"\x13\x00",
                    data={
                        "product_id": product[0],
                        "product_name": product[2],
                        "price_euro": f"{product[1]/100:.2f}",
                        "payment": payment,
                    },
                    description=f"{'Verkauf' if vend.success else 'FEHLGESCHLAGEN'}: "
                                f"{product[2]} ({product[1]/100:.2f} EUR, {payment})"
                )
                self._process_message(msg)

    # Befehle die NICHT in die DB gespeichert werden (Routine-Polling)
    _SKIP_DB_COMMANDS = frozenset({
        "POLL", "ACK", "RESET",
        "CMD_0x6A", "CMD_0xE2",  # Sielaff-proprietaer, Routine-Polls
    })

    def _should_store_in_db(self, msg: MDBMessage) -> bool:
        """Entscheidet ob eine Nachricht in die DB gespeichert wird.
        POLL/ACK/RESET sind Routine (~90% Traffic) und werden uebersprungen.
        Alles andere (Verkaeufe, Muenzen, Fehler, Konfig, Daten) wird gespeichert.
        """
        if msg.command in self._SKIP_DB_COMMANDS:
            return False
        return True

    def _process_message(self, msg: MDBMessage):
        """Verarbeitet eine geparste MDB-Nachricht."""
        msg_dict = msg.to_dict()
        self.recent_messages.appendleft(msg_dict)

        with self.stats_lock:
            self.stats["total_messages"] += 1
            self.stats["last_activity"] = time.strftime("%H:%M:%S")

        # Nur relevante Nachrichten in DB speichern (kein POLL/ACK Spam)
        if self._should_store_in_db(msg):
            with self.stats_lock:
                self.stats["total_messages_stored"] += 1
            try:
                conn = sqlite3.connect(self.db_path)
                conn.execute(
                    "INSERT INTO mdb_messages (timestamp, direction, device, device_addr, "
                    "command, raw_hex, data_json, description) VALUES (?,?,?,?,?,?,?,?)",
                    (msg.timestamp, msg.direction, msg.device, f"0x{msg.device_addr:02X}",
                     msg.command, msg.raw_bytes.hex(" "), json.dumps(msg.data), msg.description)
                )
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error("DB-Fehler: %s", e)

        # Error-Callbacks bei NAK/ERROR
        if msg.command in ("NAK", "ERROR"):
            for cb in self.on_error_callbacks:
                try:
                    cb(msg_dict)
                except Exception as e:
                    logger.error("Error-Callback Fehler: %s", e)

        # Verkauf erkennen
        # VEND_REQUEST = Cashless-Zahlung (Karte/App)
        # CASH_SALE = Barzahlung (Muenzen/Scheine), wird aber auch ueber Cashless-Adresse gemeldet
        if msg.command in ("VEND_REQUEST", "CASH_SALE") and msg.data.get("price_cents"):
            if msg.command == "CASH_SALE":
                pay_method = "coin"
            else:
                pay_method = "cashless"
            vend = VendEvent(
                timestamp=msg.timestamp,
                product_id=msg.data.get("product_id", 0),
                price_cents=msg.data["price_cents"],
                payment_method=pay_method,
                success=True,
            )
            self._process_vend(vend)

    def _process_vend(self, vend: VendEvent):
        """Verarbeitet einen Verkaufsvorgang."""
        vend_dict = vend.to_dict()
        # Produktname anreichern
        self.products.enrich_vend(vend_dict)
        self.recent_vends.appendleft(vend_dict)

        with self.stats_lock:
            self.stats["total_vends"] += 1
            if vend.success:
                self.stats["total_revenue_cents"] += vend.price_cents

        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT INTO vend_events (timestamp, product_id, price_cents, "
                "payment_method, success) VALUES (?,?,?,?,?)",
                (vend.timestamp, vend.product_id, vend.price_cents,
                 vend.payment_method, int(vend.success))
            )

            # Tagesstatistik aktualisieren
            date_str = time.strftime("%Y-%m-%d", time.localtime(vend.timestamp))
            conn.execute("""
                INSERT INTO daily_stats (date, total_sales, total_revenue_cents)
                VALUES (?, 1, ?)
                ON CONFLICT(date) DO UPDATE SET
                    total_sales = total_sales + 1,
                    total_revenue_cents = total_revenue_cents + ?
            """, (date_str, vend.price_cents, vend.price_cents))

            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("DB-Fehler (Vend): %s", e)

        # Fuellstand reduzieren
        if vend.success:
            remaining = self.stock.decrement_stock(vend.product_id)
            if remaining is not None:
                vend_dict["stock_remaining"] = remaining

        pname = vend_dict.get("product_name", f"Slot #{vend.product_id}")
        logger.info("VERKAUF: %s (Slot %d), Preis %.2f EUR, Zahlung: %s",
                     pname, vend.product_id, vend.price_euro, vend.payment_method)

        # Agent-Callbacks benachrichtigen (mit Produktname)
        for cb in self.on_vend_callbacks:
            try:
                cb(vend_dict)
            except Exception as e:
                logger.error("Vend-Callback Fehler: %s", e)

    def _process_coin(self, coin: CoinEvent):
        """Verarbeitet einen Muenzeinwurf."""
        self.recent_coins.appendleft(coin.to_dict())
        with self.stats_lock:
            self.stats["total_coins_inserted"] += 1

        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT INTO coin_events (timestamp, coin_type, value_euro, routing) "
                "VALUES (?,?,?,?)",
                (coin.timestamp, coin.coin_type, coin.value_euro, coin.routing)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("DB-Fehler (Coin): %s", e)

        # Agent-Callbacks benachrichtigen
        for cb in self.on_coin_callbacks:
            try:
                cb(coin.to_dict())
            except Exception as e:
                logger.error("Coin-Callback Fehler: %s", e)

    # ============================================================
    # API fuer Webserver
    # ============================================================
    def get_stats(self) -> dict:
        """Gibt aktuelle Statistiken zurueck."""
        with self.stats_lock:
            stats = dict(self.stats)
        stats["total_revenue_euro"] = f"{stats['total_revenue_cents'] / 100:.2f}"
        stats["uptime_seconds"] = int(time.time() - stats["uptime_start"])
        stats["uptime_str"] = self._format_uptime(stats["uptime_seconds"])
        return stats

    def get_recent_messages(self, limit: int = 50) -> list:
        """Gibt die letzten MDB-Nachrichten zurueck."""
        return list(self.recent_messages)[:limit]

    def get_recent_vends(self, limit: int = 20) -> list:
        """Gibt die letzten Verkaeufe zurueck."""
        return list(self.recent_vends)[:limit]

    def get_daily_stats(self, days: int = 30) -> list:
        """Gibt Tagesstatistiken aus der DB zurueck."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute(
                "SELECT date, total_sales, total_revenue_cents FROM daily_stats "
                "ORDER BY date DESC LIMIT ?", (days,)
            )
            rows = cursor.fetchall()
            conn.close()
            return [
                {"date": r[0], "sales": r[1], "revenue_euro": f"{r[2]/100:.2f}"}
                for r in rows
            ]
        except Exception:
            return []

    def get_product_stats(self) -> list:
        """Gibt Verkaufsstatistiken pro Produkt zurueck (mit Produktnamen)."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute("""
                SELECT product_id, COUNT(*) as count, SUM(price_cents) as total
                FROM vend_events WHERE success = 1
                GROUP BY product_id ORDER BY count DESC
            """)
            rows = cursor.fetchall()
            conn.close()
            stats = [
                {"product_id": r[0], "count": r[1], "total_euro": f"{r[2]/100:.2f}"}
                for r in rows
            ]
            return self.products.enrich_product_stats(stats)
        except Exception:
            return []

    @staticmethod
    def _format_uptime(seconds: int) -> str:
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        return f"{hours}h {minutes}m"
