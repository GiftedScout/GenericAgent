"""OpenSSH askpass helper: credential file -> private authentication pipe only.

Invoked by SSHClient, never registered as an agent tool. Do not log secrets.
"""
import os
from pathlib import Path
import sys


def main():
    prompt = " ".join(sys.argv[1:]).lower()
    # Never answer host-key confirmation or private-key passphrase prompts.
    if "password" not in prompt or "passphrase" in prompt:
        return 1
    try:
        value = Path(os.environ["GA_SSH_PASSWORD_FILE"]).read_text().rstrip("\r\n")
        if not value or "\n" in value or "\r" in value or "\x00" in value:
            return 1
        sys.stdout.write(value + "\n")
        sys.stdout.flush()
        return 0
    except (OSError, KeyError, UnicodeError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
