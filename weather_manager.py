"""
Wetter-Manager
===============
Holt Wetterdaten von OpenWeatherMap und korreliert sie mit Verkaufsdaten.
Laeuft als Background-Thread, speichert in SQLite.
"""

import logging
import sqlite3
import threading
import time
from typing import Optional

import httpx

from config import DB_PATH

logger = logging.getLogger("weather")


def init_weather_tables(db_path):
    """Erstellt die weather_data Tabelle."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS weather_data (
            date TEXT PRIMARY KEY,
            temp_min REAL,
            temp_max REAL,
            temp_avg REAL,
            condition TEXT,
            humidity INTEGER,
            rain_mm REAL DEFAULT 0,
            fetched_at REAL
        )
    """)
    conn.commit()
    conn.close()


class WeatherManager:
    """Holt und speichert Wetterdaten, korreliert mit Verkaeufen."""

    CONDITION_MAP = {
        "Clear": "Sonnig",
        "Clouds": "Bewoelkt",
        "Rain": "Regen",
        "Drizzle": "Nieselregen",
        "Thunderstorm": "Gewitter",
        "Snow": "Schnee",
        "Mist": "Nebel",
        "Fog": "Nebel",
        "Haze": "Dunst",
    }

    CONDITION_EMOJI = {
        "Sonnig": "\u2600\uFE0F",
        "Bewoelkt": "\u2601\uFE0F",
        "Regen": "\U0001F327\uFE0F",
        "Nieselregen": "\U0001F326\uFE0F",
        "Gewitter": "\u26C8\uFE0F",
        "Schnee": "\u2744\uFE0F",
        "Nebel": "\U0001F32B\uFE0F",
        "Dunst": "\U0001F32B\uFE0F",
    }

    def __init__(self, db_path=DB_PATH, api_key="",
                 city="Berlin", fetch_interval=7200, enabled=False):
        self.db_path = db_path
        self.api_key = api_key
        self.city = city
        self.fetch_interval = fetch_interval
        self.enabled = enabled and bool(api_key)
        self._running = False
        self._thread = None  # type: Optional[threading.Thread]
        init_weather_tables(db_path)
        logger.info("WeatherManager initialisiert (enabled=%s, city=%s)",
                     self.enabled, self.city)

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def start(self):
        """Startet den Wetter-Fetch-Thread."""
        if not self.enabled:
            logger.info("Wetter deaktiviert (API-Key fehlt oder WEATHER_ENABLED=False)")
            return
        self._running = True
        self._thread = threading.Thread(target=self._fetch_loop, daemon=True)
        self._thread.start()
        logger.info("Wetter-Thread gestartet (Intervall: %ds)", self.fetch_interval)

    def stop(self):
        self._running = False

    def _fetch_loop(self):
        """Periodisches Wetter-Abrufen."""
        # Sofort einmal holen
        self._fetch_and_store()
        while self._running:
            time.sleep(self.fetch_interval)
            if self._running:
                self._fetch_and_store()

    def _fetch_and_store(self):
        """Holt aktuelles Wetter von OpenWeatherMap und speichert es."""
        try:
            url = (
                f"https://api.openweathermap.org/data/2.5/weather"
                f"?q={self.city}&appid={self.api_key}&units=metric&lang=de"
            )
            response = httpx.get(url, timeout=10.0)
            if response.status_code != 200:
                logger.warning("Wetter-API HTTP %d: %s",
                               response.status_code, response.text[:200])
                return

            data = response.json()
            temp = data.get("main", {})
            weather = data.get("weather", [{}])[0]
            rain = data.get("rain", {})
            condition_en = weather.get("main", "Unknown")
            condition = self.CONDITION_MAP.get(condition_en, condition_en)

            today = time.strftime("%Y-%m-%d")
            now = time.time()

            conn = self._conn()
            # Existierenden Eintrag updaten (min/max anpassen)
            existing = conn.execute(
                "SELECT * FROM weather_data WHERE date = ?", (today,)
            ).fetchone()

            if existing:
                t_min = min(existing["temp_min"], temp.get("temp_min", temp.get("temp", 0)))
                t_max = max(existing["temp_max"], temp.get("temp_max", temp.get("temp", 0)))
                t_avg = (t_min + t_max) / 2
                rain_total = existing["rain_mm"] + rain.get("1h", 0)
                conn.execute("""
                    UPDATE weather_data SET
                        temp_min = ?, temp_max = ?, temp_avg = ?,
                        condition = ?, humidity = ?, rain_mm = ?, fetched_at = ?
                    WHERE date = ?
                """, (t_min, t_max, t_avg, condition,
                      temp.get("humidity", 0), rain_total, now, today))
            else:
                t = temp.get("temp", 0)
                conn.execute("""
                    INSERT INTO weather_data
                        (date, temp_min, temp_max, temp_avg, condition,
                         humidity, rain_mm, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (today, temp.get("temp_min", t), temp.get("temp_max", t),
                      t, condition, temp.get("humidity", 0),
                      rain.get("1h", 0), now))

            conn.commit()
            conn.close()
            logger.info("Wetter gespeichert: %s, %.1f°C, %s",
                         today, temp.get("temp", 0), condition)

        except Exception as e:
            logger.error("Wetter-Fetch Fehler: %s", e)

    # ============================================================
    # Abfragen
    # ============================================================

    def get_today(self) -> Optional[dict]:
        """Heutiges Wetter."""
        today = time.strftime("%Y-%m-%d")
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM weather_data WHERE date = ?", (today,)
        ).fetchone()
        conn.close()
        if not row:
            return None
        d = dict(row)
        d["emoji"] = self.CONDITION_EMOJI.get(d.get("condition", ""), "")
        return d

    def get_history(self, days=30) -> list:
        """Wetterdaten der letzten X Tage."""
        conn = self._conn()
        rows = conn.execute("""
            SELECT * FROM weather_data
            ORDER BY date DESC LIMIT ?
        """, (days,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_correlation(self, days=30) -> list:
        """Korreliert Wetter mit Verkaufsdaten."""
        conn = self._conn()
        rows = conn.execute("""
            SELECT w.date, w.temp_avg, w.condition, w.humidity, w.rain_mm,
                   COALESCE(d.total_vends, 0) as total_vends,
                   COALESCE(d.total_revenue, 0) as total_revenue
            FROM weather_data w
            LEFT JOIN (
                SELECT date(timestamp, 'unixepoch', 'localtime') as vend_date,
                       COUNT(*) as total_vends,
                       SUM(price_cents) as total_revenue
                FROM vend_events WHERE success = 1
                GROUP BY vend_date
            ) d ON w.date = d.vend_date
            WHERE w.date >= date('now', ? || ' days')
            ORDER BY w.date DESC
        """, (str(-days),)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_condition_summary(self, days=90) -> list:
        """Durchschnittliche Verkaeufe/Umsatz pro Wetterlage."""
        conn = self._conn()
        rows = conn.execute("""
            SELECT w.condition,
                   COUNT(DISTINCT w.date) as days_count,
                   COALESCE(AVG(d.total_vends), 0) as avg_vends,
                   COALESCE(AVG(d.total_revenue), 0) as avg_revenue,
                   AVG(w.temp_avg) as avg_temp
            FROM weather_data w
            LEFT JOIN (
                SELECT date(timestamp, 'unixepoch', 'localtime') as vend_date,
                       COUNT(*) as total_vends,
                       SUM(price_cents) as total_revenue
                FROM vend_events WHERE success = 1
                GROUP BY vend_date
            ) d ON w.date = d.vend_date
            WHERE w.date >= date('now', ? || ' days')
            GROUP BY w.condition
            ORDER BY avg_vends DESC
        """, (str(-days),)).fetchall()
        conn.close()

        result = []
        for r in rows:
            d = dict(r)
            d["avg_vends"] = round(d["avg_vends"], 1)
            d["avg_revenue"] = round(d["avg_revenue"], 0)
            d["avg_temp"] = round(d["avg_temp"], 1) if d["avg_temp"] else 0
            d["emoji"] = self.CONDITION_EMOJI.get(d["condition"], "")
            result.append(d)
        return result
