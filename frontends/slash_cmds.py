"""Slash-command prompt builders + scheduler-task discovery.

Goal of this module: keep TUI files (tuiapp_v2.py / tui_v3.py) thin. They only
need to forward `/update`, `/autorun`, `/morphling`, `/goal`, `/hive`
to the corresponding `build_*_prompt(args)` here, and ask
`list_scheduler_tasks()` / `start_scheduler_task()` for the `/scheduler` picker.

Design (per user 2026-05-27):
- All non-/scheduler commands are *prompt injection*: we craft a system-style
  request and feed it to the main agent as a normal user message (the TUI is
  free to display the raw `/cmd ...` as the visible bubble).  This keeps the
  agent in-session, lets it use every tool/SOP it normally would, and means
  this file owns zero LLM logic.
- `/scheduler` is the only exception — it touches local FS state directly via
  `sche_tasks/*.json` and the existing scheduler daemon, no LLM needed.
- All prompts deliberately *name* the relevant SOP file so the agent re-reads
  it before acting (per CONSTITUTION rule 2: SOP-first).

This module has zero TUI imports — both frontends can depend on it without
either depending on the other.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import subprocess
import time
from pathlib import Path
from typing import Optional


_USER_SHELL: tuple[list[str], str] | None = None

COMMIT_SIGNATURE_PROMPT = 'When you create a git commit, append "Co-Authored-By: GenericAgent <bot@gaagent.ai>" as the final line of the commit message.'

def detect_user_shell() -> tuple[list[str], str]:
    """Return `([executable, ...flags_for_-c], display_name)` for the user's
    interactive shell.  Cached after first call.

    `!cmd` in tui_v2 / tui_v3 invokes this so commands like `ls`, pipes,
    globs, and shell builtins behave the way the user expects in whatever
    shell launched the app, instead of hardcoding cmd.exe / /bin/sh.

    Resolution order:
      1. `$SHELL` if it points to an existing file (Unix, Git Bash, WSL)
      2. Windows only: Git Bash at the canonical install paths
      3. `bash` anywhere on PATH (WSL bash, Cygwin, MSYS2, etc.)
      4. Windows only: `pwsh` then `powershell.exe` on PATH
      5. Unix `/bin/sh` / Windows `%COMSPEC%` (cmd.exe) — last resort
    """
    global _USER_SHELL
    if _USER_SHELL is not None:
        return _USER_SHELL

    s = os.environ.get("SHELL")
    if s and os.path.exists(s):
        name = os.path.basename(s)
        if name.lower().endswith(".exe"):
            name = name[:-4]
        _USER_SHELL = ([s, "-c"], name)
        return _USER_SHELL

    if sys.platform == "win32":
        for p in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ):
            if os.path.exists(p):
                _USER_SHELL = ([p, "-c"], "bash")
                return _USER_SHELL
        bash = shutil.which("bash")
        if bash:
            _USER_SHELL = ([bash, "-c"], "bash")
            return _USER_SHELL
        for name in ("pwsh", "powershell"):
            p = shutil.which(name)
            if p:
                # -NoProfile keeps each `!cmd` snappy + reproducible.
                _USER_SHELL = ([p, "-NoProfile", "-Command"], name)
                return _USER_SHELL
        cmd = os.environ.get("COMSPEC", "cmd.exe")
        _USER_SHELL = ([cmd, "/d", "/s", "/c"], "cmd")
        return _USER_SHELL

    _USER_SHELL = (["/bin/sh", "-c"], "sh")
    return _USER_SHELL



# Repo root = parent of frontends/.  Avoid hard-coding; both TUIs live next to
# this file and share the same anchor.
_ROOT = Path(__file__).resolve().parent.parent

# Language resolution is owned here (not passed in as a formal arg) so every
# prompt builder stays single-parameter and TUI call sites don't need to know
# which prompt happens to be bilingual.  Source of truth, in order:
#   1. `GA_LANG` env var (scriptable override; matches tui_v3 convention)
#   2. tui_v3's persisted settings file (same path as tui_v3.py:_SETTINGS_PATH)
#   3. system locale (zh* → 'zh', else 'en')
# When the user switches language inside tui_v3 (set_lang persists), the next
# call here picks it up automatically -- no formal coupling, just a shared file.
_SETTINGS_PATH = _ROOT / "temp" / "tui_v3_settings.json"


def _current_lang() -> str:
    env = (os.environ.get("GA_LANG") or "").strip().lower()
    if env in ("zh", "en"):
        return env
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            saved = (json.load(f) or {}).get("lang")
        if saved in ("zh", "en"):
            return saved
    except Exception:
        pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var, "")
        if v:
            return "zh" if v.lower().startswith("zh") else "en"
    return "en"


# ----- prompt builders (pure functions, no I/O) ---------------------------
# SOP paths are written inline as literal strings in each builder below: a
# literal is self-documenting and locally readable, and a stale path is a
# zero-radius failure (the prompt is a hint to an intelligent agent, which
# re-reads the dir / asks if a SOP moved) — so we deliberately do NOT wrap it
# in a registry + existence-check machinery.

def _tail(args_text: str, label: str = "额外指示") -> str:
    """Append user-supplied args after a slash command as a free-form suffix.

    User pattern (2026-05-27): the base prompt is a fixed injection that names
    the SOP path; anything the user types after `/cmd ` is appended verbatim so
    they can add per-invocation hints (e.g. `/morphling https://github.com/...`
    or `/goal 调研 X，预算 50k token`).
    """
    extra = (args_text or "").strip()
    return f"\n\n{label}: {extra}" if extra else ""


def build_update_prompt(args_text: str = "") -> str:
    """Build the in-session, LLM-driven two-remote update workflow.

    Conflict resolution intentionally stays in the normal agent loop: a shell
    helper cannot decide how an upstream API change and a fork feature should
    coexist. The prompt pins git to this checkout because agent tools usually
    start in ``temp/``, where root-relative pathspecs otherwise fail.
    """
    root = str(_ROOT)
    if _current_lang() == "en":
        return (
            "Synchronize this GenericAgent checkout transactionally. First read "
            "`memory/git_sync_sop.md`. The official source is `origin/main` "
            "(https://github.com/Lsdefine/GenericAgent); `myfork/main` is the cloud mirror. "
            f"Run every git command at repo root `{root}` (use `git -C \"{root}\" ...`). "
            "Do not delegate to `ga sync`, use bare `git pull`, or blindly choose ours/theirs.\n\n"
            "1. Preflight: require checked-out branch `main`; stop on an unfinished merge, "
            "rebase, or cherry-pick. Inspect status and both remote URLs, then fetch `--prune` "
            "from `origin` and `myfork`. Record old HEAD and remote tips, merge-base, left/right "
            "counts, incoming/local commit titles, patch-equivalent commits, and changed files.\n"
            "2. Recovery: before changing history, create a timestamped backup branch from HEAD, "
            "save staged and unstaged tracked binary patches under `/tmp`, and inventory untracked "
            "files. If dirty, make a clearly named stash including untracked files. Never mix "
            "pre-existing edits into the upstream merge commit; report all recovery references.\n"
            "3. Official update: explicitly integrate `origin/main` into local `main` (fast-forward "
            "when possible, otherwise a merge commit). For every conflict inspect merge-base, ours, "
            "theirs, callers, tests, and relevant history. Resolve semantically so upstream fixes "
            "and every still-valid local feature coexist. Never bulk-checkout one side, concatenate "
            "conflict bodies, use `git add -A`, or delete a capability merely to pass the merge. "
            "Stage only reviewed paths.\n"
            "4. Restore pre-existing dirty edits after official integration, resolving restoration "
            "conflicts with the same three-way review. Keep the stash until verified and do not "
            "commit those edits unless explicitly asked. If semantics remain uncertain, stop in a "
            "recoverable state and ask one minimal question rather than guessing.\n"
            "5. Validate before push: unmerged entries must be empty; `origin/main` must be an "
            "ancestor of HEAD and its behind count zero. Review the full upstream/local feature diff; "
            "run touched-area tests, the repository suite, tracked-Python `py_compile`, applicable "
            "script syntax checks, and an affected command/frontend smoke test. Fix regressions, not tests.\n"
            "6. Cloud sync: inspect commits and patches unique to `myfork/main`, distinguishing real "
            "fork-only work from patch-equivalent duplicate history, and preserve any real work not "
            "represented locally. Then mirror local `main` to `myfork/main`. Push normally if fast-forward. "
            "If exact alignment is non-fast-forward, do not merge obsolete fork history into main and "
            "never use plain `--force`: show what would be displaced, create and push a timestamped fork "
            "backup ref when feasible, then ask for approval before using "
            "`--force-with-lease=<expected-tip>`. Never push official `origin`. Fetch myfork again and "
            "prove local `main` and `myfork/main` tips are identical.\n"
            "7. Report an update ledger: old/new HEAD and remote tips; incoming upstream commits and "
            "user-visible changes; retained local features; conflict decisions by file; dirty-edit "
            "recovery; tests; backups/patches; push result; final ahead/behind counts; residual risk. "
            "A process merely starting is not sufficient verification."
            f"{_tail(args_text, 'Extra instructions')}"
        )
    return (
        "请以事务式流程同步当前 GenericAgent 仓库。先读 `memory/git_sync_sop.md`。"
        "官方源是 `origin/main`（https://github.com/Lsdefine/GenericAgent），"
        "`myfork/main` 是云端镜像。所有 git 命令必须在仓库根目录 "
        f"`{root}` 执行（使用 `git -C \"{root}\" ...`）。禁止委托给 `ga sync`、"
        "裸 `git pull` 或盲选 ours/theirs。\n\n"
        "1. 前检：当前分支必须是 `main`；若有未完成的 merge/rebase/cherry-pick 就停止。"
        "检查工作区和两个 remote URL，再分别 `fetch --prune origin` 与 `fetch --prune myfork`。"
        "记录旧 HEAD、两端 tip、merge-base、左右提交数、上游/本地提交标题、patch 等价关系及变更文件。\n"
        "2. 可恢复保护：改历史前从 HEAD 创建带时间戳的 backup 分支，把 tracked 的 staged 与 "
        "unstaged binary patch 保存到 `/tmp`，并盘点 untracked 文件。若工作区不干净，用名称明确且"
        "包含 untracked 的 stash 保存；不得把原有编辑混入上游 merge commit。汇报全部恢复引用。\n"
        "3. 官方更新：把 `origin/main` 显式整合进本地 `main`（可快进则快进，否则创建 merge commit）。"
        "每个冲突都必须查看 merge-base、ours、theirs、附近调用/测试和相关历史，做语义合并："
        "上游新增行为与修复必须保留，仍有效的本地功能也必须完整保留并适配新接口。禁止整批 checkout "
        "某一侧、拼接冲突块、`git add -A` 或为通过合并删除能力；只 stage 已审文件。\n"
        "4. 官方整合后恢复原有脏编辑；恢复冲突同样三方审查，验证前保留 stash。除非用户明确要求，"
        "不得提交更新前已有的编辑。无法确信语义正确时保持仓库可恢复，只问一个最小问题，禁止猜。\n"
        "5. 推送前验证：unmerged 为空，`origin/main` 是 HEAD 祖先且相对 origin behind=0；"
        "复审完整上游差异与本地功能差异；运行触及区域定向测试、仓库测试套件、tracked Python "
        "`py_compile`、适用脚本语法检查及受影响命令/前端 smoke test。修实现而非削弱测试。\n"
        "6. 云同步：检查 `myfork/main` 独有提交与 patch，区分真实 fork 工作和 patch 等价的重复历史，"
        "先保全尚未在本地表达的真实工作，再把本地 `main` 镜像到 `myfork/main`。可快进则正常 push。"
        "若精确对齐必须非快进，禁止把陈旧 fork 历史反向合进 main，也禁止裸 `--force`：先展示会被"
        "替换的内容，条件允许时创建并推送带时间戳的 fork 备份 ref，再向用户请示；获批后才可用绑定"
        "预期 tip 的 `--force-with-lease=<expected-tip>`。绝不 push 官方 `origin`。完成后重新 fetch "
        "myfork，并证明本地 main 与 `myfork/main` tip 完全一致。\n"
        "7. 最终更新账单：旧/新 HEAD 与两端 tip；本次上游提交及用户可感知变化；保留的本地功能；"
        "冲突和逐文件裁决；脏编辑恢复；测试；backup/patch；推送结果；最终 ahead/behind；"
        "以及未经验证的残余风险。仅启动成功不算验证。"
        f"{_tail(args_text)}"
    )

def build_autorun_prompt(args_text: str = "") -> str:
    return (
        "请进入「自主探索 / autonomous 模式」：先读 "
        "memory/autonomous_operation_sop.md。"
        "全程自驱，不可逆 / 高风险动作先 ask_user ，"
        "结案给一份简明回执（做了什么 / 产物在哪 / 下一步）。"
        f"{_tail(args_text, '任务种子')}"
    )


def build_morphling_prompt(args_text: str = "") -> str:
    return (
        "请启用 Morphling 模式吞噬 / 蒸馏外部项目到本仓库：先读 "
        "memory/morphling_sop.md。"
        "没有目标先 ask_user 取 GitHub 仓库 / 本地路径 / 能力描述。"
        f"{_tail(args_text, '目标技能/仓库')}"
    )


def build_goal_prompt(args_text: str = "") -> str:
    return (
        "请进入 Goal 模式：先读 memory/goal_mode_sop.md。"
        "若未给目标，先 ask_user 一次性问清：一句话目标 + condition 约束。"
        f"{_tail(args_text, '用户目标')}"
    )


def build_hive_prompt(args_text: str = "") -> str:
    return (
        "请进入 Goal Hive 模式（多 worker 协作版 goal）：先读 "
        "memory/goal_hive_sop.md。"
        "集群目标 / worker 配额 / 终止条件未明确时先 ask_user 补齐再启动。"
        f"{_tail(args_text, '集群目标')}"
    )


def build_conductor_prompt(args_text: str = "") -> str:
    """`/conductor <task>` → run `frontends/conductor.py` on the task.

    Upstream `memory/` ships no conductor SOP, so we deliberately keep the
    prompt short: name the entrypoint and forward the task verbatim.  The
    agent is expected to know how to drive `conductor.py` (or consult a
    local SOP if one happens to be installed).
    """
    args_text = (args_text or "").strip()
    if args_text:
        return f"请调用 frontends/conductor.py 执行：{args_text}"
    return (
        "请调用 frontends/conductor.py，根据后续指令完成多 subagent 编排。"
        "若任务描述缺失，先 ask_user 一次性补齐。"
    )


# ----- /scheduler reflect-task discovery + launch -------------------------

def list_reflect_tasks() -> list[dict]:
    """Return [{name, path, doc}] for every reflect/*.py task script.

    `doc` is the module docstring's first line (best-effort) so the picker can
    show a one-liner next to each name.  Empty list if reflect/ doesn't exist.
    """
    out: list[dict] = []
    refl = _ROOT / "reflect"
    if not refl.is_dir():
        return out
    for p in sorted(refl.glob("*.py")):
        if p.name.startswith("_"):
            continue
        doc = ""
        try:
            # Cheap docstring sniff: read first ~40 lines, look for """...""".
            head = p.read_text(encoding="utf-8", errors="ignore").splitlines()[:40]
            joined = "\n".join(head)
            for q in ('"""', "'''"):
                i = joined.find(q)
                if i != -1:
                    j = joined.find(q, i + 3)
                    if j != -1:
                        doc = joined[i + 3:j].strip().splitlines()[0].strip()
                        break
        except Exception:
            pass
        out.append({"name": p.stem, "path": str(p), "doc": doc})
    return out


# ----- hub.pyw parity: every launchable service ---------------------------

_HUB_EXCLUDES = {"goal_mode.py", "chatapp_common.py", "tuiapp.py"}


def _sniff_doc(p) -> str:
    """Best-effort first line of a module docstring (cheap ~40-line read)."""
    try:
        head = p.read_text(encoding="utf-8", errors="ignore").splitlines()[:40]
        joined = "\n".join(head)
        for q in ('"""', "'''"):
            i = joined.find(q)
            if i != -1:
                j = joined.find(q, i + 3)
                if j != -1:
                    body = joined[i + 3:j].strip()
                    if body:
                        return body.splitlines()[0].strip()
    except Exception:
        pass
    return ""


def list_launchable_services() -> list[dict]:
    """Mirror hub.pyw's discover_services() so `/scheduler` shows the *same*
    set of launchable services as the GUI launcher.

    Sources (hub.pyw EXCLUDES = goal_mode.py / chatapp_common.py / tuiapp.py):
      • reflect/*.py   (not '_'-prefixed, not excluded)
          → cmd = [python, agentmain.py, --reflect, reflect/<f>]
      • frontends/*app*.py (not excluded)
          → 'stapp' → `python -m streamlit run … --server.headless=true`
            others   → `python frontends/<f>`

    Returns [{name, cmd, doc, kind}] where `name` is the hub-style path
    ('reflect/foo.py' / 'frontends/bar.py') and doubles as the picker value.
    """
    out: list[dict] = []
    refl = _ROOT / "reflect"
    if refl.is_dir():
        for p in sorted(refl.glob("*.py")):
            if p.name.startswith("_") or p.name in _HUB_EXCLUDES:
                continue
            rel = "reflect/" + p.name
            out.append({
                "name": rel,
                "cmd": [sys.executable, "agentmain.py", "--reflect", rel],
                "doc": _sniff_doc(p),
                "kind": "reflect",
            })
    fe = _ROOT / "frontends"
    if fe.is_dir():
        for p in sorted(fe.glob("*.py")):
            if "app" not in p.name or p.name in _HUB_EXCLUDES:
                continue
            rel = "frontends/" + p.name
            if "stapp" in p.name:
                cmd = [sys.executable, "-m", "streamlit", "run", rel,
                       "--server.headless=true"]
            else:
                cmd = [sys.executable, rel]
            out.append({"name": rel, "cmd": cmd, "doc": _sniff_doc(p),
                        "kind": "frontend"})
    return out


def start_service(name: str) -> tuple[bool, str]:
    """Launch a service from list_launchable_services(), detached & window-less
    (CONSTITUTION rule 14: creationflags at the launch layer only, never via
    subprocess.Popen monkeypatch).

    `name` accepts the hub-style path ('reflect/foo.py') or a bare reflect stem
    ('foo') for backward-compat with `/scheduler start <stem>`.
    """
    svcs = list_launchable_services()
    svc = next((s for s in svcs if s["name"] == name), None)
    if svc is None:  # bare reflect stem fallback
        cand = "reflect/" + name + ".py"
        svc = next((s for s in svcs if s["name"] == cand), None)
    if svc is None:
        return False, f"未知服务: {name}"
    try:
        flags = 0
        if os.name == "nt":
            flags = 0x00000200 | 0x08000000  # NEW_PROCESS_GROUP | NO_WINDOW
        proc = subprocess.Popen(
            svc["cmd"],
            cwd=str(_ROOT),
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        # Poll-and-confirm: if the child dies immediately (bad path, import
        # error, port-in-use, etc) Popen still returns happily — without this
        # check the picker would tick "✅ started" while nothing is running,
        # which is exactly the bug#4 the user hit.  0.4s is the smallest
        # window that catches "explodes at import" without making the UI
        # feel laggy on healthy starts.
        time.sleep(0.4)
        rc = proc.poll()
        if rc is not None:
            return False, f"启动失败 (退出码 {rc}): {svc['name']}"
        invalidate_running_cache()
        return True, f"已启动 {svc['name']} (pid={proc.pid})"
    except Exception as e:
        return False, f"启动失败: {type(e).__name__}: {e}"


# ----- running-state introspection (bug#4) --------------------------------
# Why psutil cmdline-scan instead of a launched-by-us pid registry?
#   • Services launched by a previous TUI run, or by hub.pyw, must also be
#     recognised — otherwise /scheduler would happily start a duplicate.
#   • A registry tied to this process dies when the TUI restarts, but the
#     services keep running (CREATE_NEW_PROCESS_GROUP).  Cmdline scan is the
#     only single source of truth across launchers, surviving restarts.
# Trade-off: it costs ~30ms per /scheduler open, and matches by cmdline tail,
# so two checkouts of GA can collide.  We accept that — running two GAs out
# of two clones is already an unsupported configuration.

def _match_service(cmdline: list[str], svc: dict) -> bool:
    """Does this OS process belong to `svc`?  Match on the trailing script
    arg (`reflect/foo.py` for reflect tasks, `frontends/bar.py` for apps),
    which is invariant across `python` vs `pythonw` vs venv shims.

    Reflect detection used to require BOTH `agentmain.py` AND the reflect
    path in cmdline.  That rejected tasks launched directly (`python
    reflect/scheduler.py`) by launch.pyw, dev scripts, or by an earlier
    TUI run that used a different launcher — they showed unticked in
    /scheduler even when alive.  Path-only match handles both styles; the
    Python-process pre-filter in `running_services` keeps false positives
    (greps, editors with the file open) from sneaking in."""
    if not cmdline:
        return False
    rel = svc["name"]  # 'reflect/foo.py' | 'frontends/bar.py'
    rel_norm = rel.replace("/", os.sep)
    return any(rel_norm in (a or "") or rel in (a or "")
               for a in cmdline)


# 2s TTL cache + name-prefilter: ~2.1s → ~1.0s cold, ~0ms warm.
# cmdline() is the per-proc cost; only pay it for python-ish survivors.
_RUNNING_CACHE: tuple[float, dict[str, int]] | None = None
_RUNNING_TTL = 2.0


def invalidate_running_cache() -> None:
    """Drop the snapshot. Call after start/stop so the next read is fresh."""
    global _RUNNING_CACHE
    _RUNNING_CACHE = None


def running_services(use_cache: bool = True) -> dict[str, int]:
    """{service_name: pid} for live services. {} if psutil missing."""
    global _RUNNING_CACHE
    if use_cache and _RUNNING_CACHE and time.time() - _RUNNING_CACHE[0] < _RUNNING_TTL:
        return dict(_RUNNING_CACHE[1])
    try:
        import psutil  # type: ignore
    except Exception:
        return {}
    svcs = list_launchable_services()
    out: dict[str, int] = {}
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["pid"] == me:
                continue
            nm = (proc.info.get("name") or "").lower()
            if "python" not in nm and "py.exe" not in nm:
                continue
            cmd = proc.cmdline()
        except Exception:
            continue
        for svc in svcs:
            if svc["name"] not in out and _match_service(cmd, svc):
                out[svc["name"]] = proc.info["pid"]
                break
    _RUNNING_CACHE = (time.time(), dict(out))
    return out


def stop_service(name: str) -> tuple[bool, str]:
    """Terminate the service `name` if running.  Returns (ok, message).

    Sends SIGTERM-equivalent (Popen.terminate on Windows = TerminateProcess),
    waits up to 3s, then escalates to kill.  Also reaps obvious children
    (e.g. `python -m streamlit` spawns the actual streamlit worker) so we
    don't leave orphans behind.
    """
    try:
        import psutil  # type: ignore
    except Exception:
        return False, "未安装 psutil，无法停止服务"
    running = running_services()
    pid = running.get(name)
    if pid is None:
        return False, f"{name} 未在运行"
    try:
        parent = psutil.Process(pid)
        kids = parent.children(recursive=True)
        for p in [parent, *kids]:
            try:
                p.terminate()
            except Exception:
                pass
        gone, alive = psutil.wait_procs([parent, *kids], timeout=3.0)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
        invalidate_running_cache()
        return True, f"已停止 {name} (pid={pid})"
    except psutil.NoSuchProcess:
        invalidate_running_cache()
        return True, f"{name} 已退出"
    except Exception as e:
        return False, f"停止失败: {type(e).__name__}: {e}"


def list_scheduler_tasks() -> list[dict]:
    """Return [{name, path, schedule, enabled}] for every sche_tasks/*.json.

    Used by the /scheduler picker so users can also toggle traditional cron
    tasks, not just reflect.* scripts.
    """
    out: list[dict] = []
    sd = _ROOT / "sche_tasks"
    if not sd.is_dir():
        return out
    for p in sorted(sd.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        out.append({
            "name": p.stem,
            "path": str(p),
            "schedule": data.get("schedule") or data.get("cron") or data.get("every") or "",
            "enabled": bool(data.get("enabled", True)),
        })
    return out


def start_reflect_task(name: str) -> tuple[bool, str]:
    """Spawn `python reflect/<name>.py` detached.  Returns (ok, message).

    Detached because reflect tasks are long-running; we don't want them to die
    with the TUI.  On Windows we use CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW
    so no console pops up (per CONSTITUTION rule 14: only at launch layer, no
    monkeypatching subprocess.Popen).
    """
    script = _ROOT / "reflect" / f"{name}.py"
    if not script.is_file():
        return False, f"reflect/{name}.py 不存在"
    try:
        flags = 0
        if os.name == "nt":
            flags = 0x00000200 | 0x08000000  # NEW_PROCESS_GROUP | NO_WINDOW
        subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(_ROOT),
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return True, f"已启动 reflect/{name}.py"
    except Exception as e:
        return False, f"启动失败: {type(e).__name__}: {e}"


# ----- dispatch table for the TUI to register against ---------------------

# (cmd, arg_hint, desc)  — kept identical between v2 and v3 so the palette
# stays consistent across frontends.
PALETTE_ENTRIES: list[tuple[str, str, str]] = [
    ("/update",    "[note]",    "LLM 合并官方更新、同步 myfork 并生成更新账单"),
    ("/autorun",   "[seed]",    "进入 autonomous_operation 自主模式"),
    ("/morphling", "[target]",  "启用 Morphling 蒸馏 / 吞噬外部技能"),
    ("/goal",      "[goal]",    "进入 Goal 模式（需 condition 约束）"),
    ("/hive",      "[target]",  "进入 Hive 多 worker 协作模式"),
    ("/conductor", "[task]",    "调用 frontends/conductor.py 多 subagent 编排"),
    ("/scheduler", "",          "多选启动/停止 reflect 任务（cron 由 reflect/scheduler.py 驱动）"),
    ("/resume",    "",           "列出最近会话并恢复其中一个（GA 端展开 prompt）"),
]


def prompt_for(cmd: str, args_text: str) -> Optional[str]:
    """Return the injected user-message for a given slash command, or None if
    the command isn't one of ours (e.g. /scheduler — handled by TUI directly).

    Language is resolved inside the builders that care about it (see
    `_current_lang()`); callers never thread it through, so both TUIs keep a
    single uniform call site.
    """
    table = {
        "/update":    build_update_prompt,
        "/autorun":   build_autorun_prompt,
        "/morphling": build_morphling_prompt,
        "/goal":      build_goal_prompt,
        "/hive":      build_hive_prompt,
        "/conductor": build_conductor_prompt,
    }
    fn = table.get(cmd)
    return fn(args_text) if fn else None
