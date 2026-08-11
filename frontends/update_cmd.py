"""Deterministic backend for the ``/update`` slash command.

The Git transition is deterministic: it fetches, explicitly merges
``origin/main``, validates the checkout, and fast-forwards the ``myfork``
mirror.  A successful transition is handed to the normal agent once more so
that the same LLM can produce the concise user-facing update summary.
Ambiguous states deliberately return a prompt instead of guessing a conflict
resolution or force-pushing a fork.
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
    system: str | None = None
    diff: str | None = None


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


_MAX_CONFLICT_DIFF = 24000


def _clip_multiline(text: str, limit: int = _MAX_CONFLICT_DIFF) -> str:
    """Keep a conflict prompt bounded without collapsing its diff into one line."""
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit("\n", 1)[0]
    return clipped + "\n... [diff truncated by /update backend] ..."


def _conflict_prompt(
    reason: str,
    root: Path,
    runner: _Run,
    note: str = "",
    conflict_files: str = "",
) -> UpdateOutcome:
    """Return a reviewable conflict snapshot and a mandatory human handoff."""
    if not conflict_files:
        files_result = _git(runner, root, "diff", "--name-only", "--diff-filter=U")
        conflict_files = _text(files_result)

    diff_result = _git(runner, root, "diff", "--cc", "--unified=3")
    diff = _text(diff_result)
    if not diff:
        # Some Git versions/configurations do not emit combined hunks for all
        # unmerged index states.  The ordinary diff is still a direct backend
        # snapshot and is preferable to asking the LLM to invent one.
        fallback = _git(runner, root, "diff", "--no-ext-diff", "--unified=3")
        diff = _text(fallback)
    if not diff:
        diff = "(Git returned no diff hunks; inspect the conflict markers in the files above.)"

    files_block = "\n".join(f"- {line}" for line in conflict_files.splitlines() if line)
    if not files_block:
        files_block = "- (Git did not return conflict filenames; inspect `git status`.)"
    extra = f"\n用户附注：{note}" if note else ""
    system = (
        "⚠️ /update 检测到需要人工决策的 Git 合并冲突\n"
        f"原因：{reason}\n"
        "冲突文件（后端直接读取）：\n"
        f"{files_block}\n"
        "以下为后端直接采集的多行 combined diff：\n"
        f"```diff\n{_clip_multiline(diff)}\n```"
    )
    return UpdateOutcome(
        system=system,
        diff=_clip_multiline(diff),
        prompt=(
            "[/update 检测到 Git 合并冲突，冲突快照已由 UI 直接展示]\n"
            f"原因：{reason}\n"
            f"冲突文件：\n{files_block}\n"
            "请用简洁中文说明冲突位于哪些文件/逻辑，不要复述或重写 UI 已展示的 diff，"
            "然后必须调用 `ask_user`，询问用户应如何取舍或融合。建议 question 为："
            "‘请确认上述冲突的处理策略；是否按语义融合并继续更新？’，"
            "candidates 可为：‘按语义融合并继续更新’、‘我先处理冲突，暂不继续’、"
            "‘放弃本次更新’。\n"
            "收到用户回答后，按用户明确策略和实际代码语义修复冲突，完成验证与更新；"
            "最终只向用户简洁汇报冲突处理结果及本次上游新增的修复、优化或功能。"
            f"{extra}"
        ),
    )


def _success_prompt(report: str) -> str:
    """Give only upstream facts to the LLM that writes the final report."""
    marker = "上游更新："
    changes = report.split(marker, 1)[1] if marker in report else "无上游新提交"
    changes = changes.split("\n验证：", 1)[0].strip() or "无上游新提交"
    return (
        "[/update 后端已完成确定性同步]\n"
        "下面是后端从 origin/main 采集的本次上游变更事实，仅供归纳：\n"
        f"{changes}\n\n"
        "请用当前这个 LLM 向用户做最终的简洁中文汇报。只概括本次上游新增的修复、优化或功能；"
        "不要复述本地已有提交，不要逐字照抄内部 Git 状态、哈希、命令或测试过程，也不要长篇解释。"
        "如果没有上游新提交，明确说本次没有上游更新。不要再调用工具。"
    )


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
    in_progress = _git(runner, root, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    if in_progress.returncode == 0:
        unmerged = _git(runner, root, "diff", "--name-only", "--diff-filter=U")
        if _text(unmerged):
            return _conflict_prompt(
                "a merge is already in progress", root, runner, note, _text(unmerged)
            )
        return _attention_prompt("a merge is already in progress", root, note)
    if _text(dirty):
        return _attention_prompt("the worktree has uncommitted changes", root, note)

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
            return _conflict_prompt(
                "origin/main merge has conflicts", root, runner, note, _text(unmerged)
            )
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
    if subjects:
        changes = "\n".join(f"- {subject}" for subject in subjects)
        if len(subjects) == 6:
            changes += "\n- 更多上游提交已省略"
    else:
        changes = "- 无上游新提交"
    return UpdateOutcome(
        report=(f"✅ 更新完成 · {mode} · main/myfork {head_id[:10]}\n"
                f"上游更新：\n{changes}\n"
                "验证：已通过 Python 编译与单元测试。")
    )


def handle(agent, body: str, display_queue) -> str | None:
    outcome = run_update(body)
    if outcome.prompt:
        # Conflict snapshots are UI-owned and must not terminate the ordinary
        # agent turn.  Enqueue them first, then return the prompt so the same
        # agent/LLM can explain the conflict and call ask_user.
        if outcome.system is not None:
            display_queue.put({
                "update_notice": outcome.system,
                "diff": outcome.diff or "",
                "source": "system",
            })
            # Keep the exact backend prompt for the user's next answer.  The
            # current turn must not consume it; the run-loop skips it once.
            agent._pending_update_prompt = outcome.prompt
            agent._update_conflict_just_shown = True
        return outcome.prompt
    report = outcome.report or "❌ /update produced no result"
    if report.startswith("✅ 更新完成"):
        # The deterministic Git work is done.  Ask the same LLM for one
        # concise Chinese explanation, with no tools and no long tool loop.
        agent._update_single_turn = True
        return _success_prompt(report)
    display_queue.put({"done": report, "source": "system"})
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
