"""Edit config.toml in place for the web interface, keeping its comments and layout."""
from __future__ import annotations

import json
import re
from dataclasses import asdict

SECTIONS = ["general", "stats", "entry", "tqs", "risk", "costs", "live", "walkforward"]
_KEY = re.compile(r"^(\s*)([A-Za-z_]\w*)(\s*=\s*)(.*)$")
_HEADER = re.compile(r"^\s*\[+\s*([\w.]+)\s*\]+\s*(#.*)?$")


def _split_comment(rest: str) -> tuple:
    quote = None
    for i, ch in enumerate(rest):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return rest[:i].rstrip(), rest[i:]
    return rest.rstrip(), ""


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def _kind(v) -> str:
    for t, name in ((bool, "bool"), (int, "int"), (float, "float"), (list, "list")):
        if isinstance(v, t):
            return name
    return "str"


def describe(text: str, cfg) -> list:
    """Sections with their fields, current values, types and the comment next to each key."""
    hints, section = {}, None
    for line in text.splitlines():
        h = _HEADER.match(line)
        if h:
            section = h.group(1)
            continue
        m = _KEY.match(line)
        if m and section:
            hints[(section, m.group(2))] = _split_comment(m.group(4))[1].lstrip("# ").strip()
    return [{"name": s, "fields": [{"key": k, "value": v, "type": _kind(v), "hint": hints.get((s, k), "")}
                                   for k, v in asdict(getattr(cfg, s)).items()]} for s in SECTIONS]


def coerce(cfg, values: dict) -> dict:
    """Convert values posted by the browser to the type each option already has."""
    out = {}
    for section, items in values.items():
        if section not in SECTIONS:
            raise ValueError(f"unknown section [{section}]")
        current = asdict(getattr(cfg, section))
        for key, v in items.items():
            if key not in current:
                raise ValueError(f"unknown option {section}.{key}")
            kind = _kind(current[key])
            try:
                if kind == "bool":
                    if not isinstance(v, bool):
                        raise ValueError
                elif kind == "int":
                    if float(v) != int(float(v)):
                        raise ValueError
                    v = int(float(v))
                elif kind == "float":
                    v = float(v)
                elif kind == "list":
                    if not isinstance(v, list):
                        raise ValueError
                else:
                    v = str(v)
            except (TypeError, ValueError):
                raise ValueError(f"{section}.{key}: expected {kind}") from None
            out.setdefault(section, {})[key] = v
    return out


def set_values(text: str, values: dict) -> str:
    lines, section, done = text.splitlines(), None, set()
    for i, line in enumerate(lines):
        h = _HEADER.match(line)
        if h:
            section = h.group(1)
            continue
        m = _KEY.match(line)
        if not m or section not in values or m.group(2) not in values[section]:
            continue
        key = m.group(2)
        _, comment = _split_comment(m.group(4))
        new = m.group(1) + key + m.group(3) + _fmt(values[section][key])
        if comment:
            col = line.index(comment)  # keep the comment where it was when the new value fits
            new = new.ljust(col) + comment if len(new) < col else new + "  " + comment
        lines[i] = new
        done.add((section, key))
    for section, items in values.items():
        missing = [k for k in items if (section, k) not in done]
        if not missing:
            continue
        add = [f"{k} = {_fmt(items[k])}" for k in missing]
        at = next((i for i, l in enumerate(lines) if (h := _HEADER.match(l)) and h.group(1) == section
                   and l.strip().startswith(f"[{section}]")), None)
        if at is None:
            lines += ["", f"[{section}]"] + add
        else:
            lines[at + 1:at + 1] = add
    return "\n".join(lines) + "\n"


def set_pairs(text: str, pairs: list) -> str:
    """Replace every [[pairs]] block with the given list."""
    lines, out, first, skipping = text.splitlines(), [], None, False
    for line in lines:
        if line.strip().startswith("[[pairs]]"):
            skipping = True
            first = len(out) if first is None else first
            continue
        if skipping and line.lstrip().startswith("["):
            skipping = False
        if not skipping:
            out.append(line)
    block = []
    for p in pairs:
        block += ["[[pairs]]", f"a = {_fmt(p['a'])}", f"b = {_fmt(p['b'])}", f"sector = {_fmt(p['sector'])}", ""]
    first = len(out) if first is None else first
    out[first:first] = block
    return "\n".join(out).rstrip("\n") + "\n"
