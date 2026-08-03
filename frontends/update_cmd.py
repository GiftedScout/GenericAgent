"""Deterministic backend for the ``/update`` slash command.

The normal update path has no need for an LLM: it fetches, explicitly merges
``origin/main``, validates the checkout, and fast-forwards the ``myfork``
mirror.  Ambiguous states deliberately return a short prompt to the main agent
instead of guessing a conflict resolution or force-pushing a fork.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


_ROOT = Path(__file__).resolve().parents[1]
_Run = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class UpdateOutcome:
    """Result rendered directly or handed back to the normal agent loop."""

    report: str | None = None
    prompt: str | None = None


def _run(argv: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), cwd=cwd, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=180, check=False,
    )


def _text(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "").strip()


def _git(runner: _Run, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return runner(("git", "-C", str(root), *args), root)


def _fail(action: str, result: subprocess.CompletedProcess[str]) -> UpdateOutcome:
    detail = _text(result).splitlines()[-1:] or ["no command output"]
    return UpdateOutcome(report=f"❌ /update stopped: {action}: {detail[0]}")


def _attention_prompt(reason: str, root: Path, note: str = "") -> UpdateOutcome:
    extra = f"\nUser note: {note}" if note else ""
    return UpdateOutcome(prompt=(
        f"/update backend stopped safely: {reason}. Work at `{root}`. "
        "Read `memory/git_sync_sop.md`, inspect the actual Git state, and make "
        "only the minimal semantic repair. Do not discard either side, use "
        "blind ours/theirs, or force-push. Once clean, run `python3 -m unittest "
        "discover -s tests -p 'test_*.py'`; then tell the user only the outcome "
        "and concise user-visible update summary."
        f"{extra}"
    ))


def _validate(runner: _Run, root: Path) -> str | None:
    """Return a concise validation failure, or ``None`` when all checks pass."""
    unmerged = _git(runner, root, "diff", "--name-only", "--diff-filter=U")
    if unmerged.returncode or _text(unmerged):
        return "unmerged paths remain"

    files = _git(runner, root, "ls-files", "-z", "--", "*.py")
    if files.returncode:
        return "could not enumerate tracked Python files"
    python_files = [name for name in (files.stdout or "").split("\0") if name]
    if python_files:
        compiled = runner((sys.executable, "-m", "py_compile", *python_files), root)
        if compiled.returncode:
            return "tracked Python compilation failed"

    tests = runner((sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"), root)
    if tests.returncode:
        return "unit tests failed"
    return None


def run_update(note: str = "", *, root: Path = _ROOT, runner: _Run = _run) -> UpdateOutcome:
    """Synchronize a clean main checkout, without irreversible guesswork."""
    root = Path(root).resolve()
    top = _git(runner, root, "rev-parse", "--show-toplevel")
    if top.returncode:
        return _fail("not a Git checkout", top)
    root = Path(_text(top)).resolve()

    branch = _git(runner, root, "branch", "--show-current")
    if branch.returncode or _text(branch) != "main":
        return UpdateOutcome(report="❌ /update requires the checked-out branch to be main.")
    dirty = _git(runner, root, "status", "--porcelain")
    if dirty.returncode:
        return _fail("could not read worktree status", dirty)
    if _text(dirty):
        return _attention_prompt("the worktree has uncommitted changes", root, note)
    in_progress = _git(runner, root, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    if in_progress.returncode == 0:
        return _attention_prompt("a merge is already in progress", root, note)

    old = _git(runner, root, "rev-parse", "HEAD")
    if old.returncode:
        return _fail("could not resolve HEAD", old)
    old_head = _text(old)
    for remote in ("origin", "myfork"):
        fetched = _git(runner, root, "fetch", "--prune", remote)
        if fetched.returncode:
            return _fail(f"fetch {remote}", fetched)

    upstream = _git(runner, root, "rev-parse", "origin/main")
    mirror = _git(runner, root, "rev-parse", "myfork/main")
    if upstream.returncode:
        return _fail("origin/main is unavailable after fetch", upstream)
    if mirror.returncode:
        return _fail("myfork/main is unavailable after fetch", mirror)

    incoming = _git(runner, root, "log", "--format=%s", "-6", f"{old_head}..origin/main")
    if incoming.returncode:
        return _fail("could not inspect upstream changes", incoming)
    ahead = _git(runner, root, "merge-base", "--is-ancestor", "HEAD", "origin/main")
    behind = _git(runner, root, "merge-base", "--is-ancestor", "origin/main", "HEAD")
    if ahead.returncode == 0:
        merged = _git(runner, root, "merge", "--ff-only", "origin/main")
        mode = "fast-forwarded"
    elif behind.returncode == 0:
        merged = subprocess.CompletedProcess([], 0, "")
        mode = "already current"
    else:
        merged = _git(runner, root, "merge", "--no-edit", "origin/main")
        mode = "merged"
    if merged.returncode:
        unmerged = _git(runner, root, "diff", "--name-only", "--diff-filter=U")
        if _text(unmerged):
            return _attention_prompt("origin/main merge has conflicts", root, note)
        return _fail("merge origin/main", merged)

    validation_error = _validate(runner, root)
    if validation_error:
        return _attention_prompt(validation_error, root, note)

    head = _git(runner, root, "rev-parse", "HEAD")
    if head.returncode:
        return _fail("could not resolve updated HEAD", head)
    head_id = _text(head)
    mirror_is_ancestor = _git(runner, root, "merge-base", "--is-ancestor", "myfork/main", "HEAD")
    if _text(mirror) != head_id:
        if mirror_is_ancestor.returncode != 0:
            return _attention_prompt("myfork/main diverges and would require a force push", root, note)
        pushed = _git(runner, root, "push", "myfork", "HEAD:main")
        if pushed.returncode:
            return _fail("push myfork/main", pushed)
        fetched = _git(runner, root, "fetch", "myfork", "main")
        if fetched.returncode:
            return _fail("verify myfork/main", fetched)
    verified = _git(runner, root, "rev-parse", "myfork/main")
    if verified.returncode or _text(verified) != head_id:
        return UpdateOutcome(report="❌ /update stopped: myfork/main did not verify at local main.")

    subjects = [line for line in _text(incoming).splitlines() if line]
    changes = "；".join(subjects) if subjects else "无上游新提交"
    suffix = "；更多更新已省略" if len(subjects) == 6 else ""
    return UpdateOutcome(
        report=(f"✅ 更新完成 · {mode} · main/myfork {head_id[:10]}\n"
                f"上游更新：{changes}{suffix}\n"
                "验证：已通过 Python 编译与单元测试。")
    )


def handle(agent, body: str, display_queue) -> str | None:
    outcome = run_update(body)
    if outcome.prompt:
        return outcome.prompt
    display_queue.put({"done": outcome.report or "❌ /update produced no result", "source": "system"})
    return None


def install(cls) -> None:
    """Install /update before the normal agent run loop sees the command."""
    orig = cls._handle_slash_cmd
    if getattr(orig, "_update_patched", False):
        return

    def patched(self, raw_query, display_queue):
        text = (raw_query or "").strip()
        if text == "/update" or text.startswith("/update ") or text.startswith("/update\t"):
            result = handle(self, text[len("/update"):].strip(), display_queue)
            return None if result is None else result
        return orig(self, raw_query, display_queue)

    patched._update_patched = True
    cls._handle_slash_cmd = patched
