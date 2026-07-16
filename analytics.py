"""
Sales Analytics
===============
Verkaufszeiten-Analyse, Produktgeschwindigkeit, Preisempfehlungen.
Arbeitet auf bestehenden vend_events und product_mappings Daten.
"""

import logging
import sqlite3
import time
from typing import Optional

from config import DB_PATH

logger = logging.getLogger("analytics")


class SalesAnalytics:
    """Analyse-Engine fuer Verkaufsdaten."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ============================================================
    # Verkaufszeiten (Peak Hours)
    # ============================================================

    def get_peak_hours(self, slot_id: Optional[int] = None,
                       days: int = 30) -> list:
        """Verkaeufe nach Stunde (0-23) gruppiert.
        Gibt 24 Eintraege zurueck, auch fuer Stunden ohne Verkaeufe."""
        conn = self._conn()
        cutoff = time.time() - (days * 86400)

        query = (
            "SELECT CAST(strftime('%H', datetime(timestamp, 'unixepoch', 'localtime')) AS INTEGER) as hour, "
            "COUNT(*) as count "
            "FROM vend_events WHERE success = 1 AND timestamp > ? "
        )
        params = [cutoff]

        if slot_id is not None:
            query += "AND product_id = ? "
            params.append(slot_id)

        query += "GROUP BY hour ORDER BY hour"
        rows = conn.execute(query, params).fetchall()
        conn.close()

        hour_map = {r["hour"]: r["count"] for r in rows}
        return [{"hour": h, "count": hour_map.get(h, 0)} for h in range(24)]

    def get_peak_hours_by_product(self, days: int = 30) -> dict:
        """Verkaufszeiten aufgeschluesselt nach Produkt."""
        conn = self._conn()
        cutoff = time.time() - (days * 86400)

        rows = conn.execute("""
            SELECT v.product_id,
                   CAST(strftime('%H', datetime(v.timestamp, 'unixepoch', 'localtime')) AS INTEGER) as hour,
                   COUNT(*) as count,
                   p.product_name
            FROM vend_events v
            LEFT JOIN product_mappings p ON v.product_id = p.slot_id
                AND p.active_until IS NULL
            WHERE v.success = 1 AND v.timestamp > ?
            GROUP BY v.product_id, hour
            ORDER BY v.product_id, hour
        """, (cutoff,)).fetchall()
        conn.close()

        result = {}
        for r in rows:
            pid = r["product_id"]
            if pid not in result:
                result[pid] = {
                    "product_id": pid,
                    "product_name": r["product_name"] or f"Slot #{pid}",
                    "hours": [0] * 24,
                }
            result[pid]["hours"][r["hour"]] = r["count"]

        return list(result.values())

    # ============================================================
    # Verkaufsgeschwindigkeit
    # ============================================================

    def get_product_velocity(self, days: int = 14) -> list:
        """Verkaufsgeschwindigkeit pro Produkt (Verkaeufe/Tag)."""
        conn = self._conn()
        cutoff = time.time() - (days * 86400)

        rows = conn.execute("""
            SELECT v.product_id,
                   COUNT(*) as total_sales,
                   SUM(v.price_cents) as total_revenue,
                   COUNT(DISTINCT date(v.timestamp, 'unixepoch', 'localtime')) as active_days,
                   p.product_name,
                   p.price_cents as current_price
            FROM vend_events v
            LEFT JOIN product_mappings p ON v.product_id = p.slot_id
                AND p.active_until IS NULL
            WHERE v.success = 1 AND v.timestamp > ?
            GROUP BY v.product_id
            ORDER BY total_sales DESC
        """, (cutoff,)).fetchall()
        conn.close()

        result = []
        for r in rows:
            active = max(r["active_days"], 1)
            result.append({
                "product_id": r["product_id"],
                "product_name": r["product_name"] or f"Slot #{r['product_id']}",
                "total_sales": r["total_sales"],
                "total_revenue_cents": r["total_revenue"] or 0,
                "active_days": r["active_days"],
                "avg_per_day": round(r["total_sales"] / active, 2),
                "current_price_cents": r["current_price"],
            })
        return result

    # ============================================================
    # Preisempfehlungen
    # ============================================================

    def get_price_recommendations(self, days: int = 14) -> list:
        """Generiert Preisempfehlungen basierend auf Verkaufsgeschwindigkeit."""
        velocity = self.get_product_velocity(days)
        if not velocity:
            return []

        # Durchschnittliche Geschwindigkeit berechnen
        total_avg = sum(p["avg_per_day"] for p in velocity)
        avg_velocity = total_avg / len(velocity) if velocity else 0

        recommendations = []
        for p in velocity:
            ratio = p["avg_per_day"] / avg_velocity if avg_velocity > 0 else 1
            current = p["current_price_cents"]

            if ratio < 0.3 and p["total_sales"] > 0:
                rec = "reduce"
                reason = "Sehr schwacher Absatz — Preissenkung empfohlen"
                change_pct = -15
            elif ratio < 0.6:
                rec = "reduce_slight"
                reason = "Unterdurchschnittlicher Absatz"
                change_pct = -10
            elif ratio > 2.0:
                rec = "increase"
                reason = "Hohe Nachfrage — Preissteigerung moeglich"
                change_pct = 10
            elif ratio > 1.5:
                rec = "increase_slight"
                reason = "Ueberdurchschnittlicher Absatz"
                change_pct = 5
            else:
                rec = "keep"
                reason = "Normaler Absatz"
                change_pct = 0

            suggested = None
            if current and change_pct != 0:
                raw = current * (1 + change_pct / 100)
                # Auf 10 Cent runden
                suggested = round(raw / 10) * 10

            recommendations.append({
                "product_id": p["product_id"],
                "product_name": p["product_name"],
                "current_price_cents": current,
                "avg_per_day": p["avg_per_day"],
                "total_sales": p["total_sales"],
                "velocity_ratio": round(ratio, 2),
                "recommendation": rec,
                "reason": reason,
                "suggested_change_percent": change_pct,
                "suggested_price_cents": suggested,
            })

        return sorted(recommendations, key=lambda r: r["velocity_ratio"])
