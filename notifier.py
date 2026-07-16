"""
n8n Webhook-Notifier
====================
Sendet Events (Verkaeufe, Fehler, Alerts, Reports) an n8n Webhooks.
Queue-basiert mit Retry-Logik.
"""

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Optional

import httpx

from config import N8N_WEBHOOK_URL, N8N_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED

logger = logging.getLogger("notifier")


@dataclass
class Alert:
    """Ein Event/Alert das an n8n gesendet wird."""
    type: str           # sale, error, alert, daily_report, weekly_report
    severity: str       # info, warning, critical
    title: str
    message: str
    data: dict = field(default_factory=dict)
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")

    def to_dict(self) -> dict:
        return asdict(self)


class WebhookNotifier:
    """Sendet Alerts an n8n Webhooks mit Queue und Retry."""

    def __init__(self, webhook_url: str = N8N_WEBHOOK_URL, enabled: bool = N8N_ENABLED):
        self.webhook_url = webhook_url
        self.enabled = enabled
        self.queue: deque = deque(maxlen=500)
        self.sent_log: deque = deque(maxlen=100)
        self.stats = {
            "total_sent": 0,
            "total_failed": 0,
            "last_sent": None,
            "last_error": None,
        }
        self._lock = threading.Lock()
        self._running = False

    def start(self):
        """Startet den Sender-Thread."""
        if not self.enabled:
            logger.info("Notifier deaktiviert (N8N_ENABLED=False)")
            return
        self._running = True
        threading.Thread(target=self._sender_loop, daemon=True).start()
        logger.info("Notifier gestartet -> %s", self.webhook_url)

    def stop(self):
        self._running = False

    def send(self, alert: Alert):
        """Fuegt einen Alert zur Queue hinzu."""
        if not self.enabled:
            return
        self.queue.append(alert)
        logger.debug("Alert in Queue: %s", alert.title)

    def get_stats(self) -> dict:
        with self._lock:
            return dict(self.stats)

    def get_recent(self, limit: int = 20) -> list:
        return list(self.sent_log)[:limit]

    def _sender_loop(self):
        """Verarbeitet die Queue alle 2 Sekunden."""
        logger.info("Sender-Loop gestartet")
        while self._running:
            time.sleep(2)
            self._flush_queue()

    def _flush_queue(self):
        """Sendet alle wartenden Alerts."""
        while self.queue:
            alert = self.queue.popleft()
            success = self._send_webhook(alert)
            with self._lock:
                if success:
                    self.stats["total_sent"] += 1
                    self.stats["last_sent"] = time.strftime("%H:%M:%S")
                    self.sent_log.appendleft(alert.to_dict())
                else:
                    self.stats["total_failed"] += 1

    def _send_webhook(self, alert: Alert, retries: int = 3) -> bool:
        """Sendet einen einzelnen Alert an n8n mit Retry."""
        payload = alert.to_dict()

        for attempt in range(1, retries + 1):
            try:
                response = httpx.post(
                    self.webhook_url,
                    json=payload,
                    timeout=10.0,
                    headers={"Content-Type": "application/json"},
                )
                if response.status_code < 300:
                    logger.info("Webhook gesendet: %s (%d)", alert.title, response.status_code)
                    return True
                else:
                    logger.warning("Webhook %d: HTTP %d", attempt, response.status_code)

            except httpx.TimeoutException:
                logger.warning("Webhook Timeout (Versuch %d/%d)", attempt, retries)
            except httpx.ConnectError:
                logger.warning("Webhook nicht erreichbar (Versuch %d/%d)", attempt, retries)
                with self._lock:
                    self.stats["last_error"] = f"Nicht erreichbar ({time.strftime('%H:%M:%S')})"
            except Exception as e:
                logger.error("Webhook Fehler: %s", e)
                with self._lock:
                    self.stats["last_error"] = str(e)
                break

            if attempt < retries:
                time.sleep(2 * attempt)

        return False


class TelegramNotifier:
    """Sendet Alerts direkt an Telegram (Fallback/Ergaenzung zu n8n)."""

    SEVERITY_EMOJI = {
        "critical": "\U0001F6A8",    # 🚨
        "warning": "\u26A0\uFE0F",   # ⚠️
        "info": "\u2139\uFE0F",      # ℹ️
    }

    TYPE_EMOJI = {
        "sale": "\U0001F4B0",        # 💰
        "error": "\u274C",           # ❌
        "alert": "\U0001F514",       # 🔔
        "daily_report": "\U0001F4CA", # 📊
        "weekly_report": "\U0001F4C8", # 📈
    }

    def __init__(self, bot_token: str = TELEGRAM_BOT_TOKEN,
                 chat_id: str = TELEGRAM_CHAT_ID,
                 enabled: bool = TELEGRAM_ENABLED):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled and bool(bot_token) and bool(chat_id)
        self.queue = deque(maxlen=500)
        self._lock = threading.Lock()
        self._running = False
        self.stats = {
            "total_sent": 0,
            "total_failed": 0,
            "last_sent": None,
            "last_error": None,
        }

    def start(self):
        """Startet den Telegram-Sender-Thread."""
        if not self.enabled:
            logger.info("Telegram deaktiviert (Token/Chat-ID fehlt oder TELEGRAM_ENABLED=False)")
            return
        self._running = True
        threading.Thread(target=self._sender_loop, daemon=True).start()
        logger.info("Telegram-Notifier gestartet (Chat: %s)", self.chat_id)

    def stop(self):
        self._running = False

    def send(self, alert):
        """Fuegt einen Alert zur Telegram-Queue hinzu."""
        if not self.enabled:
            return
        self.queue.append(alert)

    def get_stats(self) -> dict:
        with self._lock:
            return dict(self.stats)

    def _sender_loop(self):
        """Verarbeitet die Telegram-Queue alle 2 Sekunden."""
        while self._running:
            time.sleep(2)
            while self.queue:
                alert = self.queue.popleft()
                self._send_message(alert)

    def _format_message(self, alert) -> str:
        """Formatiert einen Alert als huebsche Telegram-Nachricht."""
        d = alert.to_dict() if hasattr(alert, 'to_dict') else alert
        atype = d.get("type", "alert")

        # Spezial-Formatierung fuer Reports
        if atype == "daily_report":
            return self._format_daily_report(d)
        if atype == "weekly_report":
            return self._format_weekly_report(d)

        # Standard-Formatierung fuer Alerts
        sev = d.get("severity", "info")
        emoji = self.SEVERITY_EMOJI.get(sev, "\U0001F514")
        type_emoji = self.TYPE_EMOJI.get(atype, "")

        lines = [
            f"{emoji} {type_emoji} *{d.get('title', 'Alert')}*",
            "",
            d.get("message", ""),
        ]

        # Relevante Daten anhaengen (nicht alles dumpen)
        data = d.get("data", {})
        extras = []
        for k in ("slot_id", "remaining", "deviation_percent", "payment_method"):
            if k in data:
                extras.append(f"\u2022 {k}: `{data[k]}`")
        if extras:
            lines.append("")
            lines.extend(extras)

        lines.append(f"\n\U0001F552 {d.get('timestamp', '')}")
        return "\n".join(lines)

    def _format_daily_report(self, d: dict) -> str:
        """Huebscher Tagesbericht fuer Telegram."""
        data = d.get("data", {})
        sales = data.get("total_sales", 0)
        revenue = data.get("revenue_euro", "0.00")
        errors = data.get("errors", 0)
        date = data.get("date", "?")

        lines = [
            "\U0001F4CA *TAGESBERICHT*",
            f"\U0001F4C5 {date}",
            "",
            f"\U0001F4B0 Umsatz: *{revenue} EUR*",
            f"\U0001F6D2 Verkaeufe: *{sales}*",
        ]

        # Durchschnittspreis
        avg_price = data.get("avg_price_euro", "")
        if avg_price and avg_price != "0.00":
            lines.append(f"\U0001F4B2 \u2300 Preis: {avg_price} EUR")

        # Top-Produkt
        top_id = data.get("top_product_id")
        top_cnt = data.get("top_product_count", 0)
        if top_id:
            lines.append(f"\U0001F3C6 Top: Slot #{top_id} ({top_cnt}x)")

        # Zahlungsarten
        breakdown = data.get("payment_breakdown", {})
        if breakdown:
            parts = [f"{k}: {v}" for k, v in breakdown.items()]
            lines.append(f"\U0001F4B3 {', '.join(parts)}")

        # Fehler
        if errors > 0:
            lines.append(f"\u26A0\uFE0F Fehler: {errors}")

        # LLM-Analyse aus message extrahieren
        msg = d.get("message", "")
        if "Analyse:" in msg:
            analysis = msg.split("Analyse:", 1)[1].strip()
            if analysis:
                lines.append(f"\n\U0001F9E0 _{analysis}_")

        # Stunden-Info aus message
        if "Top-Stunden:" in msg:
            hours_part = msg.split("Top-Stunden:", 1)[1].split("\n")[0].strip()
            if hours_part:
                lines.append(f"\u23F0 Peaks: {hours_part}")

        lines.append(f"\n\U0001F552 {d.get('timestamp', '')}")
        return "\n".join(lines)

    def _format_weekly_report(self, d: dict) -> str:
        """Huebscher Wochenbericht fuer Telegram."""
        data = d.get("data", {})
        sales = data.get("total_sales", 0)
        revenue = data.get("revenue_euro", "0.00")
        trend = data.get("trend_percent", 0)
        avg_daily = data.get("avg_daily_revenue", "0.00")
        best_day = data.get("best_day", "?")
        best_rev = data.get("best_day_revenue", "0.00")

        # Trend Emoji
        if trend > 5:
            trend_emoji = "\U0001F4C8"  # 📈
            trend_str = f"+{trend:.0f}%"
        elif trend < -5:
            trend_emoji = "\U0001F4C9"  # 📉
            trend_str = f"{trend:.0f}%"
        else:
            trend_emoji = "\u27A1\uFE0F"  # ➡️
            trend_str = "stabil"

        lines = [
            "\U0001F4C8 *WOCHENBERICHT*",
            f"\U0001F4C5 Letzte 7 Tage",
            "",
            f"\U0001F4B0 Umsatz: *{revenue} EUR*",
            f"\U0001F6D2 Verkaeufe: *{sales}*",
            f"{trend_emoji} Trend: *{trend_str}* vs. Vorwoche",
            f"\U0001F4CA \u2300 Tagesumsatz: {avg_daily} EUR",
            f"\U0001F3C6 Bester Tag: {best_day} ({best_rev} EUR)",
        ]

        # Preisempfehlungen aus message
        msg = d.get("message", "")
        if "Preisempfehlungen:" in msg:
            rec_part = msg.split("Preisempfehlungen:", 1)[1].split("\nAnalyse:")[0].strip()
            if rec_part:
                lines.append(f"\n\U0001F4A1 *Preisempfehlungen:*")
                for line in rec_part.split("\n"):
                    line = line.strip()
                    if line:
                        lines.append(f"  {line}")

        # LLM-Analyse
        if "Analyse:" in msg:
            analysis = msg.split("Analyse:", 1)[1].strip()
            if analysis:
                lines.append(f"\n\U0001F9E0 _{analysis}_")

        lines.append(f"\n\U0001F552 {d.get('timestamp', '')}")
        return "\n".join(lines)

    def _send_message(self, alert, retries: int = 3) -> bool:
        """Sendet eine Nachricht an Telegram mit Retry."""
        text = self._format_message(alert)
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        for attempt in range(1, retries + 1):
            try:
                response = httpx.post(url, json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                }, timeout=10.0)

                if response.status_code == 200:
                    with self._lock:
                        self.stats["total_sent"] += 1
                        self.stats["last_sent"] = time.strftime("%H:%M:%S")
                    logger.info("Telegram gesendet: %s",
                                alert.title if hasattr(alert, 'title') else 'Alert')
                    return True
                else:
                    logger.warning("Telegram HTTP %d (Versuch %d/%d): %s",
                                   response.status_code, attempt, retries,
                                   response.text[:200])

            except httpx.TimeoutException:
                logger.warning("Telegram Timeout (Versuch %d/%d)", attempt, retries)
            except httpx.ConnectError:
                logger.warning("Telegram nicht erreichbar (Versuch %d/%d)", attempt, retries)
            except Exception as e:
                logger.error("Telegram Fehler: %s", e)
                with self._lock:
                    self.stats["last_error"] = str(e)
                break

            if attempt < retries:
                time.sleep(2 * attempt)

        with self._lock:
            self.stats["total_failed"] += 1
            self.stats["last_error"] = f"Fehlgeschlagen ({time.strftime('%H:%M:%S')})"
        return False

    def send_test(self) -> dict:
        """Sendet eine Test-Nachricht und gibt das Ergebnis zurueck."""
        if not self.enabled:
            return {"success": False, "error": "Telegram deaktiviert"}

        test_alert = Alert(
            type="alert",
            severity="info",
            title="Test-Nachricht",
            message="Telegram-Verbindung funktioniert! "
                    "Der CEO-Agent kann dir jetzt Nachrichten senden.",
            data={"test": True},
        )
        success = self._send_message(test_alert)
        return {"success": success, "error": None if success else "Senden fehlgeschlagen"}
