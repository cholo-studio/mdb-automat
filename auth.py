"""
Authentifizierung fuer das MDB Dashboard
========================================
Schlanker Passwort-Login (Session-Cookie, signiert ueber SECRET_KEY).
Keine externen Abhaengigkeiten - nur stdlib (secrets, hmac, hashlib).

- SECRET_KEY und Dashboard-Passwort werden beim ersten Start automatisch
  generiert und in Dateien neben der App abgelegt (chmod 600).
- Schuetzt sowohl die Web-Oberflaeche als auch alle /api-Endpunkte.
- Zusaetzlich zu Tailscale - greift auch, falls Port 5000 versehentlich
  im LAN oder per Tailscale Funnel offen ist.
"""

import os
import hmac
import secrets
import hashlib
import logging
from functools import wraps

from flask import session, request, redirect, url_for, render_template, jsonify

import config

logger = logging.getLogger("mdb_auth")


def _write_secret_file(path: str, value: str):
    """Schreibt einen Geheim-Wert mit restriktiven Rechten (nur Besitzer)."""
    with open(path, "w") as f:
        f.write(value)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_or_create_secret_key() -> str:
    """Liefert den Flask SECRET_KEY (Override > Datei > neu generiert)."""
    if config.SECRET_KEY:
        return config.SECRET_KEY
    path = config.SECRET_KEY_FILE
    if os.path.exists(path):
        with open(path) as f:
            key = f.read().strip()
        if key:
            return key
    key = secrets.token_hex(32)
    _write_secret_file(path, key)
    logger.info("Neuer SECRET_KEY generiert: %s", path)
    return key


def load_or_create_password() -> str:
    """Liefert das Dashboard-Passwort (Override > Datei > neu generiert)."""
    if config.DASHBOARD_PASSWORD:
        return config.DASHBOARD_PASSWORD
    path = config.PASSWORD_FILE
    if os.path.exists(path):
        with open(path) as f:
            pw = f.read().strip()
        if pw:
            return pw
    # Gut merkbares, aber zufaelliges Passwort erzeugen
    pw = secrets.token_urlsafe(9)
    _write_secret_file(path, pw)
    logger.warning("=" * 56)
    logger.warning("NEUES DASHBOARD-PASSWORT generiert (in %s):", path)
    logger.warning("    %s", pw)
    logger.warning("Mit diesem Passwort am Dashboard anmelden.")
    logger.warning("=" * 56)
    return pw


def load_or_create_ingest_key() -> str:
    """API-Key fuer Maschine-zu-Maschine-Ingest (Document AI, Smart Plug)."""
    if config.INGEST_KEY:
        return config.INGEST_KEY
    path = config.INGEST_KEY_FILE
    if os.path.exists(path):
        with open(path) as f:
            key = f.read().strip()
        if key:
            return key
    key = secrets.token_urlsafe(24)
    _write_secret_file(path, key)
    logger.warning("Neuer INGEST-API-Key generiert (in %s): %s", path, key)
    return key


# Beim Import einmalig laden
_PASSWORD = load_or_create_password() if config.AUTH_ENABLED else ""
_INGEST_KEY = load_or_create_ingest_key()


def check_ingest_key(candidate: str) -> bool:
    """Zeitkonstanter Vergleich des Ingest-API-Keys (Header X-API-Key)."""
    if not candidate:
        return False
    return hmac.compare_digest(candidate.encode(), _INGEST_KEY.encode())


def ingest_key_required(view):
    """Decorator fuer Ingest-Endpunkte: erfordert gueltigen X-API-Key."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        key = request.headers.get("X-API-Key", "") or request.args.get("api_key", "")
        if not check_ingest_key(key):
            return jsonify({"error": "invalid_api_key"}), 401
        return view(*args, **kwargs)
    return wrapped


def check_password(candidate: str) -> bool:
    """Zeitkonstanter Passwortvergleich."""
    if not config.AUTH_ENABLED:
        return True
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode()).digest(),
        hashlib.sha256(_PASSWORD.encode()).digest(),
    )


def is_authenticated() -> bool:
    if not config.AUTH_ENABLED:
        return True
    return bool(session.get("authenticated"))


def login_required(view):
    """Decorator: schuetzt eine Route. API -> 401 JSON, Web -> Redirect zum Login."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if is_authenticated():
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        return redirect(url_for("login", next=request.path))
    return wrapped
