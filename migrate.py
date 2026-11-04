"""
Cloud Account Migration - command line.

  python migrate.py platforms
  python migrate.py discover  snowflake            # what's in the OLD account
  python migrate.py backup    snowflake            # save everything to Migration_Backups/
  python migrate.py restore   Migration_Backups/snowflake_20261103_120000
  python migrate.py validate  Migration_Backups/snowflake_20261103_120000
  python migrate.py migrate   snowflake            # backup + restore + validate in one go
"""
import argparse
import sys
from collections import Counter

from dotenv import load_dotenv

from core import engine
from core.registry import PLUGINS


def summary(counts: dict, pkg) -> None:
    print("\n  " + "   ".join(f"{k}: {v}" for k, v in counts.items()))
    print(f"  Full report: {pkg}")


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description="Move your work from an expiring cloud account to a new one.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("platforms")
    d = sub.add_parser("discover"); d.add_argument("platform"); d.add_argument("--side", choices=["SRC", "TGT"], default="SRC")
    for name in ("backup", "migrate"):
        s = sub.add_parser(name); s.add_argument("platform"); s.add_argument("--out", default="Migration_Backups")
    s = sub.add_parser("restore"); s.add_argument("package")
    s.add_argument("--only", nargs="*", help="restore only these parts, e.g. --only workspaces")
    s = sub.add_parser("validate"); s.add_argument("package")
    a = p.parse_args()

    try:
        if a.cmd == "platforms":
            print("\n".join(PLUGINS))
        elif a.cmd == "discover":
            res = engine.discover(a.platform, a.side)
            for kind, n in Counter(r.kind for r in res).items():
                print(f"  {kind:<22}{n}")
            for r in res:
                print(f"  - {r.kind}: {r.name} {r.details or ''}")
        elif a.cmd == "backup":
            pkg, _, counts = engine.backup(a.platform, a.out)
            summary(counts, pkg / "report_backup.md")
        elif a.cmd == "restore":
            _, _, counts = engine.restore(a.package, only=a.only)
            summary(counts, f"{a.package}/report_restore.md")
        elif a.cmd == "validate":
            _, _, counts = engine.validate(a.package)
            summary(counts, f"{a.package}/report_validate.md")
        elif a.cmd == "migrate":
            pkg, all_counts = engine.migrate(a.platform, a.out)
            for op, counts in all_counts.items():
                print(f"\n[{op}]", end="")
                summary(counts, pkg / f"report_{op}.md")
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
