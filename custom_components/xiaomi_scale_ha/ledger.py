"""Persistent weigh-in records and ownership decisions, independent of HA."""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from statistics import median


def seconds_between(first: str, second: str) -> float:
    return (datetime.fromisoformat(second) - datetime.fromisoformat(first)).total_seconds()


class Ledger:
    def __init__(self, records=None):
        self.records = records or []

    def get(self, measurement_id):
        return next((r for r in self.records if r["measurement_id"] == measurement_id), None)

    def observe(self, payload):
        """Merge packets from one physical session; never merge separate weigh-ins."""
        recent = self.records[-20:]
        packet_id = payload.get("packet_id")
        if packet_id and any(packet_id in r.get("packet_ids", []) for r in recent):
            return None, False
        previous = recent[-1] if recent else None
        if not payload["stabilized"]:
            if previous:
                previous["closed"] = True
            return None, False
        merge = previous and 0 <= seconds_between(previous["last_seen"], payload["timestamp"]) < 45
        merge = merge and abs(previous["weight"] - payload["weight"]) <= 0.2
        merge = merge and (not previous.get("closed") or payload.get("load_removed"))
        if merge:
            record = previous
        else:
            record = {"measurement_id": uuid4().hex, "status": "collecting",
                      "timestamp": payload["timestamp"], "weight": payload["weight"],
                      "unit": "kg", "impedance": None, "packet_ids": [], "declined": []}
            self.records.append(record)
        record.update(last_seen=payload["timestamp"], rssi=payload.get("rssi"),
                      source_address=payload["source_address"],
                      closed=bool(payload.get("load_removed")))
        if payload.get("impedance"):
            record["impedance"] = payload["impedance"]
        record["has_impedance"] = record["impedance"] is not None
        if packet_id:
            record["packet_ids"] = (record["packet_ids"] + [packet_id])[-128:]
        return record, not bool(merge)

    def finalize(self, measurement_id, users):
        record = self.get(measurement_id)
        if not record or record["status"] != "collecting":
            return None
        candidates = []
        for user in users:
            if 'GT' in user and 'LT' in user:
                matches = user['GT'] <= record['weight'] <= user['LT']
            else:
                # Learn only from human-confirmed readings; automatic assignments
                # cannot gradually move the matching range onto another person.
                confirmed = [r['weight'] for r in self.records
                             if r['status'] == 'assigned' and r.get('user_name') == user['NAME']
                             and r.get('resolved_at')][-10:]
                matches = bool(confirmed) and abs(record['weight'] - median(confirmed)) <= 2.0
            if matches:
                candidates.append(user['NAME'])
        record["candidates"] = candidates or [u["NAME"] for u in users]
        if len(candidates) == 1:
            record.update(status="assigned", user_name=candidates[0])
        else:
            record["status"] = "pending"
        return record

    def resolve(self, measurement_id, user_name, decision, now, manual=False):
        record = self.get(measurement_id)
        if not record or record["status"] != "pending":
            return None
        if not manual and seconds_between(record["timestamp"], now) > 86400:
            return None
        if decision == "discard" and manual:
            record.update(status="discarded", resolved_at=now)
            return record
        if not manual and (user_name not in record.get("candidates", []) or user_name in record["declined"]):
            return None
        if decision == "decline":
            record["declined"].append(user_name)
        elif decision == "claim":
            record.update(status="assigned", user_name=user_name, resolved_at=now)
        else:
            return None
        return record
