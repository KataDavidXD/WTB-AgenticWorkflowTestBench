"""Stable identity helpers for durable external-runtime attempts."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any


def canonical_json_sha256(value: Mapping[str, Any]) -> str:
    """Hash one strict JSON object identically across process restarts.

    Durable identities must never depend on ``repr`` fallbacks, insertion
    order, non-finite numbers, or Python-only container/key semantics.
    """
    if not isinstance(value, Mapping):
        raise TypeError("durable runtime input must be a JSON object")
    normalized = _normalize_json(value, path="$", active=set())
    encoded = json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_json(value: Any, *, path: str, active: set[int]) -> Any:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a cyclic JSON object")
        active.add(identity)
        try:
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"{path} contains a non-string JSON object key")
                normalized[key] = _normalize_json(
                    item,
                    path=f"{path}.{key}",
                    active=active,
                )
            return normalized
        finally:
            active.remove(identity)
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a cyclic JSON array")
        active.add(identity)
        try:
            return [
                _normalize_json(item, path=f"{path}[{index}]", active=active)
                for index, item in enumerate(value)
            ]
        finally:
            active.remove(identity)
    raise TypeError(
        f"{path} contains non-JSON value of type {type(value).__name__}"
    )


__all__ = ["canonical_json_sha256"]
