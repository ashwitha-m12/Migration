"""The common migration engine: Discover -> Backup -> Restore -> Validate."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from .base import FAILED, MANUAL, OK, SKIPPED, Result
from .registry import load

TOOL_VERSION = "1.1"
INCOMPLETE = "BACKUP_INCOMPLETE.txt"   # removed only when a backup finishes


def _stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _checksums(pkg: Path) -> dict:
    out = {}
    for p in sorted(pkg.rglob("*")):
        if p.is_file() and p.name not in ("manifest.json", INCOMPLETE) and not p.name.startswith("report_"):
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            out[p.relative_to(pkg).as_posix()] = h.hexdigest()
    return out


def write_report(pkg: Path, op: str, platform: str, results: list[Result]) -> dict:
    (pkg / f"report_{op}.json").write_text(json.dumps([asdict(r) for r in results], indent=2))
    counts = {s: sum(r.status == s for r in results) for s in (OK, FAILED, MANUAL, SKIPPED)}
    lines = [f"# {platform} - {op} report", "", f"Generated {dt.datetime.now():%Y-%m-%d %H:%M}", "",
             " | ".join(f"**{k}**: {v}" for k, v in counts.items()), ""]
    for title, status in (("Failed", FAILED), ("Needs manual action", MANUAL)):
        items = [r for r in results if r.status == status]
        if items:
            lines += [f"## {title}", ""] + [f"- `{r.kind}` **{r.name}** - {r.message}" for r in items] + [""]
    lines += ["## All items", "", "| Status | Kind | Name | Message |", "|---|---|---|---|"]
    lines += [f"| {r.status} | {r.kind} | {r.name} | {r.message.replace('|', '/')} |" for r in results]
    (pkg / f"report_{op}.md").write_text("\n".join(lines), encoding="utf-8")
    return counts


def _open(platform: str, side: str, log=None):
    c = load(platform)(side)
    if log:
        c.log = log
    c.connect()
    return c


def test_connection(platform: str, side: str) -> tuple[bool, str]:
    """Used by the app's 'Test connection' button."""
    try:
        c = _open(platform, side)
        try:
            n = len(c.discover())
        finally:
            c.close()
        return True, f"Connected - {n} resources visible"
    except Exception as e:
        return False, (str(e).strip().splitlines() or [type(e).__name__])[0][:300]


def discover(platform: str, side: str = "SRC"):
    c = _open(platform, side)
    try:
        return c.discover()
    finally:
        c.close()


def backup(platform: str, out_root: str = "Migration_Backups", log=print):
    pkg = Path(out_root) / f"{platform}_{_stamp()}"
    pkg.mkdir(parents=True)
    (pkg / INCOMPLETE).write_text("This backup did not finish. Do not restore it - run the backup again.")
    c = _open(platform, "SRC", log)
    try:
        log("Scanning the old account ...")
        inv = c.discover()
        (pkg / "inventory.json").write_text(json.dumps([asdict(r) for r in inv], indent=2, default=str))
        log(f"Found {len(inv)} resources. Backing up ...")
        results = c.backup(pkg)
    finally:
        c.close()
    manifest = {"platform": platform, "tool_version": TOOL_VERSION,
                "created_at": dt.datetime.now().isoformat(timespec="seconds"),
                "resource_count": len(inv), "files": _checksums(pkg)}
    (pkg / "manifest.json").write_text(json.dumps(manifest, indent=2))
    counts = write_report(pkg, "backup", platform, results)
    (pkg / INCOMPLETE).unlink()
    log(f"Backup saved to {pkg}")
    return pkg, results, counts


def verify(pkg: Path) -> dict:
    if (pkg / INCOMPLETE).exists() or not (pkg / "manifest.json").exists():
        raise RuntimeError(f"{pkg} is an unfinished backup (it stopped part-way). Run the backup again and use the new folder.")
    manifest = json.loads((pkg / "manifest.json").read_text())
    current = _checksums(pkg)
    bad = [f for f, h in manifest["files"].items() if current.get(f) != h]
    if bad:
        raise RuntimeError(f"Backup package is damaged: {len(bad)} file(s) changed or missing, e.g. {bad[:3]}")
    return manifest


def restore(pkg, log=print, only=None):
    pkg = Path(pkg)
    platform = verify(pkg)["platform"]
    c = _open(platform, "TGT", log)
    if only:
        c.only_parts = set(only)
    try:
        log(f"Restoring {platform} into the new account ...")
        results = c.restore(pkg)
    finally:
        c.close()
    return platform, results, write_report(pkg, "restore", platform, results)


def validate(pkg, log=print):
    pkg = Path(pkg)
    platform = verify(pkg)["platform"]
    c = _open(platform, "TGT", log)
    try:
        log("Comparing old (from backup) vs new account ...")
        results = c.validate(pkg)
    finally:
        c.close()
    return platform, results, write_report(pkg, "validate", platform, results)


def list_packages(out_root: str = "Migration_Backups", platform: str | None = None) -> list[dict]:
    """Finished backups, newest first (unfinished ones are skipped)."""
    root = Path(out_root)
    out = []
    if root.exists():
        for p in sorted(root.iterdir(), reverse=True):
            m = p / "manifest.json"
            if p.is_dir() and m.exists() and not (p / INCOMPLETE).exists():
                info = json.loads(m.read_text())
                if not platform or info.get("platform") == platform:
                    out.append({"path": p, **{k: v for k, v in info.items() if k != "files"}})
    return out


def migrate(platform: str, out_root: str = "Migration_Backups", log=print):
    pkg, _, b = backup(platform, out_root, log)
    _, _, r = restore(pkg, log)
    _, _, v = validate(pkg, log)
    return pkg, {"backup": b, "restore": r, "validate": v}
