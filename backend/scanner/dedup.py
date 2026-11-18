"""Event deduplication.

A reconnect can replay the same message, and a single transaction can legitimately contain
several trades. The key therefore combines the signature with a hash of the full canonical
payload: identical replays collide, distinct trades in one transaction do not.
"""

import hashlib
import json
from collections import OrderedDict
from typing import Any


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def dedup_key(source: str, event_type: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()[:32]
    signature = payload.get("signature")
    sig = signature if isinstance(signature, str) and len(signature) <= 88 else "-"
    return f"{source}:{event_type}:{sig}:{digest}"


class RecentKeys:
    """Bounded memory of recently seen keys (first line of defence before the DB constraint)."""

    def __init__(self, capacity: int = 100_000) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._keys: OrderedDict[str, None] = OrderedDict()

    def seen(self, key: str) -> bool:
        """Return True if the key was already seen; otherwise remember it and return False."""
        if key in self._keys:
            self._keys.move_to_end(key)
            return True
        self._keys[key] = None
        if len(self._keys) > self._capacity:
            self._keys.popitem(last=False)
        return False

    def __len__(self) -> int:
        return len(self._keys)
