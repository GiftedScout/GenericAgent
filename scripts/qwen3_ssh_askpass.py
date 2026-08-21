#!/usr/bin/env python3
"""SSH_ASKPASS helper. The password never enters command arguments or GA config."""
from pathlib import Path
import sys

try:
    password = Path("/home/pushuai/sshpassword").read_text(encoding="utf-8").splitlines()[0]
except (OSError, IndexError) as exc:
    print(f"Unable to obtain SSH password: {exc}", file=sys.stderr)
    raise SystemExit(1)
print(password)
