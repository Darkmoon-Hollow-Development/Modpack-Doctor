"""Small shared helpers."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _strip_comments(text: str) -> str:
    out = []
    i, n = 0, len(text)
    in_str = esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and text[i + 1 : i + 2] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and text[i + 1 : i + 2] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    out = []
    i, n = 0, len(text)
    in_str = esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
        elif c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
            out.append(c)
        else:
            out.append(c)
        i += 1
    return "".join(out)


def lenient_json_loads(text: str) -> Any:
    """json.loads that also accepts comments, trailing commas and a BOM (like most mod loaders)."""
    text = text.lstrip("﻿")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_strip_trailing_commas(_strip_comments(text)))


def safe_filename(name: str, default: str = "file") -> str:
    base = re.sub(r"[^A-Za-z0-9._+\- ]", "_", Path(str(name)).name).strip(" .")
    return base or default


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # pragma: no cover
