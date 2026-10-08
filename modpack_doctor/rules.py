"""Loading the rule database (bundled defaults + optional user file)."""
from __future__ import annotations

import json
import pkgutil
from pathlib import Path
from typing import Optional

from .util import lenient_json_loads

_LIST_KEYS = ("conflicts", "redundant", "recommended", "config_rules", "ram_guidance")


def load_rules(extra_path: Optional[str] = None) -> dict:
    # pkgutil also works when the app runs from inside a single-file .pyz archive
    rules = json.loads(pkgutil.get_data("modpack_doctor", "data/rules.json").decode("utf-8"))
    if extra_path:
        extra = lenient_json_loads(Path(extra_path).read_text(encoding="utf-8"))
        for key in _LIST_KEYS:
            rules.setdefault(key, []).extend(extra.get(key, []))
        rules.setdefault("slug_overrides", {}).update(extra.get("slug_overrides", {}))
    return rules
