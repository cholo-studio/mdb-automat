"""
Web-Dashboard fuer MDB Sniffer
================================
Flask-basierter Webserver mit REST-API und Live-Dashboard.
Zugreifbar ueber Tailscale von ueberall.
"""

import csv
import io
import os
import time
import json
import shutil
import subprocess
from datetime import timedelta
from flask import (Flask, render_template, jsonify, request, Response,
                   session, redirect, url_for)
from mdb_sniffer import MDBSniffer
from agent import AutomatCEO
import config
import auth
from auth import login_required, is_authenticated, check_password, load_or_create_secret_key
from config import (
    WEB_HOST, WEB_PORT, SECRET_KEY, AUTOMAT_NAME, AUTOMAT_STANDORT,
    WEATHER_API_KEY, WEATHER_CITY, WEATHER_ENABLED, WEATHER_FETCH_INTERVAL,
)
from analytics import SalesAnalytics
from weather_manager import WeatherManager
from telegram_bot import TelegramBot
from ad_manager import AdManager

# Flask App
app = Flask(__name__)
app.secret_key = load_or_create_secret_key()   # starker Key aus Datei
app.permanent_session_lifetime = timedelta(hours=config.SESSION_HOURS)
# Upload-Groesse hart begrenzen (Anti-DoS: Pi hat nur 4 GB RAM)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB


@app.errorhandler(413)
def _too_large(e):
    return jsonify({"error": "Datei zu gross (max. 5 MB)"}), 413


# ============================================================
# Authentifizierung  (globales Gate vor allen Routen)
# ============================================================
# Ohne Login erreichbar: Login/Logout, Health-Check (Monitoring), Ad-Pull (Display).
_PUBLIC_EXACT = {"/login", "/logout", "/api/health", "/api/ads/current"}


@app.before_request
def _require_login():
    if not config.AUTH_ENABLED:
        return
    p = request.path
    if p in _PUBLIC_EXACT or p.startswith("/static/"):
        return
    if is_authenticated():
        return
    if p.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect(url_for("login", next=p))


@app.route("/login", methods=["GET", "POST"])
def login():
    if is_authenticated():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if check_password(request.form.get("password", "")):
            session.permanent = True
            session["authenticated"] = True
            nxt = request.args.get("next", "")
            if nxt.startswith("/"):
                return redirect(nxt)
            return redirect(url_for("index"))
        error = "Falsches Passwort."
        time.sleep(1)  # leichtes Bruteforce-Bremsen
    return render_template("login.html", error=error, automat_name=AUTOMAT_NAME)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# Globale Instanzen (werden in main() initialisiert)
sniffer: MDBSniffer = None
ceo: AutomatCEO = None
analytics: SalesAnalytics = None
weather: WeatherManager = None
bot: TelegramBot = None
ad_manager: AdManager = None


# ============================================================
# Webseiten
# ============================================================
@app.route("/")
def index():
    """Hauptseite mit Dashboard."""
    return render_template("index.html",
                           automat_name=AUTOMAT_NAME,
                           standort=AUTOMAT_STANDORT)


@app.route("/system")
def system_page():
    """System-Uebersicht: Architektur, API-Routen, Dateien."""
    return render_template("system.html",
                           automat_name=AUTOMAT_NAME,
                           standort=AUTOMAT_STANDORT)


# ============================================================
# REST API Endpunkte
# ============================================================
@app.route("/api/stats")
def api_stats():
    """Aktuelle Statistiken."""
    return jsonify(sniffer.get_stats())


@app.route("/api/messages")
def api_messages():
    """Letzte MDB-Nachrichten."""
    limit = request.args.get("limit", 50, type=int)
    return jsonify(sniffer.get_recent_messages(limit))


@app.route("/api/vends")
def api_vends():
    """Letzte Verkaeufe."""
    limit = request.args.get("limit", 20, type=int)
    return jsonify(sniffer.get_recent_vends(limit))


@app.route("/api/daily")
def api_daily():
    """Tagesstatistiken."""
    days = request.args.get("days", 30, type=int)
    return jsonify(sniffer.get_daily_stats(days))


@app.route("/api/products")
def api_products():
    """Produkt-Statistiken (mit Produktnamen)."""
    return jsonify(sniffer.get_product_stats())


# ============================================================
# Produkt-Verwaltung API
# ============================================================
@app.route("/api/products/mappings")
def api_product_mappings():
    """Alle aktiven Slot-Zuordnungen."""
    return jsonify(sniffer.products.get_active_mappings())


@app.route("/api/products/mappings/all")
def api_product_mappings_all():
    """Alle Zuordnungen (inkl. historische)."""
    return jsonify(sniffer.products.get_all_mappings())


@app.route("/api/products/mappings", methods=["POST"])
def api_set_product():
    """Produkt fuer einen Slot setzen.
    JSON: {"slot_id": 37, "product_name": "Mars", "price_cents": 120, "category": "Schokoriegel"}
    """
    data = request.get_json()
    if not data or "slot_id" not in data or "product_name" not in data:
        return jsonify({"error": "slot_id und product_name erforderlich"}), 400

    result = sniffer.products.set_product(
        slot_id=data["slot_id"],
        product_name=data["product_name"],
        price_cents=data.get("price_cents"),
        category=data.get("category", ""),
        article_number=data.get("article_number", ""),
        size=data.get("size", ""),
        notes=data.get("notes", ""),
    )
    return jsonify(result)


@app.route("/api/products/mappings/<int:slot_id>", methods=["PUT"])
def api_update_product(slot_id):
    """Aktives Mapping eines Slots bearbeiten (ohne neue Historie).
    JSON: {"product_name": "Snickers", "price_cents": 130, "category": "Schokoriegel"}
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON-Body erforderlich"}), 400

    result = sniffer.products.update_product(
        slot_id=slot_id,
        product_name=data.get("product_name"),
        price_cents=data.get("price_cents"),
        category=data.get("category"),
        article_number=data.get("article_number"),
        size=data.get("size"),
        notes=data.get("notes"),
    )
    if result is None:
        return jsonify({"error": f"Kein aktives Mapping fuer Slot {slot_id}"}), 404
    return jsonify(result)


@app.route("/api/products/mappings/<int:slot_id>", methods=["DELETE"])
def api_remove_product(slot_id):
    """Aktive Zuordnung eines Slots entfernen."""
    removed = sniffer.products.remove_product(slot_id)
    return jsonify({"removed": removed, "slot_id": slot_id})


@app.route("/api/products/mappings/history/<int:slot_id>")
def api_slot_history(slot_id):
    """Historie eines Slots."""
    return jsonify(sniffer.products.get_slot_history(slot_id))


@app.route("/api/products/mappings/bulk", methods=["POST"])
def api_bulk_set():
    """Mehrere Zuordnungen auf einmal setzen.
    JSON: [{"slot_id": 37, "product_name": "Mars"}, ...]
    """
    data = request.get_json()
    if not isinstance(data, list):
        return jsonify({"error": "Liste von Zuordnungen erwartet"}), 400
    count = sniffer.products.bulk_set(data)
    return jsonify({"imported": count})


@app.route("/api/products/catalog")
def api_product_catalog():
    """Produktkatalog (alle jemals verwendeten Produkte)."""
    return jsonify(sniffer.products.get_catalog())


@app.route("/api/products/unmapped")
def api_unmapped_slots():
    """Slots mit Verkaeufen aber ohne Produktzuordnung."""
    return jsonify(sniffer.products.get_unmapped_slots())


# ============================================================
# Fuellstand API
# ============================================================
@app.route("/api/stock")
def api_stock():
    """Alle Fuellstaende."""
    return jsonify(sniffer.stock.get_all_levels())


@app.route("/api/stock/<int:slot_id>", methods=["PUT"])
def api_set_stock(slot_id):
    """Fuellstand fuer einen Slot setzen.
    JSON: {"count": 15, "max_capacity": 20}
    """
    data = request.get_json()
    if not data or "count" not in data:
        return jsonify({"error": "count erforderlich"}), 400
    result = sniffer.stock.set_stock(
        slot_id=slot_id,
        count=data["count"],
        max_capacity=data.get("max_capacity", 15),
    )
    return jsonify(result)


@app.route("/api/stock/<int:slot_id>/refill", methods=["POST"])
def api_refill_stock(slot_id):
    """Slot auffuellen.
    JSON: {"count": 15}
    """
    data = request.get_json()
    if not data or "count" not in data:
        return jsonify({"error": "count erforderlich"}), 400
    result = sniffer.stock.refill_slot(slot_id, data["count"])
    if result is None:
        return jsonify({"error": f"Slot {slot_id} nicht im Tracking"}), 404
    return jsonify(result)


@app.route("/api/stock/<int:slot_id>", methods=["DELETE"])
def api_remove_stock(slot_id):
    """Slot aus Tracking entfernen."""
    sniffer.stock.remove_slot(slot_id)
    return jsonify({"removed": True, "slot_id": slot_id})


# ============================================================
# Analytics API
# ============================================================
@app.route("/api/analytics/peak-hours")
def api_peak_hours():
    """Verkaufszeiten-Analyse (Verkaeufe pro Stunde)."""
    slot_id = request.args.get("slot_id", type=int)
    days = request.args.get("days", 30, type=int)
    group = request.args.get("group_by", "")

    if group == "product":
        return jsonify(analytics.get_peak_hours_by_product(days))
    return jsonify(analytics.get_peak_hours(slot_id=slot_id, days=days))


@app.route("/api/analytics/velocity")
def api_velocity():
    """Verkaufsgeschwindigkeit pro Produkt."""
    days = request.args.get("days", 14, type=int)
    return jsonify(analytics.get_product_velocity(days))


@app.route("/api/analytics/price-recommendations")
def api_price_recommendations():
    """Preisempfehlungen basierend auf Absatzgeschwindigkeit."""
    days = request.args.get("days", 14, type=int)
    return jsonify(analytics.get_price_recommendations(days))


@app.route("/api/health")
def api_health():
    """Health-Check fuer Monitoring."""
    stats = sniffer.get_stats()
    return jsonify({
        "status": "ok",
        "connection": stats["connection_status"],
        "uptime": stats["uptime_str"],
        "timestamp": time.time(),
    })


# ============================================================
# System-Metriken (CPU / Temperatur / RAM / Disk)  — Pi 5
# ============================================================
_cpu_prev = {"total": 0, "idle": 0}


def _cpu_percent():
    """CPU-Auslastung in % ueber das Intervall seit dem letzten Aufruf."""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        total = sum(vals)
        dt = total - _cpu_prev["total"]
        di = idle - _cpu_prev["idle"]
        _cpu_prev["total"], _cpu_prev["idle"] = total, idle
        if dt <= 0:
            return None
        return round(100.0 * (dt - di) / dt, 1)
    except Exception:
        return None


def _cpu_temp():
    """SoC-Temperatur in Grad C (deckt CPU und GPU/VideoCore ab)."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None


def _vcgencmd(*args):
    try:
        out = subprocess.run(["vcgencmd", *args], capture_output=True,
                             text=True, timeout=2)
        return out.stdout.strip()
    except Exception:
        return ""


def _clock_mhz(which):
    s = _vcgencmd("measure_clock", which)  # "frequency(0)=1500000000"
    try:
        return round(int(s.split("=")[1]) / 1_000_000)
    except Exception:
        return None


def _mem():
    try:
        d = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                d[k] = int(rest.strip().split()[0])  # kB
        total = d["MemTotal"]
        avail = d.get("MemAvailable", d.get("MemFree", 0))
        used = total - avail
        return {"total_mb": total // 1024, "used_mb": used // 1024,
                "percent": round(100.0 * used / total, 1)}
    except Exception:
        return {}


@app.route("/api/system")
def api_system():
    """Hardware-Telemetrie des Pi: CPU, SoC-Temperatur, Takt, RAM, Disk, Drossel-Status."""
    temp = _cpu_temp()
    # Drossel-Status (Bitmaske von 'vcgencmd get_throttled')
    raw = _vcgencmd("get_throttled")
    code = 0
    if "=" in raw:
        try:
            code = int(raw.split("=")[1], 16)
        except Exception:
            code = 0
    throttle = {
        "undervoltage_now": bool(code & 0x1),
        "freq_capped_now": bool(code & 0x2),
        "throttled_now": bool(code & 0x4),
        "soft_temp_limit_now": bool(code & 0x8),
        "undervoltage_seit_boot": bool(code & 0x10000),
        "throttled_seit_boot": bool(code & 0x40000),
        "raw": raw,
    }
    # Ampel fuer die Temperatur (Pi 5 drosselt weich ab 80, hart ab 85 C)
    if temp is None:
        temp_status = "unbekannt"
    elif temp >= 80:
        temp_status = "kritisch"
    elif temp >= 70:
        temp_status = "heiss"
    elif temp >= 60:
        temp_status = "warm"
    else:
        temp_status = "ok"

    try:
        du = shutil.disk_usage("/")
        disk = {"total_gb": round(du.total / 1e9, 1),
                "used_gb": round(du.used / 1e9, 1),
                "free_gb": round(du.free / 1e9, 1),
                "percent": round(100.0 * du.used / du.total, 1)}
    except Exception:
        disk = {}

    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
    except Exception:
        up = None

    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = None

    return jsonify({
        "cpu_percent": _cpu_percent(),
        "load": {"1m": load1, "5m": load5, "15m": load15},
        "temp_c": temp,
        "temp_status": temp_status,
        "throttle": throttle,
        "clock": {"cpu_mhz": _clock_mhz("arm"), "gpu_mhz": _clock_mhz("core")},
        "mem": _mem(),
        "disk": disk,
        "uptime_seconds": up,
        "timestamp": time.time(),
    })


# ============================================================
# CEO Agent API Endpunkte
# ============================================================
@app.route("/api/agent/status")
def api_agent_status():
    """Agent-Status: aktiv, LLM, Notifier, letzte Checks."""
    return jsonify(ceo.get_status())


@app.route("/api/agent/alerts")
def api_agent_alerts():
    """Letzte Agent-Alerts."""
    limit = request.args.get("limit", 50, type=int)
    return jsonify(ceo.get_recent_alerts(limit))


@app.route("/api/agent/report/daily", methods=["POST"])
def api_agent_daily_report():
    """Manuell Tagesbericht ausloesen."""
    report = ceo.trigger_daily_report()
    return jsonify(report.to_dict())


@app.route("/api/agent/report/weekly", methods=["POST"])
def api_agent_weekly_report():
    """Manuell Wochenbericht ausloesen."""
    report = ceo.trigger_weekly_report()
    return jsonify(report.to_dict())


@app.route("/api/test/telegram", methods=["POST"])
def api_test_telegram():
    """Test-Nachricht an Telegram senden."""
    result = ceo.telegram.send_test()
    return jsonify(result)


# ============================================================
# Wetter API
# ============================================================
@app.route("/api/weather/today")
def api_weather_today():
    """Heutiges Wetter."""
    if not weather.enabled:
        return jsonify({"enabled": False, "message": "Wetterdaten deaktiviert"})
    data = weather.get_today()
    if data:
        data["enabled"] = True
        return jsonify(data)
    return jsonify({"enabled": True, "message": "Noch keine Daten"})


@app.route("/api/weather/correlation")
def api_weather_correlation():
    """Wetter + Verkaeufe korreliert."""
    days = request.args.get("days", 30, type=int)
    return jsonify(weather.get_correlation(days))


@app.route("/api/weather/summary")
def api_weather_summary():
    """Zusammenfassung pro Wetterlage."""
    days = request.args.get("days", 90, type=int)
    return jsonify(weather.get_condition_summary(days))


# ============================================================
# Dashboard-Filter + Export API
# ============================================================
@app.route("/api/daily/range")
def api_daily_range():
    """Tagesstatistiken nach Datumsbereich.
    ?from=2026-01-01&to=2026-03-13 oder ?month=2026-03
    """
    import sqlite3
    month = request.args.get("month", "")
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")

    conn = sqlite3.connect(sniffer.db_path)
    conn.row_factory = sqlite3.Row

    if month:
        rows = conn.execute(
            "SELECT * FROM daily_stats WHERE date LIKE ? ORDER BY date DESC",
            (month + "%",)
        ).fetchall()
    elif date_from and date_to:
        rows = conn.execute(
            "SELECT * FROM daily_stats WHERE date >= ? AND date <= ? ORDER BY date DESC",
            (date_from, date_to)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM daily_stats ORDER BY date DESC LIMIT 30"
        ).fetchall()

    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/analytics/monthly-summary")
def api_monthly_summary():
    """Monats-Aggregation: Gesamtumsatz, Verkaeufe, Top-Produkt."""
    import sqlite3
    month = request.args.get("month", time.strftime("%Y-%m"))

    conn = sqlite3.connect(sniffer.db_path)
    conn.row_factory = sqlite3.Row

    # Aggregation
    row = conn.execute("""
        SELECT COALESCE(SUM(total_sales), 0) as total_sales,
               COALESCE(SUM(total_revenue_cents), 0) as total_revenue,
               COUNT(*) as days_count,
               COALESCE(AVG(total_sales), 0) as avg_daily_sales,
               COALESCE(AVG(total_revenue_cents), 0) as avg_daily_revenue
        FROM daily_stats WHERE date LIKE ?
    """, (month + "%",)).fetchone()

    # Top-Produkt des Monats
    top = conn.execute("""
        SELECT v.product_id, COUNT(*) as cnt,
               p.product_name
        FROM vend_events v
        LEFT JOIN product_mappings p ON v.product_id = p.slot_id
            AND p.active_until IS NULL
        WHERE v.success = 1
            AND date(v.timestamp, 'unixepoch', 'localtime') LIKE ?
        GROUP BY v.product_id
        ORDER BY cnt DESC LIMIT 1
    """, (month + "%",)).fetchone()

    conn.close()

    d = dict(row) if row else {}
    d["month"] = month
    d["total_revenue_euro"] = f"{d.get('total_revenue', 0) / 100:.2f}"
    d["avg_daily_revenue_euro"] = f"{d.get('avg_daily_revenue', 0) / 100:.2f}"
    d["avg_daily_sales"] = round(d.get("avg_daily_sales", 0), 1)
    if top:
        d["top_product"] = top["product_name"] or f"Slot #{top['product_id']}"
        d["top_product_count"] = top["cnt"]
    else:
        d["top_product"] = "-"
        d["top_product_count"] = 0

    return jsonify(d)


@app.route("/api/export/csv")
def api_export_csv():
    """CSV-Export: Tagesstatistiken oder Verkaeufe.
    ?type=daily&from=...&to=...&month=...
    ?type=vends&from=...&to=...&month=...
    """
    import sqlite3
    export_type = request.args.get("type", "daily")
    month = request.args.get("month", "")
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")

    conn = sqlite3.connect(sniffer.db_path)
    conn.row_factory = sqlite3.Row

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_MINIMAL)

    if export_type == "vends":
        # Verkaeufe exportieren
        query = """
            SELECT date(v.timestamp, 'unixepoch', 'localtime') as datum,
                   time(v.timestamp, 'unixepoch', 'localtime') as uhrzeit,
                   v.product_id as slot,
                   COALESCE(p.product_name, 'Slot #' || v.product_id) as produkt,
                   v.price_cents / 100.0 as preis_eur,
                   v.payment_method as zahlungsart
            FROM vend_events v
            LEFT JOIN product_mappings p ON v.product_id = p.slot_id
                AND p.active_until IS NULL
            WHERE v.success = 1
        """
        params = []
        if month:
            query += " AND date(v.timestamp, 'unixepoch', 'localtime') LIKE ?"
            params.append(month + "%")
        elif date_from and date_to:
            query += " AND date(v.timestamp, 'unixepoch', 'localtime') >= ? AND date(v.timestamp, 'unixepoch', 'localtime') <= ?"
            params.extend([date_from, date_to])
        query += " ORDER BY v.timestamp DESC"

        rows = conn.execute(query, params).fetchall()
        writer.writerow(["Datum", "Uhrzeit", "Slot", "Produkt", "Preis (EUR)", "Zahlungsart"])
        for r in rows:
            writer.writerow([r["datum"], r["uhrzeit"], r["slot"],
                            r["produkt"], f"{r['preis_eur']:.2f}".replace('.', ','),
                            r["zahlungsart"]])
        filename = f"verkaeufe_{month or date_from or 'alle'}.csv"

    else:
        # Tagesstatistiken exportieren
        query = "SELECT * FROM daily_stats WHERE 1=1 "
        params = []
        if month:
            query += "AND date LIKE ? "
            params.append(month + "%")
        elif date_from and date_to:
            query += "AND date >= ? AND date <= ? "
            params.extend([date_from, date_to])
        query += "ORDER BY date DESC"

        rows = conn.execute(query, params).fetchall()
        writer.writerow(["Datum", "Verkaeufe", "Umsatz (EUR)", "Fehler"])
        for r in rows:
            writer.writerow([r["date"], r["total_sales"],
                            f"{r['total_revenue_cents'] / 100:.2f}".replace('.', ','),
                            r["errors"]])
        filename = f"tagesstatistik_{month or date_from or 'alle'}.csv"

    conn.close()

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )



# ============================================================
# Werbe-Display (ESP32) API
# ============================================================
@app.route("/api/ads/current")
def api_ads_current():
    """Aktuelles Werbebild als JPEG (wird vom ESP32 abgerufen)."""
    data = ad_manager.get_current_ad()
    if data is None:
        return "Keine Werbung konfiguriert", 404
    return Response(data, mimetype="image/jpeg")


@app.route("/api/ads/list")
def api_ads_list():
    """Alle Werbungen auflisten."""
    return jsonify(ad_manager.list_ads())


@app.route("/api/ads/status")
def api_ads_status():
    """Display-Status."""
    return jsonify(ad_manager.get_status())


@app.route("/api/ads/upload", methods=["POST"])
def api_ads_upload():
    """Neues Werbebild hochladen."""
    if "file" not in request.files:
        return jsonify({"error": "Kein Bild hochgeladen"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Kein Dateiname"}), 400
    # Nur Bild-Endungen zulassen (Anti-Missbrauch)
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ("jpg", "jpeg", "png", "webp"):
        return jsonify({"error": "Nur JPG/PNG/WEBP erlaubt"}), 400
    title = request.form.get("title", "")
    result = ad_manager.add_ad(file.filename, file.read(), title)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/ads/<ad_id>", methods=["DELETE"])
def api_ads_delete(ad_id):
    """Werbung loeschen."""
    return jsonify(ad_manager.delete_ad(ad_id))


@app.route("/api/ads/<ad_id>/toggle", methods=["POST"])
def api_ads_toggle(ad_id):
    """Werbung aktivieren/deaktivieren."""
    return jsonify(ad_manager.toggle_ad(ad_id))


@app.route("/api/ads/interval", methods=["POST"])
def api_ads_interval():
    """Wechsel-Intervall setzen. JSON: {"seconds": 10}"""
    data = request.get_json()
    if not data or "seconds" not in data:
        return jsonify({"error": "seconds erforderlich"}), 400
    return jsonify(ad_manager.set_interval(data["seconds"]))


# ============================================================
# Main
# ============================================================
def main():
    global sniffer, ceo, analytics, weather, ad_manager

    print(r"""
    ╔══════════════════════════════════════════════╗
    ║   MDB Sniffer Dashboard + CEO Agent          ║
    ║   Sielaff SUe2020 + Qibixx Pi Hat Plus       ║
    ╚══════════════════════════════════════════════╝
    """)

    # Sniffer initialisieren
    sniffer = MDBSniffer()

    # Serielle Verbindung herstellen
    connected = sniffer.connect()
    if connected:
        print("[OK] Verbindung hergestellt")
    else:
        print("[!!] Verbindung fehlgeschlagen - starte im Demo-Modus")

    # Sniffer starten
    sniffer.start()
    print("[OK] MDB Sniffer laeuft")

    # Analytics
    analytics = SalesAnalytics(sniffer.db_path)
    print("[OK] Sales Analytics bereit")

    # Wetter-Manager starten
    weather = WeatherManager(
        db_path=sniffer.db_path,
        api_key=WEATHER_API_KEY,
        city=WEATHER_CITY,
        fetch_interval=WEATHER_FETCH_INTERVAL,
        enabled=WEATHER_ENABLED,
    )
    weather.start()
    print(f"[OK] Wetter-Manager {'aktiv' if weather.enabled else 'deaktiviert'}")

    # Ad-Manager (ESP32 Werbe-Display)
    ad_manager = AdManager()
    print('[OK] Ad-Manager bereit')

    # CEO-Agent starten
    ceo = AutomatCEO(sniffer)
    ceo.start()
    print("[OK] CEO-Agent aktiv")

    # Telegram-Bot starten (interaktive Befehle)
    bot = TelegramBot(
        sniffer=sniffer,
        ceo=ceo,
        analytics=analytics,
        weather=weather,
    )
    bot.start()
    print(f"[OK] Telegram-Bot {'aktiv' if bot.enabled else 'deaktiviert'}")

    # Webserver starten
    print(f"[OK] Dashboard: http://{WEB_HOST}:{WEB_PORT}")
    print(f"     Tailscale: http://<dein-tailscale-hostname>:{WEB_PORT}")
    print()
    print("     API Endpunkte:")
    print("       GET  /api/stats           - Statistiken")
    print("       GET  /api/messages        - MDB-Nachrichten")
    print("       GET  /api/vends           - Verkaeufe")
    print("       GET  /api/daily           - Tagesstatistik")
    print("       GET  /api/products        - Produktstatistik (mit Namen)")
    print("       GET  /api/health          - Health-Check")
    print("       --- Produktverwaltung ---")
    print("       GET  /api/products/mappings        - Aktive Zuordnungen")
    print("       POST /api/products/mappings        - Produkt zuordnen")
    print("       DEL  /api/products/mappings/<slot> - Zuordnung entfernen")
    print("       GET  /api/products/unmapped        - Unbenannte Slots")
    print("       --- CEO Agent ---")
    print("       GET  /api/agent/status    - Agent-Status")
    print("       GET  /api/agent/alerts    - Agent-Alerts")
    print("       POST /api/agent/report/daily   - Tagesbericht")
    print("       POST /api/agent/report/weekly  - Wochenbericht")
    print()

    try:
        app.run(host=WEB_HOST, port=WEB_PORT, debug=False, threaded=True)
    except KeyboardInterrupt:
        print("\nBeende...")
    finally:
        bot.stop()
        ceo.stop()
        sniffer.stop()
        print("Bot + Agent + Sniffer gestoppt. Tschuess!")


if __name__ == "__main__":
    main()
