"""Decode scale broadcasts. Reception time is used because the scale clock may be unset."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

MISCALE_V2_UUID = '0000181b-0000-1000-8000-00805f9b34fb'
MISCALE_V1_UUID = '0000181d-0000-1000-8000-00805f9b34fb'

@dataclass(frozen=True)
class ScaleMeasurement:
    weight: float
    unit: str
    timestamp: str
    stabilized: bool
    impedance: int | None
    source_address: str
    rssi: int | None
    load_removed: bool = False
    packet_id: str = ''

def parse_service_info(info: Any) -> ScaleMeasurement | None:
    service_data = {str(k).lower(): bytes(v) for k,v in info.service_data.items()}
    impedance = None
    if MISCALE_V2_UUID in service_data:
        data = service_data[MISCALE_V2_UUID]
        if len(data) != 13:
            return None
        control = data[1]
        raw = int.from_bytes(data[11:13], 'little')
        if data[0] == 2:
            weight = raw / 200
        elif data[0] == 3:
            weight = raw / 100 * 0.45359237
        else:
            return None
        if control & 2:
            value = int.from_bytes(data[9:11], 'little')
            if 0 < value < 65533:
                impedance = value
    elif MISCALE_V1_UUID in service_data:
        data = service_data[MISCALE_V1_UUID]
        if len(data) < 3:
            return None
        control = data[0]
        raw = int.from_bytes(data[1:3], 'little')
        if control & 1:
            weight = raw / 100 * 0.45359237
        elif control & 16:
            weight = raw / 200
        else:
            weight = raw / 200
    else:
        return None
    if not 0 < weight <= 250:
        return None
    return ScaleMeasurement(
        round(weight, 2), 'kg', datetime.now(timezone.utc).isoformat(),
        bool(control & 32), impedance, info.address, getattr(info, 'rssi', None),
        bool(control & 128), sha256(data).hexdigest(),
    )
