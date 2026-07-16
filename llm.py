"""
Ollama/Mistral LLM Client
==========================
Verbindet sich mit Ollama auf dem Windows-PC (3080 GPU) fuer
natuerlichsprachliche Analysen und Reports.

Fallback: Wenn LLM nicht erreichbar, wird regelbasierter Text verwendet.
"""

import logging
import time
from typing import Optional

import httpx

from config import OLLAMA_HOST, OLLAMA_MODEL, LLM_ENABLED, LLM_TIMEOUT

logger = logging.getLogger("llm")

SYSTEM_PROMPT = (
    "Du bist der Geschaeftsfuehrer eines Sielaff SUe2020 Kaffeeautomaten. "
    "Du analysierst Verkaufsdaten, Fehler und Trends. "
    "Antworte knapp, professionell, auf Deutsch. "
    "Verwende konkrete Zahlen. Gib Handlungsempfehlungen wenn noetig."
)


class OllamaClient:
    """Client fuer Ollama LLM-API."""

    def __init__(self):
        self.host = OLLAMA_HOST
        self.model = OLLAMA_MODEL
        self.enabled = LLM_ENABLED
        self.timeout = LLM_TIMEOUT
        self._available: Optional[bool] = None
        self._last_check: float = 0

    def is_available(self) -> bool:
        """Prueft ob Ollama erreichbar ist (cached fuer 60s)."""
        if not self.enabled:
            return False

        now = time.time()
        if self._available is not None and (now - self._last_check) < 60:
            return self._available

        try:
            resp = httpx.get(f"{self.host}/api/tags", timeout=5)
            self._available = resp.status_code == 200
        except Exception:
            self._available = False

        self._last_check = now
        if self._available:
            logger.info("Ollama erreichbar: %s (%s)", self.host, self.model)
        else:
            logger.warning("Ollama nicht erreichbar: %s", self.host)
        return self._available

    def generate(self, prompt: str, system: str = SYSTEM_PROMPT) -> Optional[str]:
        """Sendet einen Prompt an Ollama und gibt die Antwort zurueck."""
        if not self.is_available():
            return None

        try:
            resp = httpx.post(
                f"{self.host}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "system": system,
                    "stream": False,
                    "options": {
                        "temperature": 0.3,
                        "num_predict": 500,
                    },
                },
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("response", "").strip()
            else:
                logger.warning("Ollama HTTP %d: %s", resp.status_code, resp.text[:200])
                return None

        except httpx.TimeoutException:
            logger.warning("Ollama Timeout nach %ds", self.timeout)
            return None
        except Exception as e:
            logger.error("Ollama Fehler: %s", e)
            return None

    def analyze_day(self, stats: dict) -> str:
        """Analysiert die Tagesdaten mit LLM."""
        prompt = (
            f"Analysiere den heutigen Tag des Kaffeeautomaten:\n"
            f"- Verkaeufe: {stats.get('total_sales', 0)}\n"
            f"- Umsatz: {stats.get('revenue_euro', '0.00')} EUR\n"
            f"- Top-Produkt: #{stats.get('top_product_id', '?')} "
            f"({stats.get('top_product_count', 0)}x)\n"
            f"- Zahlungen: {stats.get('payment_breakdown', {})}\n"
            f"- Fehler: {stats.get('errors', 0)}\n"
            f"- Durchschnittspreis: {stats.get('avg_price_euro', '0.00')} EUR\n\n"
            f"Gib eine kurze Zusammenfassung (3-4 Saetze) mit Bewertung."
        )
        result = self.generate(prompt)
        if result:
            return result

        # Fallback ohne LLM
        return (
            f"Tagesbericht: {stats.get('total_sales', 0)} Verkaeufe, "
            f"{stats.get('revenue_euro', '0.00')} EUR Umsatz. "
            f"Top-Produkt: #{stats.get('top_product_id', '?')}."
        )

    def analyze_week(self, stats: dict) -> str:
        """Analysiert die Wochendaten mit LLM."""
        prompt = (
            f"Analysiere die Woche des Kaffeeautomaten:\n"
            f"- Gesamt-Verkaeufe: {stats.get('total_sales', 0)}\n"
            f"- Gesamt-Umsatz: {stats.get('revenue_euro', '0.00')} EUR\n"
            f"- Bester Tag: {stats.get('best_day', '?')} "
            f"({stats.get('best_day_revenue', '0.00')} EUR)\n"
            f"- Schlechtester Tag: {stats.get('worst_day', '?')} "
            f"({stats.get('worst_day_revenue', '0.00')} EUR)\n"
            f"- Tagesdurchschnitt: {stats.get('avg_daily_revenue', '0.00')} EUR\n"
            f"- Trend vs. Vorwoche: {stats.get('trend_percent', 0):+.1f}%\n\n"
            f"Gib eine kurze Zusammenfassung (3-4 Saetze) mit Trend-Bewertung."
        )
        result = self.generate(prompt)
        if result:
            return result

        trend = stats.get("trend_percent", 0)
        trend_text = "stabil" if abs(trend) < 5 else (
            f"{trend:+.0f}% vs. Vorwoche"
        )
        return (
            f"Wochenbericht: {stats.get('total_sales', 0)} Verkaeufe, "
            f"{stats.get('revenue_euro', '0.00')} EUR Umsatz. "
            f"Trend: {trend_text}."
        )

    def analyze_anomaly(self, context: str) -> str:
        """Laesst das LLM eine Anomalie analysieren."""
        prompt = (
            f"Im Kaffeeautomaten wurde eine Anomalie erkannt:\n{context}\n\n"
            f"Was koennte die Ursache sein? Kurze Einschaetzung (2-3 Saetze)."
        )
        result = self.generate(prompt)
        return result or "Anomalie erkannt — LLM-Analyse nicht verfuegbar."

    def analyze_prices(self, recommendations: list) -> str:
        """Laesst das LLM Preisempfehlungen analysieren und kommentieren."""
        if not recommendations:
            return ""

        lines = []
        for r in recommendations[:10]:
            price = f"{r['current_price_cents']/100:.2f}" if r.get('current_price_cents') else '?'
            lines.append(
                f"- {r['product_name']}: {price} EUR, "
                f"{r['avg_per_day']} Verk./Tag, "
                f"Empfehlung: {r['recommendation']} ({r['reason']})"
            )

        prompt = (
            f"Analysiere diese Preisempfehlungen fuer den Kaffeeautomaten:\n\n"
            f"{chr(10).join(lines)}\n\n"
            f"Bewerte die Empfehlungen kurz (3-4 Saetze). "
            f"Beruecksichtige: Ist die Preissensitivitaet bei Automaten hoch? "
            f"Welche Anpassungen sind realistisch?"
        )
        result = self.generate(prompt)
        return result or ""

    def analyze_weather_correlation(self, correlation_data: list) -> str:
        """Analysiert Wetter-Verkaufs-Korrelation mit LLM."""
        if not correlation_data:
            return ""

        lines = []
        for r in correlation_data[:14]:
            lines.append(
                f"- {r.get('condition', '?')}: "
                f"⌀ {r.get('avg_vends', 0)} Verk., "
                f"⌀ {r.get('avg_temp', 0)}°C, "
                f"{r.get('days_count', 0)} Tage"
            )

        prompt = (
            f"Analysiere den Zusammenhang zwischen Wetter und Verkaeufen "
            f"am Kaffeeautomaten:\n\n{chr(10).join(lines)}\n\n"
            f"Gibt es Muster? Kurze Einschaetzung (2-3 Saetze)."
        )
        result = self.generate(prompt)
        return result or ""

    def get_status(self) -> dict:
        """Status des LLM-Clients."""
        return {
            "enabled": self.enabled,
            "available": self.is_available() if self.enabled else False,
            "host": self.host,
            "model": self.model,
        }
