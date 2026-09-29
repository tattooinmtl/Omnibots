"""python -m omnibots.doctor [--no-fix] [--online] [--install-deps] [--only GROUP ...] [--json] [--all]"""

from __future__ import annotations

import argparse
import json
import sys

from omnibots.doctor.doctor import GROUPS, run_doctor


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m omnibots.doctor",
                                 description="Check, set up and repair everything OmniBots needs (see docs/DOCTOR.md).")
    ap.add_argument("--no-fix", action="store_true", help="only check; repair nothing (the report is still saved to logs/doctor.json)")
    ap.add_argument("--online", action="store_true", help="also test each provider key against its server")
    ap.add_argument("--install-deps", action="store_true", help="pip-install missing Python packages")
    ap.add_argument("--only", nargs="+", choices=GROUPS, help="run only these groups")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--all", action="store_true", help="list the checks that passed too")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")     # a cp1252 console can't show → or ·
    except (AttributeError, ValueError):
        pass
    rep = run_doctor(fix=not a.no_fix, online=a.online, install_deps=a.install_deps, groups=a.only)
    if a.json:
        print(json.dumps(rep.to_dict(), indent=2))
    else:
        print(rep.text(all_lines=a.all))
    return 1 if rep.worst == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
