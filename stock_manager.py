"""
Fuellstand-Manager
==================
Verwaltet geschaetzte Fuellstaende pro Slot.
User traegt ein wie voll ein Slot ist, bei jedem Verkauf -1.
"""

import logging
import sqlite3
import time
from typing import Optional

from config import DB_PATH

logger = logging.getLogger("stock")


def init_stock_tables(db_path: str = DB_PATH):
    """Erstellt die stock_levels Tabelle."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_levels (
            slot_id INTEGER PRIMARY KEY,
            initial_count INTEGER NOT NULL DEFAULT 0,
            current_count INTEGER NOT NULL DEFAULT 0,
            max_capacity INTEGER NOT NULL DEFAULT 15,
            last_refill REAL,
            last_vend REAL,
            updated_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


class StockManager:
    """Verwaltet Fuellstaende fuer Automaten-Slots."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        init_stock_tables(db_path)
        logger.info("StockManager initialisiert")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def set_stock(self, slot_id: int, count: int,
                  max_capacity: int = 15) -> dict:
        """Setzt den Fuellstand fuer einen Slot."""
        now = time.time()
        conn = self._conn()
        conn.execute("""
            INSERT INTO stock_levels (slot_id, initial_count, current_count,
                                      max_capacity, last_refill, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(slot_id) DO UPDATE SET
                initial_count = excluded.initial_count,
                current_count = excluded.current_count,
                max_capacity = excluded.max_capacity,
                last_refill = excluded.last_refill,
                updated_at = excluded.updated_at
        """, (slot_id, count, count, max_capacity, now, now))
        conn.commit()

        row = conn.execute(
            "SELECT * FROM stock_levels WHERE slot_id = ?", (slot_id,)
        ).fetchone()
        conn.close()

        logger.info("Fuellstand gesetzt: Slot #%d = %d/%d", slot_id, count, max_capacity)
        return dict(row) if row else {}

    def decrement_stock(self, slot_id: int) -> Optional[int]:
        """Reduziert den Fuellstand um 1 nach einem Verkauf.
        Gibt den neuen Stand zurueck, oder None wenn Slot nicht getrackt."""
        now = time.time()
        conn = self._conn()

        row = conn.execute(
            "SELECT current_count FROM stock_levels WHERE slot_id = ?",
            (slot_id,)
        ).fetchone()

        if not row:
            conn.close()
            return None

        new_count = max(0, row["current_count"] - 1)
        conn.execute("""
            UPDATE stock_levels
            SET current_count = ?, last_vend = ?, updated_at = ?
            WHERE slot_id = ?
        """, (new_count, now, now, slot_id))
        conn.commit()
        conn.close()

        logger.debug("Fuellstand Slot #%d: %d -> %d", slot_id, row["current_count"], new_count)
        return new_count

    def refill_slot(self, slot_id: int, count: int) -> Optional[dict]:
        """Fuellt einen Slot auf (Reset auf neuen Wert)."""
        now = time.time()
        conn = self._conn()

        row = conn.execute(
            "SELECT * FROM stock_levels WHERE slot_id = ?", (slot_id,)
        ).fetchone()

        if not row:
            conn.close()
            return None

        max_cap = row["max_capacity"]
        if count > max_cap:
            max_cap = count

        conn.execute("""
            UPDATE stock_levels
            SET initial_count = ?, current_count = ?, max_capacity = ?,
                last_refill = ?, updated_at = ?
            WHERE slot_id = ?
        """, (count, count, max_cap, now, now, slot_id))
        conn.commit()

        row = conn.execute(
            "SELECT * FROM stock_levels WHERE slot_id = ?", (slot_id,)
        ).fetchone()
        conn.close()

        logger.info("Slot #%d aufgefuellt: %d Stueck", slot_id, count)
        return dict(row) if row else None

    def get_all_levels(self) -> list:
        """Gibt alle Fuellstaende zurueck, angereichert mit Produktnamen."""
        conn = self._conn()
        rows = conn.execute("""
            SELECT s.*, p.product_name, p.price_cents as product_price,
                   p.category, p.article_number, p.size
            FROM stock_levels s
            LEFT JOIN product_mappings p ON s.slot_id = p.slot_id
                AND p.active_until IS NULL
            ORDER BY s.current_count ASC, s.slot_id ASC
        """).fetchall()
        conn.close()

        result = []
        for r in rows:
            d = dict(r)
            d["percent"] = round(
                (d["current_count"] / d["max_capacity"] * 100)
                if d["max_capacity"] > 0 else 0
            )
            d["last_refill_str"] = (
                time.strftime("%d.%m.%Y %H:%M", time.localtime(d["last_refill"]))
                if d["last_refill"] else "-"
            )
            result.append(d)
        return result

    def get_level(self, slot_id: int) -> Optional[dict]:
        """Gibt den Fuellstand fuer einen einzelnen Slot zurueck."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM stock_levels WHERE slot_id = ?", (slot_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_low_stock(self, threshold: int = 3) -> list:
        """Gibt Slots mit niedrigem Fuellstand zurueck."""
        conn = self._conn()
        rows = conn.execute("""
            SELECT s.*, p.product_name
            FROM stock_levels s
            LEFT JOIN product_mappings p ON s.slot_id = p.slot_id
                AND p.active_until IS NULL
            WHERE s.current_count <= ?
            ORDER BY s.current_count ASC
        """, (threshold,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def remove_slot(self, slot_id: int) -> bool:
        """Entfernt einen Slot aus dem Tracking."""
        conn = self._conn()
        conn.execute("DELETE FROM stock_levels WHERE slot_id = ?", (slot_id,))
        conn.commit()
        conn.close()
        return True
