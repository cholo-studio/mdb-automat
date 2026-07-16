"""
Ad Manager fuer ESP32 Werbe-Display
====================================
Verwaltet Werbebilder die auf dem ESP32-Display im Automaten gezeigt werden.
Bilder werden als JPEG gespeichert, auf 320x480 skaliert und komprimiert.
"""

import os
import json
import time
import logging
from pathlib import Path
from io import BytesIO

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logging.warning("Pillow nicht installiert! pip install Pillow")

logger = logging.getLogger(__name__)

# Konfiguration
ADS_DIR = os.path.join(os.path.dirname(__file__), "ads")
ADS_CONFIG = os.path.join(ADS_DIR, "ads_config.json")
DISPLAY_WIDTH = 320
DISPLAY_HEIGHT = 480
MAX_JPEG_SIZE = 40000  # 40KB max (ESP32 hat 45KB Buffer)
JPEG_QUALITY_START = 85


class AdManager:
    """Verwaltet Werbebilder fuer das ESP32-Display."""

    def __init__(self):
        """Initialisiert den AdManager und erstellt Ordner."""
        os.makedirs(ADS_DIR, exist_ok=True)
        self.config = self._load_config()
        logger.info(f"AdManager: {len(self.config.get('ads', []))} Werbungen geladen")

    def _load_config(self):
        """Laedt die Ads-Konfiguration."""
        if os.path.exists(ADS_CONFIG):
            try:
                with open(ADS_CONFIG, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Config-Fehler: {e}")
        return {"ads": [], "current_index": 0, "interval_seconds": 10}

    def _save_config(self):
        """Speichert die Ads-Konfiguration."""
        with open(ADS_CONFIG, "w") as f:
            json.dump(self.config, f, indent=2)

    def add_ad(self, filename, image_data, title=""):
        """
        Fuegt eine neue Werbung hinzu.
        Skaliert auf 320x480 und komprimiert als JPEG.

        Args:
            filename: Original-Dateiname
            image_data: Bild-Bytes
            title: Optionaler Titel

        Returns:
            dict mit Ergebnis
        """
        if not PIL_AVAILABLE:
            return {"error": "Pillow nicht installiert! pip install Pillow"}

        try:
            # Bild oeffnen
            img = Image.open(BytesIO(image_data))

            # In RGB konvertieren (falls PNG mit Alpha)
            if img.mode != "RGB":
                img = img.convert("RGB")

            # Auf Display-Groesse skalieren (Portrait 320x480)
            img = self._resize_image(img)

            # Als JPEG komprimieren (max 55KB)
            jpeg_data = self._compress_jpeg(img)

            if jpeg_data is None:
                return {"error": "Bild konnte nicht auf <55KB komprimiert werden"}

            # Speichern
            ad_id = f"ad_{int(time.time())}"
            ad_filename = f"{ad_id}.jpg"
            ad_path = os.path.join(ADS_DIR, ad_filename)

            with open(ad_path, "wb") as f:
                f.write(jpeg_data)

            # Config updaten
            ad_entry = {
                "id": ad_id,
                "filename": ad_filename,
                "original_name": filename,
                "title": title or filename,
                "size_bytes": len(jpeg_data),
                "added": time.strftime("%Y-%m-%d %H:%M"),
                "active": True,
            }
            self.config["ads"].append(ad_entry)
            self._save_config()

            logger.info(f"Ad hinzugefuegt: {ad_filename} ({len(jpeg_data)} bytes)")
            return {"success": True, "ad": ad_entry}

        except Exception as e:
            logger.error(f"Ad-Upload Fehler: {e}")
            return {"error": str(e)}

    def _resize_image(self, img):
        """Skaliert Bild auf 320x480 mit korrektem Seitenverhaeltnis."""
        target_ratio = DISPLAY_WIDTH / DISPLAY_HEIGHT  # 0.667
        img_ratio = img.width / img.height

        if img_ratio > target_ratio:
            # Bild ist breiter -> Hoehe anpassen, dann croppen
            new_height = DISPLAY_HEIGHT
            new_width = int(new_height * img_ratio)
        else:
            # Bild ist hoeher -> Breite anpassen, dann croppen
            new_width = DISPLAY_WIDTH
            new_height = int(new_width / img_ratio)

        img = img.resize((new_width, new_height), Image.LANCZOS)

        # Zentriert croppen auf 320x480
        left = (new_width - DISPLAY_WIDTH) // 2
        top = (new_height - DISPLAY_HEIGHT) // 2
        img = img.crop((left, top, left + DISPLAY_WIDTH, top + DISPLAY_HEIGHT))

        return img

    def _compress_jpeg(self, img):
        """Komprimiert Bild als JPEG unter MAX_JPEG_SIZE."""
        quality = JPEG_QUALITY_START

        while quality >= 20:
            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=quality, optimize=True)
            data = buffer.getvalue()

            if len(data) <= MAX_JPEG_SIZE:
                logger.debug(f"JPEG: {len(data)} bytes bei quality={quality}")
                return data

            quality -= 5

        return None

    def get_current_ad(self):
        """Gibt das aktuelle Werbebild als JPEG-Bytes zurueck."""
        active_ads = [a for a in self.config.get("ads", []) if a.get("active", True)]

        if not active_ads:
            return None

        # Index rotieren
        idx = self.config.get("current_index", 0) % len(active_ads)
        ad = active_ads[idx]

        # Naechsten Index setzen
        self.config["current_index"] = (idx + 1) % len(active_ads)
        self._save_config()

        # Bild laden
        ad_path = os.path.join(ADS_DIR, ad["filename"])
        if os.path.exists(ad_path):
            with open(ad_path, "rb") as f:
                return f.read()

        logger.warning(f"Ad nicht gefunden: {ad_path}")
        return None

    def list_ads(self):
        """Gibt alle Werbungen zurueck."""
        return self.config.get("ads", [])

    def delete_ad(self, ad_id):
        """Loescht eine Werbung."""
        ads = self.config.get("ads", [])
        for i, ad in enumerate(ads):
            if ad["id"] == ad_id:
                # Datei loeschen
                ad_path = os.path.join(ADS_DIR, ad["filename"])
                if os.path.exists(ad_path):
                    os.remove(ad_path)
                # Aus Config entfernen
                ads.pop(i)
                self._save_config()
                logger.info(f"Ad geloescht: {ad_id}")
                return {"success": True}

        return {"error": "Ad nicht gefunden"}

    def toggle_ad(self, ad_id):
        """Aktiviert/Deaktiviert eine Werbung."""
        for ad in self.config.get("ads", []):
            if ad["id"] == ad_id:
                ad["active"] = not ad.get("active", True)
                self._save_config()
                return {"success": True, "active": ad["active"]}

        return {"error": "Ad nicht gefunden"}

    def set_interval(self, seconds):
        """Setzt das Wechsel-Intervall."""
        self.config["interval_seconds"] = max(3, min(300, seconds))
        self._save_config()
        return {"success": True, "interval": self.config["interval_seconds"]}

    def get_status(self):
        """Gibt den Display-Status zurueck."""
        active = [a for a in self.config.get("ads", []) if a.get("active", True)]
        return {
            "total_ads": len(self.config.get("ads", [])),
            "active_ads": len(active),
            "interval_seconds": self.config.get("interval_seconds", 10),
            "current_index": self.config.get("current_index", 0),
        }
