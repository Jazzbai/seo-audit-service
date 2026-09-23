"""Small pure helpers shared by the WordPress and WooCommerce clients."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any


_NUMERIC_ID_EXPRESSIONS = frozenset({r"\d+", r"[\d]+", r"[0-9]+"})


def is_numeric_id_placeholder(value: Any) -> bool:
    """Return whether a REST route placeholder is a numeric ``id``."""

    if value == "{id}":
        return True
    if not isinstance(value, str):
        return False
    match = re.fullmatch(r"\(\?P<id>(?P<expression>[^)]+)\)", value)
    return bool(match and match.group("expression") in _NUMERIC_ID_EXPRESSIONS)


def json_clone(value: Any) -> Any:
    """Return a JSON-compatible deep copy without retaining caller objects."""

    return json.loads(json.dumps(value, ensure_ascii=False))


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def raw_field(value: Any) -> Any:
    if isinstance(value, Mapping):
        if "raw" in value:
            return value["raw"]
        if "rendered" in value:
            return value["rendered"]
    return value


def text_value(value: Any) -> str:
    value = raw_field(value)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    value = re.sub(r"[\s_-]+", "-", value, flags=re.UNICODE).strip("-")
    return value[:180] or "forge-post"


def operation_key_header(operation_key: str) -> str:
    if not isinstance(operation_key, str) or not operation_key.strip():
        raise ValueError("operation_key must be a non-empty string")
    if len(operation_key) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in operation_key):
        raise ValueError("operation_key contains invalid characters")
    return operation_key


def walk_mapping_keys(value: Any, prefix: str = ""):
    """Yield ``(path, key)`` for every mapping key, including nested values."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            yield path, key_text
            yield from walk_mapping_keys(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_mapping_keys(child, f"{prefix}[{index}]")


def find_key(value: Any, names: set[str]) -> str | None:
    normalized = {name.lower().replace("-", "_") for name in names}
    for path, key in walk_mapping_keys(value):
        candidate = key.lower().replace("-", "_")
        if candidate in normalized:
            return path
    return None


def copy_json(value: Any) -> Any:
    # deepcopy preserves integer ids and is safe after the response was parsed
    # as JSON; json_clone is used at public boundaries where strict JSON is
    # important.
    return copy.deepcopy(value)
