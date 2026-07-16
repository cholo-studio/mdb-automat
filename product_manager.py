"""
Produkt-Manager: Slot-zu-Produktname-Zuordnung mit Zeitverlauf
==============================================================
Jeder Automaten-Slot (z.B. #37) kann ueber die Zeit verschiedene
Produkte enthalten. Der Manager trackt welches Produkt wann wo war,
sodass auch historische Verkaeufe korrekt zugeordnet werden.

Beispiel:
  Slot 37: "Mars Riegel"    (seit 01.01.2026)
  Slot 37: "Snickers"       (ab 15.03.2026 → Mars wird automatisch beendet)
"""

import logging
import sqlite3
import time
from typing import Optional

from config import DB_PATH

logger = logging.getLogger("product_manager")


# ============================================================
# DB-Schema
# ============================================================
def init_product_tables(db_path: str = DB_PATH):
    """Erstellt die Produktverwaltungs-Tabellen."""
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # Temporale Slot-zu-Produkt-Zuordnung
    c.execute("""
        CREATE TABLE IF NOT EXISTS product_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            price_cents INTEGER,
            category TEXT DEFAULT '',
            article_number TEXT DEFAULT '',
            size TEXT DEFAULT '',
            active_from REAL NOT NULL,
            active_until REAL,
            notes TEXT DEFAULT ''
        )
    """)

    # Migration: Spalten nachtraeglich hinzufuegen (falls Tabelle schon existiert)
    for col, coltype in [("article_number", "TEXT DEFAULT ''"), ("size", "TEXT DEFAULT ''")]:
        try:
            c.execute(f"ALTER TABLE product_mappings ADD COLUMN {col} {coltype}")
        except Exception:
            pass  # Spalte existiert bereits

    # Produktkatalog (alle jemals bekannten Produkte)
    c.execute("""
        CREATE TABLE IF NOT EXISTS product_catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT DEFAULT '',
            article_number TEXT DEFAULT '',
            size TEXT DEFAULT '',
            default_price_cents INTEGER,
            created_at REAL NOT NULL
        )
    """)

    for col, coltype in [("article_number", "TEXT DEFAULT ''"), ("size", "TEXT DEFAULT ''")]:
        try:
            c.execute(f"ALTER TABLE product_catalog ADD COLUMN {col} {coltype}")
        except Exception:
            pass

    c.execute("CREATE INDEX IF NOT EXISTS idx_pm_slot ON product_mappings(slot_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pm_active ON product_mappings(active_from, active_until)")

    conn.commit()
    conn.close()
    logger.info("Produkt-Tabellen initialisiert")


# ============================================================
# Produkt-Manager Klasse
# ============================================================
class ProductManager:
    """Verwaltet Slot-zu-Produkt-Zuordnungen mit Zeitverlauf."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        init_product_tables(db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- Lookup ----

    def get_product_name(self, slot_id: int, timestamp: float = None) -> Optional[str]:
        """Gibt den Produktnamen fuer einen Slot zu einem Zeitpunkt zurueck.
        Wenn kein Zeitpunkt angegeben → aktuell aktives Produkt.
        """
        if timestamp is None:
            timestamp = time.time()
        conn = self._conn()
        row = conn.execute(
            "SELECT product_name FROM product_mappings "
            "WHERE slot_id = ? AND active_from <= ? "
            "AND (active_until IS NULL OR active_until > ?) "
            "ORDER BY active_from DESC LIMIT 1",
            (slot_id, timestamp, timestamp)
        ).fetchone()
        conn.close()
        if row:
            return row["product_name"]
        return None

    def get_product_info(self, slot_id: int, timestamp: float = None) -> Optional[dict]:
        """Gibt alle Infos zum Produkt in einem Slot zurueck."""
        if timestamp is None:
            timestamp = time.time()
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM product_mappings "
            "WHERE slot_id = ? AND active_from <= ? "
            "AND (active_until IS NULL OR active_until > ?) "
            "ORDER BY active_from DESC LIMIT 1",
            (slot_id, timestamp, timestamp)
        ).fetchone()
        conn.close()
        if row:
            return dict(row)
        return None

    # ---- Aktive Zuordnungen ----

    def get_active_mappings(self) -> list:
        """Gibt alle aktuell aktiven Slot-Zuordnungen zurueck."""
        now = time.time()
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM product_mappings "
            "WHERE active_from <= ? AND (active_until IS NULL OR active_until > ?) "
            "ORDER BY slot_id ASC",
            (now, now)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_all_mappings(self) -> list:
        """Gibt alle Zuordnungen zurueck (auch historische)."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM product_mappings ORDER BY slot_id ASC, active_from DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ---- Zuordnung setzen ----

    def set_product(self, slot_id: int, product_name: str,
                    price_cents: int = None, category: str = "",
                    article_number: str = "", size: str = "",
                    notes: str = "") -> dict:
        """Setzt ein Produkt fuer einen Slot.
        Wenn der Slot bereits ein aktives Produkt hat, wird das alte
        automatisch beendet (active_until = jetzt).
        """
        now = time.time()
        conn = self._conn()

        # Bisheriges aktives Mapping beenden
        conn.execute(
            "UPDATE product_mappings SET active_until = ? "
            "WHERE slot_id = ? AND active_until IS NULL",
            (now, slot_id)
        )

        # Neues Mapping anlegen
        conn.execute(
            "INSERT INTO product_mappings "
            "(slot_id, product_name, price_cents, category, article_number, size, active_from, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (slot_id, product_name, price_cents, category, article_number, size, now, notes)
        )

        # Produktkatalog aktualisieren
        conn.execute(
            "INSERT OR IGNORE INTO product_catalog (name, category, article_number, size, default_price_cents, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (product_name, category, article_number, size, price_cents, now)
        )

        conn.commit()

        # Neues Mapping zurueckgeben
        row = conn.execute(
            "SELECT * FROM product_mappings "
            "WHERE slot_id = ? AND active_until IS NULL "
            "ORDER BY active_from DESC LIMIT 1",
            (slot_id,)
        ).fetchone()
        conn.close()

        logger.info("Produkt gesetzt: Slot %d → %s", slot_id, product_name)
        return dict(row) if row else {}

    def update_product(self, slot_id: int, product_name: str = None,
                       price_cents: int = None, category: str = None,
                       article_number: str = None, size: str = None,
                       notes: str = None) -> Optional[dict]:
        """Bearbeitet das aktive Mapping eines Slots direkt (ohne neue Historie).
        Nur uebergebene Felder werden geaendert.
        """
        conn = self._conn()

        # Aktuelles aktives Mapping holen
        row = conn.execute(
            "SELECT * FROM product_mappings "
            "WHERE slot_id = ? AND active_until IS NULL",
            (slot_id,)
        ).fetchone()

        if not row:
            conn.close()
            return None

        # Felder aktualisieren (nur was uebergeben wurde)
        new_name = product_name if product_name is not None else row["product_name"]
        new_price = price_cents if price_cents is not None else row["price_cents"]
        new_cat = category if category is not None else row["category"]
        new_art = article_number if article_number is not None else row["article_number"]
        new_size = size if size is not None else row["size"]
        new_notes = notes if notes is not None else row["notes"]

        conn.execute(
            "UPDATE product_mappings SET product_name = ?, price_cents = ?, "
            "category = ?, article_number = ?, size = ?, notes = ? WHERE id = ?",
            (new_name, new_price, new_cat, new_art, new_size, new_notes, row["id"])
        )

        # Katalog aktualisieren
        if product_name is not None:
            conn.execute(
                "INSERT OR IGNORE INTO product_catalog (name, category, article_number, size, default_price_cents, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (new_name, new_cat, new_art, new_size, new_price, time.time())
            )

        conn.commit()

        updated = conn.execute(
            "SELECT * FROM product_mappings WHERE id = ?", (row["id"],)
        ).fetchone()
        conn.close()

        logger.info("Produkt bearbeitet: Slot %d → %s", slot_id, new_name)
        return dict(updated) if updated else None

    def remove_product(self, slot_id: int) -> bool:
        """Entfernt die aktive Zuordnung eines Slots (beendet sie)."""
        now = time.time()
        conn = self._conn()
        cursor = conn.execute(
            "UPDATE product_mappings SET active_until = ? "
            "WHERE slot_id = ? AND active_until IS NULL",
            (now, slot_id)
        )
        conn.commit()
        conn.close()
        if cursor.rowcount > 0:
            logger.info("Produkt entfernt aus Slot %d", slot_id)
            return True
        return False

    # ---- Slot-Historie ----

    def get_slot_history(self, slot_id: int) -> list:
        """Gibt die komplette Historie eines Slots zurueck."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM product_mappings "
            "WHERE slot_id = ? ORDER BY active_from DESC",
            (slot_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ---- Produktkatalog ----

    def get_catalog(self) -> list:
        """Gibt den Produktkatalog zurueck (alle jemals verwendeten Produkte)."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM product_catalog ORDER BY name ASC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ---- Bulk Import ----

    def bulk_set(self, mappings: list) -> int:
        """Setzt mehrere Zuordnungen auf einmal.
        mappings = [{"slot_id": 37, "product_name": "Mars", "price_cents": 120}, ...]
        """
        count = 0
        for m in mappings:
            self.set_product(
                slot_id=m["slot_id"],
                product_name=m["product_name"],
                price_cents=m.get("price_cents"),
                category=m.get("category", ""),
                article_number=m.get("article_number", ""),
                size=m.get("size", ""),
                notes=m.get("notes", ""),
            )
            count += 1
        return count

    # ---- Enrichment ----

    def enrich_vend(self, vend_dict: dict) -> dict:
        """Reichert einen Verkaufs-Eintrag mit Produktname an."""
        product_id = vend_dict.get("product_id")
        timestamp = vend_dict.get("timestamp", time.time())
        if product_id is not None:
            name = self.get_product_name(product_id, timestamp)
            vend_dict["product_name"] = name or f"Slot #{product_id}"
        return vend_dict

    def enrich_product_stats(self, stats: list) -> list:
        """Reichert Produkt-Statistiken mit aktuellen Namen an."""
        for item in stats:
            pid = item.get("product_id")
            if pid is not None:
                name = self.get_product_name(pid)
                item["product_name"] = name or f"Slot #{pid}"
        return stats

    # ---- Auto-Detect aus Verkaeufen ----

    def get_unmapped_slots(self) -> list:
        """Findet Slot-IDs die verkauft wurden aber kein Mapping haben."""
        now = time.time()
        conn = self._conn()
        rows = conn.execute("""
            SELECT DISTINCT v.product_id, COUNT(*) as sales, MAX(v.price_cents) as last_price
            FROM vend_events v
            LEFT JOIN product_mappings pm
                ON pm.slot_id = v.product_id
                AND pm.active_from <= v.timestamp
                AND (pm.active_until IS NULL OR pm.active_until > v.timestamp)
            WHERE pm.id IS NULL AND v.success = 1
            GROUP BY v.product_id
            ORDER BY sales DESC
        """).fetchall()
        conn.close()
        return [{"slot_id": r[0], "sales": r[1], "last_price_cents": r[2]} for r in rows]
