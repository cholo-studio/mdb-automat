"""
Regelengine fuer den CEO-Agent
===============================
Prueft Schwellwerte, erkennt Anomalien, triggert Alerts.
Rein regelbasiert — kein LLM noetig, reagiert sofort.
"""

import logging
import sqlite3
import time
from typing import Optional

from notifier import Alert
from config import (
    ALERT_NO_SALES_AFTER_HOUR,
    ALERT_COIN_LOW_PERCENT,
    ALERT_COIN_HIGH_PERCENT,
    ALERT_ERROR_CRITICAL,
    ALERT_REVENUE_DEVIATION,
    DAILY_REPORT_HOUR,
    WEEKLY_REPORT_DAY,
    WEEKLY_REPORT_HOUR,
    DB_PATH,
)

logger = logging.getLogger("rules")


class RuleEngine:
    """Regelbasierte Ueberwachung des Automaten."""

    def __init__(self):
        self._last_daily_report_date: str = ""
        self._last_weekly_report_date: str = ""
        self._no_sales_alerted_today: bool = False
        self._last_check_date: str = ""
        self._revenue_alerted_today: bool = False
        self._last_revenue_check_date: str = ""

    def check_sale(self, vend_dict: dict) -> Alert:
        """Erzeugt einen Info-Alert bei jedem Verkauf."""
        price = vend_dict.get("price_euro", "0.00")
        product = vend_dict.get("product_id", "?")
        payment = vend_dict.get("payment_method", "?")
        success = vend_dict.get("success", True)

        if not success:
            return Alert(
                type="error",
                severity="warning",
                title=f"Verkauf fehlgeschlagen: Produkt #{product}",
                message=f"Preis: {price} EUR, Zahlung: {payment}",
                data=vend_dict,
            )

        return Alert(
            type="sale",
            severity="info",
            title=f"Verkauf: Produkt #{product} — {price} EUR",
            message=f"Zahlung: {payment}, Preis: {price} EUR",
            data=vend_dict,
        )

    def check_error(self, msg_dict: dict) -> Optional[Alert]:
        """Prueft ob eine MDB-Nachricht einen kritischen Fehler enthaelt."""
        command = msg_dict.get("command", "")

        if command in ALERT_ERROR_CRITICAL:
            return Alert(
                type="error",
                severity="critical",
                title=f"MDB-Fehler: {command}",
                message=f"Geraet: {msg_dict.get('device', '?')}, "
                        f"Details: {msg_dict.get('description', '-')}",
                data=msg_dict,
            )

        if command == "NAK":
            return Alert(
                type="error",
                severity="warning",
                title="MDB NAK — Geraet antwortet negativ",
                message=f"Geraet: {msg_dict.get('device', '?')}",
                data=msg_dict,
            )

        return None

    def check_no_sales_anomaly(self, db_path: str = DB_PATH) -> Optional[Alert]:
        """Prueft ob nach einer bestimmten Uhrzeit noch keine Verkaeufe stattfanden."""
        now = time.localtime()
        today = time.strftime("%Y-%m-%d")

        # Reset bei neuem Tag
        if today != self._last_check_date:
            self._no_sales_alerted_today = False
            self._last_check_date = today

        if self._no_sales_alerted_today:
            return None

        if now.tm_hour < ALERT_NO_SALES_AFTER_HOUR:
            return None

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.execute(
                "SELECT total_sales FROM daily_stats WHERE date = ?", (today,)
            )
            row = cursor.fetchone()
            conn.close()

            sales_today = row[0] if row else 0
        except Exception:
            return None

        if sales_today == 0:
            self._no_sales_alerted_today = True
            return Alert(
                type="alert",
                severity="warning",
                title=f"Keine Verkaeufe seit Mitternacht",
                message=f"Es ist {now.tm_hour}:{now.tm_min:02d} Uhr und "
                        f"noch kein einziger Verkauf. Automat pruefen!",
                data={"hour": now.tm_hour, "sales_today": 0},
            )

        return None

    def check_revenue_anomaly(self, db_path: str = DB_PATH) -> Optional[Alert]:
        """Prueft EINMAL taeglich (abends) ob der Tagesumsatz stark vom
        echten 7-Tage-Schnitt abweicht. Behebt den frueheren Alarm-Spam:
        (1) echter 7-Tage-Schnitt statt All-Time, (2) Bewertung erst am
        (fast) vollen Tag statt Teil-Tag-gegen-Voll-Tag, (3) max 1 Alarm/Tag,
        (4) Mindest-Historie & Mindest-Schnitt gegen Rauschen bei Kleinmengen.
        """
        now = time.localtime()
        today = time.strftime("%Y-%m-%d")

        # Tages-Reset des Cooldown-Flags
        if today != self._last_revenue_check_date:
            self._revenue_alerted_today = False
            self._last_revenue_check_date = today

        # Erst abends bewerten -> Tag ist (fast) vollstaendig, fairer Vergleich
        if now.tm_hour < 20:
            return None
        if self._revenue_alerted_today:
            return None

        try:
            conn = sqlite3.connect(db_path)

            row = conn.execute(
                "SELECT total_revenue_cents, total_sales FROM daily_stats WHERE date = ?",
                (today,),
            ).fetchone()
            today_revenue = row[0] if row else 0
            sales_today = row[1] if row else 0

            # ECHTER 7-Tage-Schnitt via Subquery (LIMIT wirkt vor AVG)
            row = conn.execute(
                "SELECT AVG(r), COUNT(r) FROM ("
                "  SELECT total_revenue_cents AS r FROM daily_stats "
                "  WHERE date < ? ORDER BY date DESC LIMIT 7)",
                (today,),
            ).fetchone()
            avg_revenue = row[0] if row and row[0] else 0
            history_days = row[1] if row else 0
            conn.close()

            # Guards gegen Rauschen: genug Historie, sinnvoller Schnitt,
            # heute ueberhaupt Betrieb (sonst uebernimmt der no-sales-Check)
            if history_days < 3 or avg_revenue < 300 or sales_today < 1:
                return None

            deviation = ((today_revenue - avg_revenue) / avg_revenue) * 100

            if abs(deviation) > ALERT_REVENUE_DEVIATION:
                self._revenue_alerted_today = True   # nur EIN Alarm pro Tag
                direction = "unter" if deviation < 0 else "ueber"
                return Alert(
                    type="alert",
                    severity="warning",
                    title=f"Umsatz {abs(deviation):.0f}% {direction} Durchschnitt",
                    message=f"Heute: {today_revenue/100:.2f} EUR, "
                            f"7-Tage-Schnitt: {avg_revenue/100:.2f} EUR "
                            f"({history_days} Tage Historie)",
                    data={
                        "today_revenue_cents": today_revenue,
                        "avg_revenue_cents": int(avg_revenue),
                        "deviation_percent": round(deviation, 1),
                    },
                )
        except Exception as e:
            logger.error("Revenue-Check Fehler: %s", e)

        return None

    def check_daily_report_due(self) -> bool:
        """Prueft ob der Tagesbericht faellig ist."""
        now = time.localtime()
        today = time.strftime("%Y-%m-%d")

        if now.tm_hour == DAILY_REPORT_HOUR and self._last_daily_report_date != today:
            self._last_daily_report_date = today
            return True
        return False

    def check_weekly_report_due(self) -> bool:
        """Prueft ob der Wochenbericht faellig ist."""
        now = time.localtime()
        today = time.strftime("%Y-%m-%d")

        if (now.tm_wday == WEEKLY_REPORT_DAY and
                now.tm_hour == WEEKLY_REPORT_HOUR and
                self._last_weekly_report_date != today):
            self._last_weekly_report_date = today
            return True
        return False
