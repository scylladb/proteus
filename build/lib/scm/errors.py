from __future__ import annotations

from pathlib import Path


def load_error_catalog(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split("\t", 1)
        if len(parts) != 2:
            continue
        code, desc = parts
        out[code.strip()] = desc.strip()
    return out


def decode_api_error(error_value: object, catalog: dict[str, str]) -> str:
    code = "" if error_value is None else str(error_value).strip()
    if not code:
        return ""
    desc = catalog.get(code)
    if desc:
        return f"{code}: {desc}"
    return code
