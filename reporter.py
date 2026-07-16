"""
Report-Generator
================
Erstellt Tages- und Wochenberichte aus der SQLite-Datenbank.
Optional mit LLM-Zusammenfassung.
"""

import logging
import sqlite3
import time
from typing import Optional

from config import DB_PATH
from llm import OllamaClient
from notifier import Alert
from analytics import SalesAnalytics

logger = logging.getLogger("reporter")


class Reporter:
    """Generiert Tages- und Wochenberichte."""

    def __init__(self, llm: Optional[OllamaClient] = None, db_path: str = DB_PATH):
        self.llm = llm
        self.db_path = db_path
        self.analytics = SalesAnalytics(db_path)

    def daily_report(self, date: Optional[str] = None) -> Alert:
        """Erstellt einen Tagesbericht."""
        if not date:
            date = time.strftime("%Y-%m-%d")

        stats = self._get_daily_stats(date)
        payment_breakdown = self._get_payment_breakdown(date)
        top_product = self._get_top_product(date)

        stats["payment_breakdown"] = payment_breakdown
        if top_product:
            stats["top_product_id"] = top_product[0]
            stats["top_product_count"] = top_product[1]

        # LLM-Analyse (optional)
        analysis = ""
        if self.llm:
            analysis = self.llm.analyze_day(stats)

        total_sales = stats.get("total_sales", 0)
        revenue = stats.get("revenue_euro", "0.00")

        message_parts = [
            f"Verkaeufe: {total_sales}",
            f"Umsatz: {revenue} EUR",
        ]
        if top_product:
            message_parts.append(f"Top-Produkt: #{top_product[0]} ({top_product[1]}x)")
        if payment_breakdown:
            parts = [f"{k}: {v}" for k, v in payment_breakdown.items()]
            message_parts.append(f"Zahlungen: {', '.join(parts)}")
        if stats.get("errors", 0) > 0:
            message_parts.append(f"Fehler: {stats['errors']}")
        # Top-3 Verkaufsstunden
        try:
            peak = self.analytics.get_peak_hours(days=1)
            top3 = sorted(peak, key=lambda h: h["count"], reverse=True)[:3]
            top3 = [h for h in top3 if h["count"] > 0]
            if top3:
                hours_str = ", ".join(f"{h['hour']}:00 ({h['count']}x)" for h in top3)
                message_parts.append(f"Top-Stunden: {hours_str}")
        except Exception:
            pass

        if analysis:
            message_parts.append(f"\nAnalyse: {analysis}")

        return Alert(
            type="daily_report",
            severity="info",
            title=f"Tagesbericht {date}: {revenue} EUR ({total_sales} Verkaeufe)",
            message="\n".join(message_parts),
            data=stats,
        )

    def weekly_report(self) -> Alert:
        """Erstellt einen Wochenbericht (letzte 7 Tage)."""
        stats = self._get_weekly_stats()

        # LLM-Analyse (optional)
        analysis = ""
        if self.llm:
            analysis = self.llm.analyze_week(stats)

        total_sales = stats.get("total_sales", 0)
        revenue = stats.get("revenue_euro", "0.00")
        trend = stats.get("trend_percent", 0)

        trend_str = "stabil"
        if trend > 5:
            trend_str = f"+{trend:.0f}% vs. Vorwoche"
        elif trend < -5:
            trend_str = f"{trend:.0f}% vs. Vorwoche"

        message_parts = [
            f"Verkaeufe: {total_sales}",
            f"Umsatz: {revenue} EUR",
            f"Trend: {trend_str}",
            f"Bester Tag: {stats.get('best_day', '?')} ({stats.get('best_day_revenue', '0.00')} EUR)",
            f"Tagesdurchschnitt: {stats.get('avg_daily_revenue', '0.00')} EUR",
        ]
        # Top-3 Preisempfehlungen
        try:
            recs = self.analytics.get_price_recommendations(days=14)
            action_recs = [r for r in recs if r["recommendation"] != "keep"][:3]
            if action_recs:
                rec_lines = []
                for r in action_recs:
                    arrow = "\u2191" if "increase" in r["recommendation"] else "\u2193"
                    name = r["product_name"]
                    pct = r["suggested_change_percent"]
                    rec_lines.append(f"  {arrow} {name} ({pct:+d}%)")
                message_parts.append("Preisempfehlungen:\n" + "\n".join(rec_lines))
        except Exception:
            pass

        if analysis:
            message_parts.append(f"\nAnalyse: {analysis}")

        return Alert(
            type="weekly_report",
            severity="info",
            title=f"Wochenbericht: {revenue} EUR ({total_sales} Verkaeufe, {trend_str})",
            message="\n".join(message_parts),
            data=stats,
        )

    # ============================================================
    # Datenbank-Abfragen
    # ============================================================

    def _get_daily_stats(self, date: str) -> dict:
        """Holt Tagesstatistiken aus SQLite."""
        try:
            conn = sqlite3.connect(self.db_path)

            cursor = conn.execute(
                "SELECT total_sales, total_revenue_cents, errors "
                "FROM daily_stats WHERE date = ?", (date,)
            )
            row = cursor.fetchone()

            if row:
                total_sales = row[0]
                revenue_cents = row[1]
                errors = row[2]
            else:
                total_sales = 0
                revenue_cents = 0
                errors = 0

            avg_price = revenue_cents / total_sales if total_sales > 0 else 0

            conn.close()
            return {
                "date": date,
                "total_sales": total_sales,
                "revenue_cents": revenue_cents,
                "revenue_euro": f"{revenue_cents / 100:.2f}",
                "avg_price_euro": f"{avg_price / 100:.2f}",
                "errors": errors,
            }
        except Exception as e:
            logger.error("DB-Fehler (daily): %s", e)
            return {"date": date, "total_sales": 0, "revenue_euro": "0.00", "errors": 0}

    def _get_payment_breakdown(self, date: str) -> dict:
        """Zahlungsarten-Aufschluesselung fuer einen Tag."""
        try:
            conn = sqlite3.connect(self.db_path)
            start_ts = time.mktime(time.strptime(date, "%Y-%m-%d"))
            end_ts = start_ts + 86400

            cursor = conn.execute(
                "SELECT payment_method, COUNT(*) FROM vend_events "
                "WHERE timestamp >= ? AND timestamp < ? AND success = 1 "
                "GROUP BY payment_method", (start_ts, end_ts)
            )
            breakdown = {row[0]: row[1] for row in cursor.fetchall()}
            conn.close()
            return breakdown
        except Exception:
            return {}

    def _get_top_product(self, date: str) -> Optional[tuple]:
        """Meistverkauftes Produkt eines Tages."""
        try:
            conn = sqlite3.connect(self.db_path)
            start_ts = time.mktime(time.strptime(date, "%Y-%m-%d"))
            end_ts = start_ts + 86400

            cursor = conn.execute(
                "SELECT product_id, COUNT(*) as cnt FROM vend_events "
                "WHERE timestamp >= ? AND timestamp < ? AND success = 1 "
                "GROUP BY product_id ORDER BY cnt DESC LIMIT 1",
                (start_ts, end_ts)
            )
            row = cursor.fetchone()
            conn.close()
            return (row[0], row[1]) if row else None
        except Exception:
            return None

    def _get_weekly_stats(self) -> dict:
        """Aggregierte Statistiken der letzten 7 Tage."""
        try:
            conn = sqlite3.connect(self.db_path)

            # Letzte 7 Tage
            cursor = conn.execute(
                "SELECT date, total_sales, total_revenue_cents "
                "FROM daily_stats ORDER BY date DESC LIMIT 7"
            )
            this_week = cursor.fetchall()

            # Vorwoche (Tage 8-14)
            cursor = conn.execute(
                "SELECT date, total_sales, total_revenue_cents "
                "FROM daily_stats ORDER BY date DESC LIMIT 7 OFFSET 7"
            )
            last_week = cursor.fetchall()
            conn.close()

            if not this_week:
                return {
                    "total_sales": 0, "revenue_euro": "0.00",
                    "trend_percent": 0, "avg_daily_revenue": "0.00",
                    "best_day": "-", "worst_day": "-",
                    "best_day_revenue": "0.00", "worst_day_revenue": "0.00",
                }

            total_sales = sum(r[1] for r in this_week)
            total_revenue = sum(r[2] for r in this_week)
            last_week_revenue = sum(r[2] for r in last_week) if last_week else 0

            trend = 0
            if last_week_revenue > 0:
                trend = ((total_revenue - last_week_revenue) / last_week_revenue) * 100

            best = max(this_week, key=lambda r: r[2])
            worst = min(this_week, key=lambda r: r[2])
            avg_daily = total_revenue / len(this_week) if this_week else 0

            return {
                "total_sales": total_sales,
                "revenue_cents": total_revenue,
                "revenue_euro": f"{total_revenue / 100:.2f}",
                "trend_percent": round(trend, 1),
                "avg_daily_revenue": f"{avg_daily / 100:.2f}",
                "best_day": best[0],
                "best_day_revenue": f"{best[2] / 100:.2f}",
                "worst_day": worst[0],
                "worst_day_revenue": f"{worst[2] / 100:.2f}",
                "days_count": len(this_week),
            }
        except Exception as e:
            logger.error("DB-Fehler (weekly): %s", e)
            return {
                "total_sales": 0, "revenue_euro": "0.00",
                "trend_percent": 0, "avg_daily_revenue": "0.00",
            }
