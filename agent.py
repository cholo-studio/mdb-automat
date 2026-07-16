"""
CEO-Agent fuer Sielaff SUe2020
================================
Der "Geschaeftsfuehrer" des Automaten.
Ueberwacht Verkaeufe, erkennt Fehler, meldet Anomalien,
erstellt Reports — alles automatisch.

Hybrid: Regeln (sofort) + LLM (Analyse/Reports)
"""

import logging
import sqlite3
import threading
import time
from collections import deque
from typing import Optional

from config import (
    AGENT_ENABLED, AGENT_CHECK_INTERVAL, DB_PATH,
    N8N_WEBHOOK_URL, N8N_ENABLED,
    ALERT_STOCK_LOW_THRESHOLD, ALERT_STOCK_EMPTY_THRESHOLD,
    TEMP_CHECK_ENABLED, TEMP_ALERT_C, TEMP_RECOVER_C,
)


def _read_soc_temp():
    """SoC-Temperatur des Pi in Grad C (None wenn nicht lesbar)."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None
from mdb_sniffer import MDBSniffer
from notifier import WebhookNotifier, TelegramNotifier, Alert
from rules import RuleEngine
from llm import OllamaClient
from reporter import Reporter

logger = logging.getLogger("agent")


class AutomatCEO:
    """
    CEO-Agent: Observe -> Think -> Act

    Event-getrieben:  Verkauf/Fehler -> sofortige Benachrichtigung
    Periodisch:       Anomalien, Fuellstaende, Report-Zeitplan
    """

    def __init__(self, sniffer: MDBSniffer):
        self.sniffer = sniffer
        self.enabled = AGENT_ENABLED

        # Komponenten
        self.rules = RuleEngine()
        self.notifier = WebhookNotifier()
        self.telegram = TelegramNotifier()
        self.llm = OllamaClient()
        self.reporter = Reporter(llm=self.llm, db_path=sniffer.db_path)

        # Agent-Status
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.alert_log: deque = deque(maxlen=200)
        self._lock = threading.Lock()
        self._temp_alerted = False   # Zustands-Flag gegen Temp-Alarm-Spam
        self.stats = {
            "status": "Initialisiert",
            "started_at": None,
            "total_alerts": 0,
            "total_reports": 0,
            "last_check": None,
            "last_alert": None,
            "last_report": None,
            "checks_count": 0,
        }

        # Event-Callbacks beim Sniffer registrieren
        sniffer.on_vend_callbacks.append(self._on_vend)
        sniffer.on_error_callbacks.append(self._on_error)

        logger.info("CEO-Agent initialisiert")

    def start(self):
        """Startet den Agent."""
        if not self.enabled:
            logger.info("CEO-Agent deaktiviert (AGENT_ENABLED=False)")
            with self._lock:
                self.stats["status"] = "Deaktiviert"
            return

        self._running = True
        self.notifier.start()
        self.telegram.start()

        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()

        with self._lock:
            self.stats["status"] = "Aktiv"
            self.stats["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        # Startup-Benachrichtigung
        self._emit(Alert(
            type="alert",
            severity="info",
            title="CEO-Agent gestartet",
            message=f"Automat-Ueberwachung aktiv. "
                    f"LLM: {'verfuegbar' if self.llm.is_available() else 'nicht erreichbar'}. "
                    f"n8n: {'aktiv' if N8N_ENABLED else 'deaktiviert'}.",
            data={"llm": self.llm.get_status()},
        ))

        logger.info("CEO-Agent gestartet")

    def stop(self):
        """Stoppt den Agent."""
        self._running = False
        self.notifier.stop()
        self.telegram.stop()
        if self._thread:
            self._thread.join(timeout=5)
        with self._lock:
            self.stats["status"] = "Gestoppt"
        logger.info("CEO-Agent gestoppt")

    # ============================================================
    # Event-Handler (sofort, kein Delay)
    # ============================================================

    def _on_vend(self, vend_dict: dict):
        """Wird bei jedem Verkauf aufgerufen."""
        alert = self.rules.check_sale(vend_dict)
        self._emit(alert)

        # Fuellstand pruefen
        remaining = vend_dict.get("stock_remaining")
        if remaining is not None:
            slot = vend_dict.get("product_id", "?")
            pname = vend_dict.get("product_name", f"Slot #{slot}")
            if remaining <= ALERT_STOCK_EMPTY_THRESHOLD:
                self._emit(Alert(
                    type="alert", severity="critical",
                    title=f"LEER: {pname} (Slot #{slot})",
                    message=f"Fuellstand: 0 Stueck. Sofort auffuellen!",
                    data={"slot_id": slot, "remaining": 0},
                ))
            elif remaining <= ALERT_STOCK_LOW_THRESHOLD:
                self._emit(Alert(
                    type="alert", severity="warning",
                    title=f"Fuellstand niedrig: {pname} (Slot #{slot})",
                    message=f"Nur noch {remaining} Stueck uebrig.",
                    data={"slot_id": slot, "remaining": remaining},
                ))

    def _on_error(self, msg_dict: dict):
        """Wird bei MDB-Fehlern aufgerufen."""
        alert = self.rules.check_error(msg_dict)
        if alert:
            self._emit(alert)

    # ============================================================
    # Periodischer Check-Loop
    # ============================================================

    def _check_loop(self):
        """Hauptschleife: Prueft periodisch auf Anomalien und Reports."""
        logger.info("Check-Loop gestartet (Intervall: %ds)", AGENT_CHECK_INTERVAL)

        while self._running:
            time.sleep(AGENT_CHECK_INTERVAL)
            if not self._running:
                break

            try:
                self._run_checks()
            except Exception as e:
                logger.error("Check-Loop Fehler: %s", e)

    def _run_checks(self):
        """Fuehrt alle periodischen Checks durch."""
        with self._lock:
            self.stats["last_check"] = time.strftime("%H:%M:%S")
            self.stats["checks_count"] += 1

        # Anomalie: Keine Verkaeufe
        alert = self.rules.check_no_sales_anomaly(self.sniffer.db_path)
        if alert:
            self._emit(alert)

        # Anomalie: Umsatz-Abweichung
        alert = self.rules.check_revenue_anomaly(self.sniffer.db_path)
        if alert:
            self._emit(alert)

        # Hardware: Pi-Temperatur (Drossel-Schutz)
        self._check_temperature()

        # Tagesbericht faellig?
        if self.rules.check_daily_report_due():
            self._generate_daily_report()

        # Wochenbericht faellig?
        if self.rules.check_weekly_report_due():
            self._generate_weekly_report()

    def _check_temperature(self):
        """Warnt EINMAL wenn der Pi zu heiss wird, Entwarnung mit Hysterese."""
        if not TEMP_CHECK_ENABLED:
            return
        temp = _read_soc_temp()
        if temp is None:
            return
        with self._lock:
            self.stats["cpu_temp_c"] = temp

        if temp >= TEMP_ALERT_C and not self._temp_alerted:
            self._temp_alerted = True
            self._emit(Alert(
                type="alert", severity="critical",
                title=f"Pi zu heiss: {temp:.1f} °C",
                message=(f"SoC-Temperatur {temp:.1f} °C (Schwelle {TEMP_ALERT_C} °C). "
                         f"Der Pi 5 drosselt ab 80 °C und altert schneller. "
                         f"Bitte Kuehlung pruefen (aktiver Luefter / Active Cooler)."),
                data={"temp_c": temp},
            ))
        elif temp <= TEMP_RECOVER_C and self._temp_alerted:
            self._temp_alerted = False
            self._emit(Alert(
                type="alert", severity="info",
                title=f"Pi wieder kuehl: {temp:.1f} °C",
                message=f"Temperatur zurueck auf {temp:.1f} °C (unter {TEMP_RECOVER_C} °C).",
                data={"temp_c": temp},
            ))

    # ============================================================
    # Report-Generierung
    # ============================================================

    def _generate_daily_report(self):
        """Generiert und sendet den Tagesbericht."""
        logger.info("Erstelle Tagesbericht...")
        report = self.reporter.daily_report()
        self._emit(report)
        with self._lock:
            self.stats["total_reports"] += 1
            self.stats["last_report"] = time.strftime("%H:%M:%S")

    def _generate_weekly_report(self):
        """Generiert und sendet den Wochenbericht."""
        logger.info("Erstelle Wochenbericht...")
        report = self.reporter.weekly_report()
        self._emit(report)
        with self._lock:
            self.stats["total_reports"] += 1
            self.stats["last_report"] = time.strftime("%H:%M:%S")

    def trigger_daily_report(self) -> Alert:
        """Manuell Tagesbericht ausloesen (via API)."""
        report = self.reporter.daily_report()
        self._emit(report)
        with self._lock:
            self.stats["total_reports"] += 1
            self.stats["last_report"] = time.strftime("%H:%M:%S")
        return report

    def trigger_weekly_report(self) -> Alert:
        """Manuell Wochenbericht ausloesen (via API)."""
        report = self.reporter.weekly_report()
        self._emit(report)
        with self._lock:
            self.stats["total_reports"] += 1
            self.stats["last_report"] = time.strftime("%H:%M:%S")
        return report

    # ============================================================
    # Helpers
    # ============================================================

    def _emit(self, alert: Alert):
        """Speichert Alert im Log und sendet an n8n + Telegram."""
        alert_dict = alert.to_dict()
        self.alert_log.appendleft(alert_dict)
        self.notifier.send(alert)
        self.telegram.send(alert)

        with self._lock:
            self.stats["total_alerts"] += 1
            self.stats["last_alert"] = alert.title

        # In DB speichern
        self._save_alert(alert_dict)

        level = logging.WARNING if alert.severity == "critical" else logging.INFO
        logger.log(level, "[%s] %s: %s", alert.severity.upper(), alert.type, alert.title)

    def _save_alert(self, alert_dict: dict):
        """Speichert einen Alert in der SQLite-Datenbank."""
        try:
            conn = sqlite3.connect(self.sniffer.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    type TEXT,
                    severity TEXT,
                    title TEXT,
                    message TEXT
                )
            """)
            conn.execute(
                "INSERT INTO agent_alerts (timestamp, type, severity, title, message) "
                "VALUES (?, ?, ?, ?, ?)",
                (alert_dict["timestamp"], alert_dict["type"],
                 alert_dict["severity"], alert_dict["title"], alert_dict["message"])
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("DB-Fehler (Alert): %s", e)

    def get_status(self) -> dict:
        """Gibt den Agent-Status zurueck."""
        with self._lock:
            status = dict(self.stats)
        status["llm"] = self.llm.get_status()
        status["notifier"] = self.notifier.get_stats()
        status["telegram"] = self.telegram.get_stats()
        return status

    def get_recent_alerts(self, limit: int = 50) -> list:
        """Gibt die letzten Alerts zurueck."""
        return list(self.alert_log)[:limit]
