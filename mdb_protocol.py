"""
MDB Protokoll-Parser fuer Qibixx Pi Hat Plus
=============================================
Decodiert MDB-Bus-Nachrichten zwischen VMC (Sielaff SUe2020)
und Peripheriegeraeten (Muenzwechsler, Geldscheinleser, etc.)

MDB Protokoll-Grundlagen:
- 9-Bit Kommunikation (8 Daten + 1 Mode-Bit)
- Mode-Bit = 1: Adress-Byte (erstes Byte einer Nachricht)
- Mode-Bit = 0: Daten-Byte
- Letzes Byte: Checksumme (Summe aller vorherigen Bytes, nur untere 8 Bit)
"""

import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


# ============================================================
# MDB Geraeteadressen
# ============================================================
class MDBAddress(IntEnum):
    COIN_CHANGER = 0x08
    CASHLESS_1 = 0x10
    COMM_GATEWAY = 0x18
    BILL_VALIDATOR = 0x30
    USD_1 = 0x40
    CASHLESS_2 = 0x48
    AGE_VERIFY = 0x58
    CASHLESS_3 = 0x60


# ============================================================
# VMC -> Muenzwechsler Befehle (Adresse 0x08)
# ============================================================
class CoinCommand(IntEnum):
    RESET = 0x08
    SETUP = 0x09
    TUBE_STATUS = 0x0A
    POLL = 0x0B
    COIN_TYPE = 0x0C
    DISPENSE = 0x0D
    EXPANSION = 0x0F


# ============================================================
# VMC -> Geldscheinleser Befehle (Adresse 0x30)
# ============================================================
class BillCommand(IntEnum):
    RESET = 0x30
    SETUP = 0x31
    SECURITY = 0x32
    POLL = 0x33
    BILL_TYPE = 0x34
    ESCROW = 0x35
    STACKER = 0x36
    EXPANSION = 0x37


# ============================================================
# VMC -> Cashless Befehle (Adresse 0x10)
# ============================================================
class CashlessCommand(IntEnum):
    RESET = 0x10
    SETUP = 0x11
    POLL = 0x12
    VEND = 0x13
    READER = 0x14
    REVALUE = 0x15
    EXPANSION = 0x17


# ============================================================
# Cashless VEND Sub-Befehle
# ============================================================
class VendSubCommand(IntEnum):
    VEND_REQUEST = 0x00
    VEND_CANCEL = 0x01
    VEND_SUCCESS = 0x02
    VEND_FAILURE = 0x03
    SESSION_COMPLETE = 0x04
    CASH_SALE = 0x05
    NEGATIVE_VEND = 0x06


# ============================================================
# Standard Muenzwerte (Euro, typisch fuer DE)
# ============================================================
EURO_COIN_VALUES = {
    0: 0.00,   # Nicht belegt
    1: 0.01,   # 1 Cent
    2: 0.02,   # 2 Cent
    3: 0.05,   # 5 Cent
    4: 0.10,   # 10 Cent
    5: 0.20,   # 20 Cent
    6: 0.50,   # 50 Cent
    7: 1.00,   # 1 Euro
    8: 2.00,   # 2 Euro
}

EURO_BILL_VALUES = {
    0: 5.00,    # 5 Euro
    1: 10.00,   # 10 Euro
    2: 20.00,   # 20 Euro
    3: 50.00,   # 50 Euro
}


# ============================================================
# Datenklassen
# ============================================================
@dataclass
class MDBMessage:
    """Eine geparste MDB-Nachricht."""
    timestamp: float
    direction: str          # "VMC->PER" oder "PER->VMC"
    device: str             # z.B. "Muenzwechsler"
    device_addr: int
    command: str            # z.B. "POLL", "VEND_REQUEST"
    raw_bytes: bytes
    data: dict = field(default_factory=dict)
    description: str = ""

    def to_dict(self):
        return {
            "timestamp": self.timestamp,
            "time_str": time.strftime("%H:%M:%S", time.localtime(self.timestamp)),
            "direction": self.direction,
            "device": self.device,
            "device_addr": f"0x{self.device_addr:02X}",
            "command": self.command,
            "raw_hex": self.raw_bytes.hex(" "),
            "data": self.data,
            "description": self.description,
        }


@dataclass
class VendEvent:
    """Ein Verkaufsvorgang."""
    timestamp: float
    product_id: int
    price_cents: int
    payment_method: str     # "coin", "bill", "cashless"
    success: bool

    @property
    def price_euro(self) -> float:
        return self.price_cents / 100.0

    def to_dict(self):
        return {
            "timestamp": self.timestamp,
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp)),
            "product_id": self.product_id,
            "price_cents": self.price_cents,
            "price_euro": f"{self.price_euro:.2f}",
            "payment_method": self.payment_method,
            "success": self.success,
        }


@dataclass
class CoinEvent:
    """Ein Muenzeinwurf."""
    timestamp: float
    coin_type: int
    value_euro: float
    routing: str            # "cash_box", "tubes", "reject"

    def to_dict(self):
        return {
            "timestamp": self.timestamp,
            "time_str": time.strftime("%H:%M:%S", time.localtime(self.timestamp)),
            "coin_type": self.coin_type,
            "value_euro": f"{self.value_euro:.2f}",
            "routing": self.routing,
        }


# ============================================================
# MDB Parser
# ============================================================
class MDBParser:
    """Parst rohe MDB-Bytes vom Qibixx Sniffer in strukturierte Nachrichten."""

    def __init__(self):
        self.coin_values = dict(EURO_COIN_VALUES)
        self.bill_values = dict(EURO_BILL_VALUES)
        self.tube_status = {}
        self.credit_cents = 0
        self._setup_received = False
        # Kontext-Tracking fuer Peripherie-Antworten
        self._last_master_device = "Peripherie"
        self._last_master_addr = 0
        self._last_master_command = ""

    def get_device_name(self, addr: int) -> str:
        """Geraetename anhand der MDB-Adresse."""
        base_addr = addr & 0xF8  # Untere 3 Bits sind Sub-Befehl
        names = {
            0x08: "Muenzwechsler",
            0x10: "Cashless 1",
            0x18: "Komm-Gateway",
            0x30: "Geldscheinleser",
            0x40: "USD 1",
            0x48: "Cashless 2",
            0x58: "Altersverifikation",
            0x60: "Cashless 3",
        }
        return names.get(base_addr, f"Unbekannt (0x{addr:02X})")

    def parse_sniffer_line(self, line: str) -> Optional[MDBMessage]:
        """
        Parst eine Zeile vom Qibixx Sniffer-Output.

        Qibixx API-Sniff Format (Firmware 4.x):
          x,aa,tttttttttt,yy[,data]
          - aa: 00=VMC/Master, 80=Peripherie, 02/82=Fehler
          - tttttttttt: Timestamp (0.1ms Aufloesung)
          - yy: Befehlsbyte hex (oder ACK/NAK)
          - data: optionale Datenbytes als Hex
        """
        line = line.strip()
        if not line:
            return None

        now = time.time()

        try:
            # Qibixx Sniffer-Format: x,aa,tttttttttt,yy[,data]
            if line.startswith("x,"):
                return self._parse_qibixx_sniffer(line, now)

            # Qibixx Steuermeldungen ignorieren (Version, Fehlerdetails)
            if line.startswith(("v,", "V,", "Y,")):
                return None

            # Legacy-Format Fallback (fuer Tests/Demo)
            if line.startswith(("s ", "S ")):
                return self._parse_master_message(line[2:], now)
            elif line.startswith(("r ", "R ")):
                return self._parse_peripheral_response(line[2:], now)

        except (ValueError, IndexError) as e:
            return MDBMessage(
                timestamp=now,
                direction="???",
                device="Parse-Fehler",
                device_addr=0,
                command="ERROR",
                raw_bytes=line.encode(),
                description=f"Parse-Fehler: {e}"
            )

        return None

    def _parse_qibixx_sniffer(self, line: str, timestamp: float) -> Optional[MDBMessage]:
        """
        Parst eine Qibixx API-Sniff Zeile.
        Format: x,aa,tttttttttt,yy[,data]
        aa: 00=Master, 80=Slave, 02=Slave-Fehler, 82=Master-Fehler
        """
        parts = line.split(",")
        if len(parts) < 3:
            return None

        direction_str = parts[1].strip()

        # Steuer-Antworten (x,ACK / x,NAK nach X,1 Kommando)
        if direction_str.upper() in ("ACK", "NAK"):
            return None

        try:
            direction_byte = int(direction_str, 16)
        except ValueError:
            return None

        if len(parts) < 4:
            return None

        # Alles nach Timestamp = Befehls-/Datenbytes
        payload_parts = [p.strip() for p in parts[3:]]

        is_from_master = (direction_byte & 0x80) == 0
        is_error_frame = (direction_byte & 0x7E) == 0x02  # 0x02 oder 0x82

        # --- Peripherie ACK/NAK (Text, nicht Hex) ---
        if not is_from_master and len(payload_parts) >= 1:
            token = payload_parts[0].upper()
            if token == "ACK":
                return MDBMessage(
                    timestamp=timestamp,
                    direction="PER->VMC",
                    device=self._last_master_device,
                    device_addr=self._last_master_addr,
                    command="ACK",
                    raw_bytes=b"\x00",
                    description=f"{self._last_master_device} ACK",
                )
            if token == "NAK":
                return MDBMessage(
                    timestamp=timestamp,
                    direction="PER->VMC",
                    device=self._last_master_device,
                    device_addr=self._last_master_addr,
                    command="NAK",
                    raw_bytes=b"\xff",
                    description=f"{self._last_master_device} NAK",
                )

        # --- Hex-Payload zusammenbauen ---
        hex_combined = "".join(payload_parts)
        try:
            raw_bytes = bytes.fromhex(hex_combined)
        except ValueError:
            return None

        if not raw_bytes:
            return None

        # --- Fehler-Frame ---
        if is_error_frame:
            return MDBMessage(
                timestamp=timestamp,
                direction="VMC->PER" if is_from_master else "PER->VMC",
                device="MDB-Bus",
                device_addr=0,
                command="BUS_ERROR",
                raw_bytes=raw_bytes,
                description=f"MDB Bus-Fehler: {raw_bytes.hex(' ')}",
            )

        # --- Master/VMC Nachricht ---
        if is_from_master:
            addr_byte = raw_bytes[0]
            base_addr = addr_byte & 0xF8
            device = self.get_device_name(addr_byte)
            data_bytes = raw_bytes[1:] if len(raw_bytes) > 1 else b""

            command, description, data = self._decode_command(base_addr, addr_byte, data_bytes)

            # Kontext merken fuer nachfolgende Peripherie-Antwort
            self._last_master_device = device
            self._last_master_addr = base_addr
            self._last_master_command = command

            return MDBMessage(
                timestamp=timestamp,
                direction="VMC->PER",
                device=device,
                device_addr=base_addr,
                command=command,
                raw_bytes=raw_bytes,
                data=data,
                description=description,
            )

        # --- Peripherie Daten-Antwort ---
        return MDBMessage(
            timestamp=timestamp,
            direction="PER->VMC",
            device=self._last_master_device,
            device_addr=self._last_master_addr,
            command="DATA",
            raw_bytes=raw_bytes,
            data={"bytes": [f"0x{b:02X}" for b in raw_bytes]},
            description=f"{self._last_master_device} Daten: {raw_bytes.hex(' ')}",
        )

    def _hex_to_bytes(self, hex_str: str) -> bytes:
        """Konvertiert Hex-String in Bytes (unterstuetzt kontinuierliches und getrenntes Hex)."""
        hex_str = hex_str.strip().replace("0x", "").replace(",", " ")
        parts = hex_str.split()
        result = []
        for p in parts:
            if not p:
                continue
            if len(p) > 2:
                # Kontinuierliches Hex aufteilen (z.B. "ffff0000" -> ff ff 00 00)
                for i in range(0, len(p) - 1, 2):
                    result.append(int(p[i:i+2], 16))
            else:
                result.append(int(p, 16))
        return bytes(result)

    def _parse_master_message(self, hex_str: str, timestamp: float) -> Optional[MDBMessage]:
        """Parst eine VMC->Peripherie Nachricht."""
        raw = self._hex_to_bytes(hex_str)
        if not raw:
            return None

        addr_byte = raw[0]
        base_addr = addr_byte & 0xF8
        sub_cmd = addr_byte & 0x07
        device = self.get_device_name(addr_byte)
        data_bytes = raw[1:] if len(raw) > 1 else b""

        command, description, data = self._decode_command(base_addr, addr_byte, data_bytes)

        return MDBMessage(
            timestamp=timestamp,
            direction="VMC->PER",
            device=device,
            device_addr=base_addr,
            command=command,
            raw_bytes=raw,
            data=data,
            description=description,
        )

    def _parse_peripheral_response(self, hex_str: str, timestamp: float) -> Optional[MDBMessage]:
        """Parst eine Peripherie->VMC Antwort."""
        raw = self._hex_to_bytes(hex_str)
        if not raw:
            return None

        # ACK (0x00) ist die haeufigste Antwort
        if raw == b"\x00":
            return MDBMessage(
                timestamp=timestamp,
                direction="PER->VMC",
                device="Peripherie",
                device_addr=0,
                command="ACK",
                raw_bytes=raw,
                description="Bestaetigung",
            )

        # NAK (0xFF)
        if raw == b"\xff":
            return MDBMessage(
                timestamp=timestamp,
                direction="PER->VMC",
                device="Peripherie",
                device_addr=0,
                command="NAK",
                raw_bytes=raw,
                description="Negative Bestaetigung / Fehler",
            )

        return MDBMessage(
            timestamp=timestamp,
            direction="PER->VMC",
            device="Peripherie",
            device_addr=0,
            command="DATA",
            raw_bytes=raw,
            data={"bytes": [f"0x{b:02X}" for b in raw]},
            description=f"Daten: {raw.hex(' ')}",
        )

    def _decode_command(self, base_addr: int, full_byte: int, data: bytes):
        """Decodiert einen MDB-Befehl anhand der Adresse."""
        cmd_name = f"CMD_0x{full_byte:02X}"
        description = ""
        parsed_data = {}

        # --- Muenzwechsler (0x08) ---
        if base_addr == 0x08:
            cmd_name, description, parsed_data = self._decode_coin_command(full_byte, data)

        # --- Geldscheinleser (0x30) ---
        elif base_addr == 0x30:
            cmd_name, description, parsed_data = self._decode_bill_command(full_byte, data)

        # --- Cashless (0x10, 0x48, 0x60) ---
        elif base_addr in (0x10, 0x48, 0x60):
            cmd_name, description, parsed_data = self._decode_cashless_command(full_byte, data)

        return cmd_name, description, parsed_data

    def _decode_coin_command(self, cmd_byte, data):
        """Decodiert Muenzwechsler-Befehle."""
        commands = {
            0x08: ("RESET", "Muenzwechsler Reset"),
            0x09: ("SETUP", "Muenzwechsler Setup abfragen"),
            0x0A: ("TUBE_STATUS", "Rohrenstatus abfragen"),
            0x0B: ("POLL", "Muenzwechsler pollen"),
            0x0C: ("COIN_TYPE", "Muenztypen konfigurieren"),
            0x0D: ("DISPENSE", "Muenzen ausgeben"),
            0x0F: ("EXPANSION", "Erweiterungsbefehl"),
        }
        cmd = commands.get(cmd_byte, (f"COIN_0x{cmd_byte:02X}", "Unbekannter Muenzbefehl"))
        parsed = {}

        if cmd_byte == 0x0D and len(data) >= 1:
            # Dispense: Daten enthalten Muenztyp und Anzahl
            parsed["coin_type"] = (data[0] >> 4) & 0x0F
            parsed["count"] = data[0] & 0x0F
            value = self.coin_values.get(parsed["coin_type"], 0)
            parsed["value_euro"] = f"{value:.2f}"

        return cmd[0], cmd[1], parsed

    def _decode_bill_command(self, cmd_byte, data):
        """Decodiert Geldscheinleser-Befehle."""
        commands = {
            0x30: ("RESET", "Geldscheinleser Reset"),
            0x31: ("SETUP", "Geldscheinleser Setup abfragen"),
            0x32: ("SECURITY", "Sicherheitslevel setzen"),
            0x33: ("POLL", "Geldscheinleser pollen"),
            0x34: ("BILL_TYPE", "Scheintypen konfigurieren"),
            0x35: ("ESCROW", "Schein annehmen/ablehnen"),
            0x36: ("STACKER", "Stacker-Status"),
            0x37: ("EXPANSION", "Erweiterungsbefehl"),
        }
        cmd = commands.get(cmd_byte, (f"BILL_0x{cmd_byte:02X}", "Unbekannter Scheinbefehl"))
        return cmd[0], cmd[1], {}

    def _decode_cashless_command(self, cmd_byte, data):
        """Decodiert Cashless-Befehle."""
        base = cmd_byte & 0xF8
        sub = cmd_byte & 0x07

        commands = {
            0: ("RESET", "Cashless Reset"),
            1: ("SETUP", "Cashless Setup"),
            2: ("POLL", "Cashless pollen"),
            3: ("VEND", "Verkaufsbefehl"),
            4: ("READER", "Reader ein/aus"),
            5: ("REVALUE", "Guthaben aendern"),
            7: ("EXPANSION", "Erweiterungsbefehl"),
        }
        cmd = commands.get(sub, (f"CASHLESS_0x{cmd_byte:02X}", "Unbekannter Cashless-Befehl"))
        parsed = {}

        # VEND Sub-Befehle decodieren
        if sub == 3 and len(data) >= 1:
            vend_sub = data[0]
            vend_names = {
                0x00: "VEND_REQUEST",
                0x01: "VEND_CANCEL",
                0x02: "VEND_SUCCESS",
                0x03: "VEND_FAILURE",
                0x04: "SESSION_COMPLETE",
                0x05: "CASH_SALE",
            }
            vend_name = vend_names.get(vend_sub, f"VEND_SUB_0x{vend_sub:02X}")
            parsed["vend_sub"] = vend_name

            # VEND_REQUEST: Preis und Produkt-ID extrahieren
            if vend_sub == 0x00 and len(data) >= 5:
                price = (data[1] << 8) | data[2]
                product = (data[3] << 8) | data[4]
                parsed["price_cents"] = price
                parsed["price_euro"] = f"{price / 100:.2f}"
                parsed["product_id"] = product
                return (
                    f"VEND_REQUEST",
                    f"Verkaufsanfrage: Produkt {product}, Preis {price/100:.2f} EUR",
                    parsed,
                )

            # CASH_SALE: Barverkauf
            if vend_sub == 0x05 and len(data) >= 5:
                price = (data[1] << 8) | data[2]
                product = (data[3] << 8) | data[4]
                parsed["price_cents"] = price
                parsed["price_euro"] = f"{price / 100:.2f}"
                parsed["product_id"] = product
                return (
                    "CASH_SALE",
                    f"Barverkauf: Produkt {product}, Preis {price/100:.2f} EUR",
                    parsed,
                )

            return vend_name, f"Vend: {vend_name}", parsed

        return cmd[0], cmd[1], parsed


def parse_coin_poll_response(data: bytes) -> list[CoinEvent]:
    """
    Parst eine Muenzwechsler-POLL-Antwort.
    Jedes Byte-Paar: [Routing + Muenztyp] [Anzahl in Roehren]
    """
    events = []
    now = time.time()

    i = 0
    while i < len(data) - 1:  # -1 fuer Checksumme
        byte1 = data[i]

        # Bits 6-5: Routing
        routing_bits = (byte1 >> 4) & 0x03
        routing_map = {0: "cash_box", 1: "tubes", 2: "reject", 3: "reject"}
        routing = routing_map.get(routing_bits, "unknown")

        coin_type = byte1 & 0x0F
        value = EURO_COIN_VALUES.get(coin_type, 0.0)

        if value > 0:
            events.append(CoinEvent(
                timestamp=now,
                coin_type=coin_type,
                value_euro=value,
                routing=routing,
            ))

        i += 1

    return events
