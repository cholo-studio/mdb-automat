"""
Telegram Bot — Interaktive Befehle
====================================
Empfaengt Befehle per Telegram und antwortet mit Live-Daten.
Nutzt Long-Polling (getUpdates) — kein Webhook noetig.

Befehle:
  /status   - Kurzstatus des Automaten
  /report   - Tagesbericht ausloesen
  /week     - Wochenbericht ausloesen
  /stock    - Fuellstaende anzeigen
  /sales    - Letzte 5 Verkaeufe
  /peak     - Beste Verkaufszeiten
  /weather  - Aktuelles Wetter
  /help     - Alle Befehle anzeigen
"""

import logging
import sqlite3
import threading
import time
from typing import Optional

import httpx

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED,
    AUTOMAT_NAME, AUTOMAT_STANDORT,
)

logger = logging.getLogger("telegram_bot")


class TelegramBot:
    """Empfaengt und beantwortet Telegram-Befehle."""

    def __init__(self, sniffer=None, ceo=None, analytics=None, weather=None):
        self.token = TELEGRAM_BOT_TOKEN
        self.chat_id = TELEGRAM_CHAT_ID
        self.enabled = TELEGRAM_ENABLED and bool(self.token) and bool(self.chat_id)

        # Referenzen auf die System-Komponenten
        self.sniffer = sniffer
        self.ceo = ceo
        self.analytics = analytics
        self.weather = weather

        self._running = False
        self._thread = None  # type: Optional[threading.Thread]
        self._last_update_id = 0

        # Befehle registrieren
        self.commands = {
            "/start": self._cmd_help,
            "/help": self._cmd_help,
            "/hilfe": self._cmd_help,
            "/status": self._cmd_status,
            "/report": self._cmd_daily_report,
            "/bericht": self._cmd_daily_report,
            "/week": self._cmd_weekly_report,
            "/woche": self._cmd_weekly_report,
            "/stock": self._cmd_stock,
            "/fuellstand": self._cmd_stock,
            "/sales": self._cmd_sales,
            "/verkaeufe": self._cmd_sales,
            "/peak": self._cmd_peak,
            "/weather": self._cmd_weather,
            "/wetter": self._cmd_weather,
        }

    def start(self):
        """Startet den Bot-Polling-Thread."""
        if not self.enabled:
            logger.info("Telegram-Bot deaktiviert")
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("Telegram-Bot gestartet (Polling)")

    def stop(self):
        """Stoppt den Bot."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    # ============================================================
    # Polling-Loop
    # ============================================================

    def _poll_loop(self):
        """Long-Polling: Fragt alle 3 Sekunden nach neuen Nachrichten."""
        logger.info("Bot Polling-Loop gestartet")
        while self._running:
            try:
                self._check_updates()
            except Exception as e:
                logger.error("Bot Polling-Fehler: %s", e)
            time.sleep(3)

    def _check_updates(self):
        """Holt neue Nachrichten via getUpdates."""
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params = {
            "offset": self._last_update_id + 1,
            "timeout": 10,
            "allowed_updates": ["message"],
        }

        try:
            response = httpx.get(url, params=params, timeout=15.0)
            if response.status_code != 200:
                return

            data = response.json()
            if not data.get("ok"):
                return

            for update in data.get("result", []):
                self._last_update_id = update["update_id"]
                self._handle_update(update)

        except httpx.TimeoutException:
            pass  # Normal bei Long-Polling
        except httpx.ConnectError:
            logger.warning("Telegram nicht erreichbar")
            time.sleep(10)

    def _handle_update(self, update: dict):
        """Verarbeitet eine eingehende Nachricht."""
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "").strip()

        # Nur Nachrichten aus unserem Chat akzeptieren
        if chat_id != self.chat_id:
            logger.warning("Nachricht von unbekanntem Chat: %s", chat_id)
            return

        if not text:
            return

        # Befehl extrahieren (erstes Wort)
        cmd = text.split()[0].lower()
        # @botname entfernen
        if "@" in cmd:
            cmd = cmd.split("@")[0]

        handler = self.commands.get(cmd)
        if handler:
            logger.info("Bot-Befehl: %s", cmd)
            try:
                response_text = handler()
                self._reply(response_text)
            except Exception as e:
                logger.error("Befehl %s Fehler: %s", cmd, e)
                self._reply(f"\u274C Fehler bei {cmd}: {e}")
        else:
            self._reply(
                "\U0001F914 Unbekannter Befehl.\n"
                "Tippe /help fuer alle Befehle."
            )

    def _reply(self, text: str):
        """Sendet eine Antwort an den Chat."""
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            httpx.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }, timeout=10.0)
        except Exception as e:
            logger.error("Bot Reply-Fehler: %s", e)

    # ============================================================
    # Befehle
    # ============================================================

    def _cmd_help(self) -> str:
        """Zeigt alle verfuegbaren Befehle."""
        return (
            f"\U0001F916 *{AUTOMAT_NAME}* — Bot-Befehle\n"
            f"\U0001F4CD {AUTOMAT_STANDORT}\n"
            "\n"
            "\U0001F4CA /status — Kurzstatus\n"
            "\U0001F4CB /report — Tagesbericht\n"
            "\U0001F4C8 /week — Wochenbericht\n"
            "\U0001F4E6 /stock — Fuellstaende\n"
            "\U0001F6D2 /sales — Letzte Verkaeufe\n"
            "\u23F0 /peak — Beste Verkaufszeiten\n"
            "\u2600\uFE0F /weather — Aktuelles Wetter\n"
            "\u2753 /help — Diese Hilfe\n"
            "\n"
            "_Auch auf Deutsch: /bericht /woche /fuellstand /verkaeufe /wetter_"
        )

    def _cmd_status(self) -> str:
        """Kurzstatus des Automaten."""
        if not self.sniffer:
            return "\u274C Sniffer nicht verfuegbar"

        stats = self.sniffer.get_stats()
        conn = stats.get("connection_status", "?")
        uptime = stats.get("uptime_str", "?")
        msgs = stats.get("total_messages", 0)
        total_vends = stats.get("total_vends", 0)
        total_rev = stats.get("total_revenue_euro", "0.00")

        conn_emoji = "\U0001F7E2" if conn == "Verbunden" else "\U0001F534"

        lines = [
            f"{conn_emoji} *{AUTOMAT_NAME}*",
            f"\U0001F4CD {AUTOMAT_STANDORT}",
            "",
            f"\U0001F50C Verbindung: {conn}",
            f"\u23F1 Laufzeit: {uptime}",
            f"\U0001F4E8 Nachrichten: {msgs:,}".replace(",", "."),
            f"\U0001F6D2 Verkaeufe: *{total_vends}*",
            f"\U0001F4B0 Umsatz: *{total_rev} EUR*",
        ]

        # Agent-Status
        if self.ceo:
            agent_status = self.ceo.get_status()
            checks = agent_status.get("checks_count", 0)
            alerts = agent_status.get("total_alerts", 0)
            lines.append(f"\U0001F9E0 Agent: {agent_status.get('status', '?')} ({checks} Checks, {alerts} Alerts)")

        lines.append(f"\n\U0001F552 {time.strftime('%H:%M:%S')}")
        return "\n".join(lines)

    def _cmd_daily_report(self) -> str:
        """Loest Tagesbericht aus und sendet ihn."""
        if not self.ceo:
            return "\u274C CEO-Agent nicht verfuegbar"

        # Bericht wird ueber Agent gesendet (inkl. Telegram-Formatierung)
        self.ceo.trigger_daily_report()
        return "\U0001F4CA Tagesbericht wird erstellt und gleich gesendet..."

    def _cmd_weekly_report(self) -> str:
        """Loest Wochenbericht aus und sendet ihn."""
        if not self.ceo:
            return "\u274C CEO-Agent nicht verfuegbar"

        self.ceo.trigger_weekly_report()
        return "\U0001F4C8 Wochenbericht wird erstellt und gleich gesendet..."

    def _cmd_stock(self) -> str:
        """Zeigt aktuelle Fuellstaende."""
        if not self.sniffer:
            return "\u274C Sniffer nicht verfuegbar"

        levels = self.sniffer.stock.get_all_levels()
        if not levels:
            return "\U0001F4E6 Keine Fuellstaende konfiguriert.\nSetze Fuellstaende im Dashboard unter /api/stock."

        lines = ["\U0001F4E6 *Fuellstaende*", ""]

        for item in levels:
            slot = item.get("slot_id", "?")
            name = item.get("product_name", f"Slot #{slot}")
            current = item.get("current_count", 0)
            max_cap = item.get("max_capacity", 15)
            pct = (current / max_cap * 100) if max_cap > 0 else 0

            # Balken erzeugen
            filled = int(pct / 10)
            bar = "\u2588" * filled + "\u2591" * (10 - filled)

            # Emoji nach Fuellstand
            if pct <= 0:
                emoji = "\U0001F534"  # 🔴
            elif pct <= 20:
                emoji = "\U0001F7E0"  # 🟠
            elif pct <= 50:
                emoji = "\U0001F7E1"  # 🟡
            else:
                emoji = "\U0001F7E2"  # 🟢

            lines.append(f"{emoji} *{name}*")
            lines.append(f"   {bar} {current}/{max_cap} ({pct:.0f}%)")

        lines.append(f"\n\U0001F552 {time.strftime('%H:%M:%S')}")
        return "\n".join(lines)

    def _cmd_sales(self) -> str:
        """Letzte 5 Verkaeufe."""
        if not self.sniffer:
            return "\u274C Sniffer nicht verfuegbar"

        vends = self.sniffer.get_recent_vends(5)
        if not vends:
            return "\U0001F6D2 Noch keine Verkaeufe aufgezeichnet."

        lines = ["\U0001F6D2 *Letzte Verkaeufe*", ""]

        for v in vends:
            ts = v.get("time_str", "?")
            product = v.get("product_name", f"Slot #{v.get('product_id', '?')}")
            price = v.get("price_euro", "?")
            payment = v.get("payment_method", "?")

            lines.append(f"\u2022 {ts} — *{product}*")
            lines.append(f"  {price} EUR ({payment})")

        lines.append(f"\n\U0001F552 {time.strftime('%H:%M:%S')}")
        return "\n".join(lines)

    def _cmd_peak(self) -> str:
        """Beste Verkaufszeiten."""
        if not self.analytics:
            return "\u274C Analytics nicht verfuegbar"

        hours = self.analytics.get_peak_hours(days=30)
        if not hours:
            return "\u23F0 Nicht genug Daten fuer Peak-Hours Analyse."

        # Top-5 Stunden
        top5 = sorted(hours, key=lambda h: h["count"], reverse=True)[:5]
        top5 = [h for h in top5 if h["count"] > 0]

        if not top5:
            return "\u23F0 Noch keine Verkaeufe fuer Peak-Hours Analyse."

        lines = ["\u23F0 *Beste Verkaufszeiten* (30 Tage)", ""]

        for i, h in enumerate(top5):
            medals = ["\U0001F947", "\U0001F948", "\U0001F949", "4\uFE0F\u20E3", "5\uFE0F\u20E3"]
            medal = medals[i] if i < len(medals) else "\u2022"
            lines.append(f"{medal} *{h['hour']:02d}:00-{h['hour']:02d}:59* — {h['count']} Verkaeufe")

        lines.append(f"\n\U0001F552 {time.strftime('%H:%M:%S')}")
        return "\n".join(lines)

    def _cmd_weather(self) -> str:
        """Aktuelles Wetter."""
        if not self.weather or not self.weather.enabled:
            return "\u2600\uFE0F Wetterdaten nicht aktiviert."

        data = self.weather.get_today()
        if not data:
            return "\u2600\uFE0F Noch keine Wetterdaten verfuegbar."

        emoji = data.get("condition_emoji", "\u2600\uFE0F")
        condition = data.get("condition_de", "?")
        temp_avg = data.get("temp_avg", "?")
        temp_min = data.get("temp_min", "?")
        temp_max = data.get("temp_max", "?")
        humidity = data.get("humidity", "?")
        rain = data.get("rain_mm", 0)

        lines = [
            f"{emoji} *Wetter {AUTOMAT_STANDORT}*",
            "",
            f"\U0001F321 Temperatur: *{temp_avg}\u00B0C*",
            f"  Min: {temp_min}\u00B0C / Max: {temp_max}\u00B0C",
            f"\U0001F4A7 Feuchtigkeit: {humidity}%",
            f"\U0001F327 Regen: {rain} mm",
            f"\U0001F324 Zustand: {condition}",
        ]

        # Wetter-Korrelation
        summary = self.weather.get_condition_summary(days=30)
        if summary:
            lines.append("")
            lines.append("*Verkaeufe nach Wetter (30T):*")
            for s in summary[:3]:
                cond = s.get("condition_de", "?")
                avg_vends = s.get("avg_vends", 0)
                lines.append(f"  {cond}: \u2300 {avg_vends:.1f} Verk./Tag")

        lines.append(f"\n\U0001F552 {time.strftime('%H:%M:%S')}")
        return "\n".join(lines)
