"""Command-line entry point for durable-session retention maintenance.

Usage::

    python -m frontends.session_janitor              # read-only plan
    python -m frontends.session_janitor --apply      # archive eligible hot rows

It only considers UUID-backed rows in ``SessionStore``; legacy model-response
logs are neither enumerated nor changed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from frontends.session_storage import SessionStore


def main(argv: list[str] | None = None, *, store: SessionStore | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ga session-janitor",
        description="Plan or apply retention archiving for durable GA sessions.",
    )
    parser.add_argument("--apply", action="store_true",
                        help="archive eligible UUID-backed hot sessions (default: read-only dry run)")
    parser.add_argument("--root", type=Path,
                        help="storage root; intended for tests and explicit maintenance")
    args = parser.parse_args(argv)
    if store is not None and args.root is not None:
        parser.error("--root cannot be combined with an injected store")

    result = (store or SessionStore(args.root)).janitor(apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["skipped_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
