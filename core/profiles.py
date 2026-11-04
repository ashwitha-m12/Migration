"""
Saved account profiles for the app.
Normal settings -> profiles/<name>.json
Passwords/tokens -> the operating system's credential store (Windows Credential Manager,
macOS Keychain) via `keyring`. They are never written to a file.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SERVICE = "cloud-account-migration"
DIR = Path("profiles")


def _keyring():
    try:
        import keyring
        keyring.get_password(SERVICE, "__probe__")       # fails fast if no backend is available
        return keyring
    except Exception:
        return None


def secrets_supported() -> bool:
    return _keyring() is not None


def _file(name: str) -> Path:
    return DIR / f"{re.sub(r'[^A-Za-z0-9_-]', '_', name)}.json"


def list_profiles(platform: str) -> list[str]:
    if not DIR.exists():
        return []
    out = []
    for p in sorted(DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text())
            if data.get("platform") == platform:
                out.append(data.get("name", p.stem))
        except Exception:
            pass
    return out


def save(name: str, platform: str, fields, values: dict) -> str:
    """values = {"SRC": {key: value}, "TGT": {...}}. Returns a status message."""
    kr = _keyring()
    secret_keys = {f.key for f in fields if f.secret}
    data = {"name": name, "platform": platform, "values": {}}
    lost = []
    for side, kv in values.items():
        for key, val in kv.items():
            if key in secret_keys:
                if not val:
                    continue
                if kr:
                    kr.set_password(SERVICE, f"{name}:{platform}:{side}:{key}", val)
                else:
                    lost.append(f"{side}_{key}")
            else:
                data["values"].setdefault(side, {})[key] = val
    DIR.mkdir(exist_ok=True)
    _file(name).write_text(json.dumps(data, indent=2))
    if lost:
        return "Saved, but passwords were NOT saved (no credential store on this computer) - type them each time."
    return "Saved. Passwords are stored in your system's credential manager."


def load(name: str, platform: str, fields) -> dict:
    data = json.loads(_file(name).read_text())
    values = {side: dict(kv) for side, kv in data.get("values", {}).items()}
    kr = _keyring()
    if kr:
        for side in ("SRC", "TGT"):
            for f in fields:
                if f.secret:
                    v = kr.get_password(SERVICE, f"{name}:{platform}:{side}:{f.key}")
                    if v:
                        values.setdefault(side, {})[f.key] = v
    return values


def delete(name: str, platform: str, fields) -> None:
    kr = _keyring()
    if kr:
        for side in ("SRC", "TGT"):
            for f in fields:
                if f.secret:
                    try:
                        kr.delete_password(SERVICE, f"{name}:{platform}:{side}:{f.key}")
                    except Exception:
                        pass
    _file(name).unlink(missing_ok=True)
