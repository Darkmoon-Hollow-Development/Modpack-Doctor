"""Version parsing and range matching.

Two range syntaxes are used by Minecraft mod loaders:

* Maven-style intervals (Forge / NeoForge):  ``[1.20,1.21)``, ``[47,)``, ``[1.20.1]``
* Fabric / Quilt predicates:  ``>=0.14.21``, ``~1.20.1``, ``^1.2``, ``1.20.x``, ``*``,
  space = AND, ``||`` or a JSON list = OR.

Mod authors are not consistent with version strings, so parsing is deliberately
tolerant. Every ``*_matches`` function returns ``None`` when the version is unknown
so callers can avoid false alarms.
"""
from __future__ import annotations

import re
from functools import total_ordering
from typing import Any, Optional

_TOKEN_RE = re.compile(r"\d+|[A-Za-z]+")
_PRE = {"snapshot": 0, "dev": 0, "alpha": 0, "beta": 1, "pre": 2, "preview": 2, "rc": 3, "cr": 3}
_IGNORE = {"release", "final", "ga", "stable"}
_END = (1, 0)


def _tokens(raw: str) -> list:
    s = (raw or "").strip()
    if s[:1] in ("v", "V") and s[1:2].isdigit():
        s = s[1:]
    s = s.split("+", 1)[0]  # drop build metadata
    out = []
    for tok in _TOKEN_RE.findall(s):
        if tok.isdigit():
            out.append((1, int(tok)))
        else:
            low = tok.lower()
            if low in _PRE:
                out.append((0, _PRE[low]))
            elif low in _IGNORE:
                continue
            else:
                out.append((2, low))
    return out


def _norm(tokens: list) -> list:
    t = list(tokens)
    while t and t[-1] == _END:
        t.pop()
    return t


@total_ordering
class Version:
    """Tolerant, semver-flavoured version. ``1.2 == 1.2.0``; ``1.2.0-beta < 1.2.0``."""

    def __init__(self, raw: str):
        self.raw = str(raw)
        self._t = _norm(_tokens(self.raw))

    def _cmp(self, other: "Version") -> int:
        a, b = self._t, other._t
        for i in range(max(len(a), len(b))):
            x = a[i] if i < len(a) else _END
            y = b[i] if i < len(b) else _END
            if x != y:
                return -1 if x < y else 1
        return 0

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._cmp(other) == 0

    def __lt__(self, other: "Version") -> bool:
        return self._cmp(other) < 0

    def __hash__(self) -> int:
        return hash(tuple(self._t))

    def __repr__(self) -> str:  # pragma: no cover
        return f"Version({self.raw!r})"


def numeric_parts(raw: str) -> list:
    """Leading numeric components: ``1.20.1-forge`` -> ``[1, 20, 1]``."""
    out = []
    for kind, val in _tokens(raw):
        if kind != 1:
            break
        out.append(val)
    return out


def is_release_like(raw: Optional[str]) -> bool:
    return bool(raw) and re.fullmatch(r"\d+(\.\d+)*", raw.strip()) is not None


def sort_key(raw: Optional[str]) -> Version:
    return Version(raw or "0")


# --------------------------------------------------------------------------- maven

_INTERVAL = re.compile(r"([\[\(])\s*([^\[\]\(\),]*)\s*(,)?\s*([^\[\]\(\),]*)\s*([\]\)])")


def maven_matches(rng: Any, version: Optional[str]) -> Optional[bool]:
    if version is None:
        return None
    if rng is None:
        return True
    s = str(rng).strip()
    if s in ("", "*"):
        return True
    found = _INTERVAL.findall(s)
    if not found:
        return True  # bare version = "soft" requirement in Maven, never rejects
    v = Version(version)
    for lo_b, lo, comma, hi, hi_b in found:
        lo, hi = lo.strip(), hi.strip()
        if not comma:  # [1.20.1] exact
            if lo and v == Version(lo):
                return True
            continue
        ok = True
        if lo:
            lov = Version(lo)
            ok = v > lov if lo_b == "(" else v >= lov
        if ok and hi:
            hiv = Version(hi)
            ok = v < hiv if hi_b == ")" else v <= hiv
        if ok:
            return True
    return False


# --------------------------------------------------------------------------- fabric

_TERM_RE = re.compile(r"(>=|<=|>|<|=|~|\^)?\s*([^\s<>=~^|]+)")


def _wild_prefix(ver: str) -> Optional[list]:
    parts = ver.split(".")
    if not any(p in ("x", "X", "*") for p in parts):
        return None
    prefix = []
    for p in parts:
        if p in ("x", "X", "*"):
            return prefix
        if not p.isdigit():
            return None
        prefix.append(int(p))
    return prefix


def _fabric_term(op: str, ver: str, v: Version, raw_v: str) -> bool:
    if ver == "*":
        return True
    wp = _wild_prefix(ver)
    if wp is not None:
        pv = numeric_parts(raw_v)
        pv = pv + [0] * (len(wp) - len(pv))
        return pv[: len(wp)] == wp
    target = Version(ver)
    if op in ("", "="):
        return v == target
    if op == ">=":
        return v >= target
    if op == ">":
        return v > target
    if op == "<=":
        return v <= target
    if op == "<":
        return v < target
    nums = numeric_parts(ver) or [0]
    if op == "~":
        up = [nums[0] + 1] if len(nums) == 1 else [nums[0], nums[1] + 1]
    else:  # ^
        up = [nums[0] + 1]
    return v >= target and v < Version(".".join(str(n) for n in up))


def fabric_matches(pred: Any, version: Optional[str]) -> Optional[bool]:
    if version is None:
        return None
    if pred is None:
        return True
    if isinstance(pred, (list, tuple)):
        if not pred:
            return True
        return any(bool(fabric_matches(p, version)) for p in pred)
    if isinstance(pred, dict):
        if "all" in pred:
            return all(bool(fabric_matches(p, version)) for p in pred["all"])
        if "any" in pred:
            return any(bool(fabric_matches(p, version)) for p in pred["any"])
        return True
    s = str(pred).strip()
    if s in ("", "*"):
        return True
    v = Version(version)
    for alt in s.split("||"):
        terms = _TERM_RE.findall(alt)
        if terms and all(_fabric_term(op or "", ver, v, version) for op, ver in terms):
            return True
    return False


def matches(rng: Any, syntax: str, version: Optional[str]) -> Optional[bool]:
    return maven_matches(rng, version) if syntax == "maven" else fabric_matches(rng, version)


def format_range(rng: Any) -> str:
    if rng is None:
        return "any version"
    if isinstance(rng, (list, tuple)):
        return " or ".join(str(r) for r in rng) or "any version"
    if isinstance(rng, dict):
        key = "all" if "all" in rng else "any"
        joiner = " and " if key == "all" else " or "
        return joiner.join(str(r) for r in rng.get(key, [])) or "any version"
    s = str(rng).strip()
    return s if s and s != "*" else "any version"
