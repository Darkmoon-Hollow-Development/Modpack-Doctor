"""Loading the rule database (bundled defaults + optional user file)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .util import lenient_json_loads

_DEFAULT = Path(__file__).parent / "data" / "rules.json"
_LIST_KEYS = ("conflicts", "redundant", "recommended", "config_rules", "ram_guidance")


def load_rules(extra_path: Optional[str] = None) -> dict:
    rules = json.loads(_DEFAULT.read_text(encoding="utf-8"))
    if extra_path:
        extra = lenient_json_loads(Path(extra_path).read_text(encoding="utf-8"))
        for key in _LIST_KEYS:
            rules.setdefault(key, []).extend(extra.get(key, []))
        rules.setdefault("slug_overrides", {}).update(extra.get("slug_overrides", {}))
    return rules
