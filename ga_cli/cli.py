"""
ga_cli/cli.py - GenericAgent 命令行分发系统

通过 python -m ga_cli <命令> 或 ga <命令> 调用
"""
import os, sys, subprocess, argparse, textwrap

# Windows GBK 终端兼容
if sys.platform == "win32" and sys.stdout.encoding and sys.stdout.encoding.lower() in ("gbk", "gb2312"):
    sys.stdout.reconfigure(errors="replace") if hasattr(sys.stdout, "reconfigure") else None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)


def _frontends():
    return os.path.join(PROJECT_DIR, "frontends")

def _reflect():
    return os.path.join(PROJECT_DIR, "reflect")


def launch_frontend(cmd_parts, args=None):
    """启动前端/工具进程"""
    full_cmd = []
    for part in cmd_parts:
        part = part.replace("{PROJECT_DIR}", PROJECT_DIR)
        part = part.replace("{FRONTENDS}", _frontends())
        part = part.replace("{REFLECT}", _reflect())
        full_cmd.append(part)

    # [修复] 用当前 Python 解释器路径替换硬编码 'python'
    if full_cmd and full_cmd[0] == "python":
        full_cmd[0] = sys.executable

    # 插入额外参数
    if args:
        full_cmd.extend(args)

    print(f"🚀 {' '.join(full_cmd)}")
    sys.stdout.flush()
    os.chdir(PROJECT_DIR)
    proc = subprocess.Popen(full_cmd)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        sys.exit(0)


COMMANDS = {
    "gui": {
        "help": "启动桌面GUI (qtapp)",
        "desc": "启动基于 PyQt5 的完整桌面聊天界面（气泡代码高亮、文件拖拽、历史搜索）",
        "cmd": ["python", "{FRONTENDS}/qtapp.py"],
    },
    "configure": {
        "help": "运行初始配置向导 (configure_mykey.py)",
        "desc": "首次安装后配置 API Key、模型参数等基础设置",
        "cmd": ["python", "{PROJECT_DIR}/assets/configure_mykey.py"],
    },
    "hub": {
        "help": "启动 Hub 管理器 (launcher)",
        "desc": "启动 hub 前端管理面板（系统托盘 + 浏览器界面）",
        "cmd": ["python", "{PROJECT_DIR}/hub.pyw"],
    },
    "tui": {
        "help": "启动终端 TUI (tuiapp_v2)",
        "desc": "启动新式终端图形界面（Textual v2），支持多行输入/粘贴/历史浏览",
        "cmd": ["python", "{FRONTENDS}/tuiapp_v2.py"],
    },
    "tui2": {
        "help": "启动终端 TUI v2 (tuiapp_v2)",
        "desc": "启动增强版终端图形界面（Textual v2），更多功能更好的体验",
        "cmd": ["python", "{FRONTENDS}/tuiapp_v2.py"],
    },
    "cli": {
        "help": "启动 CLI 对话 (agentmain)",
        "desc": "启动命令行交互对话模式，最轻量的使用方式",
        "cmd": ["python", "{PROJECT_DIR}/agentmain.py"],
    },
    "janitor": {
        "help": "会话归档维护（默认只读计划；--apply 执行归档）",
        "desc": "审计或归档到期 UUID 会话；不读取或改动 legacy model_responses",
        "cmd": ["python", "-m", "frontends.session_janitor"],
    },
    "launch": {
        "help": "启动 webview 桌面壳 (launch.pyw)",
        "desc": "以原生窗口形式包装 stapp Web 界面（基于 pywebview）",
        "cmd": ["python", "{PROJECT_DIR}/launch.pyw"],
    },
    "status": {
        "help": "检查运行状态",
        "desc": "检查当前是否已有 GenericAgent 进程在运行",
        "cmd": None,
        "internal": True,
    },
    "update": {
        "help": "更新项目 (git pull + pip install)",
        "desc": "从 Git 拉取最新代码并更新依赖",
        "cmd": None,
        "internal": True,
    },
    "list": {
        "help": "列出所有可用前端/服务",
        "desc": "显示所有注册的命令",
        "cmd": None,
        "internal": True,
    },
    "sync": {
        "help": "安全同步更新（stash + pull + pip install）",
        "desc": "先暂存本地修改再拉取，自动恢复。不会因为未提交改动而拒绝更新",
        "cmd": None,
        "internal": True,
    },
}


def cmd_list():
    """展示所有可用命令"""
    print()
    frontend_cmds = [(k, v) for k, v in sorted(COMMANDS.items()) if v["cmd"] is not None]
    internal_cmds = [(k, v) for k, v in sorted(COMMANDS.items()) if v["cmd"] is None]

    print(f"  {'命令':20s}  {'说明'}")
    print(f"  {'━'*20}  {'━'*40}")
    for name, info in frontend_cmds:
        print(f"  {name:20s}  {info.get('help', info['desc'][:40])}")
    print()
    for name, info in internal_cmds:
        print(f"  {name:20s}  {info.get('help', info['desc'][:40])}")
    print()


def cmd_status():
    """检查进程状态"""
    import psutil
    running = [p for p in psutil.process_iter(['pid', 'name', 'cmdline'])
               if p.info['cmdline'] and any('agentmain' in c for c in p.info['cmdline'])]
    if running:
        print(f"🟢 运行中: {len(running)} 个进程")
        for p in running:
            print(f"   PID {p.info['pid']} — {' '.join(p.info['cmdline'][:3])}")
    else:
        print("⚫ GenericAgent 进程未运行")


def cmd_update():
    """git pull + pip install"""
    os.chdir(PROJECT_DIR)
    print("🔄 git pull...")
    r = subprocess.run(["git", "pull"], capture_output=True, text=True)
    print(r.stdout)
    if r.returncode != 0:
        print(r.stderr)
    print("📦 pip install...")
    r2 = subprocess.run([sys.executable, "-m", "pip", "install", "-e", "."],
                        capture_output=True, text=True)
    print(r2.stdout[-500:] if r2.stdout else "")
    if r2.returncode != 0:
        print(r2.stderr[-500:])


def _git(args, **kwargs):
    """Run git in PROJECT_DIR and capture text output by default."""
    kwargs.setdefault("cwd", PROJECT_DIR)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(["git", *args], **kwargs)


def _has_tracked_changes() -> bool:
    """Only tracked/staged changes; do not touch untracked secrets/files."""
    return (
        _git(["diff", "--quiet"]).returncode != 0 or
        _git(["diff", "--cached", "--quiet"]).returncode != 0
    )


def _unmerged_files() -> list[str]:
    sp = _git(["diff", "--name-only", "--diff-filter=U"])
    return [f.strip() for f in sp.stdout.splitlines() if f.strip()]


def _merge_in_progress() -> bool:
    return _git(["rev-parse", "-q", "--verify", "MERGE_HEAD"]).returncode == 0



def _print_manual_conflict_help(backup_branch: str, patch_path: str) -> None:
    """Print conservative recovery hints for conflicts; no automatic semantic merge."""
    print("      git status")
    print("      git diff")
    print("      git stash list")
    print(f"      # 保护点: {backup_branch}")
    print(f"      # tracked diff 备份: {patch_path}")


def _validate_sync_result() -> bool:
    """Validate the final worktree before claiming sync succeeded."""
    unmerged = _unmerged_files()
    if unmerged:
        print("❌ 仍有未解决冲突:")
        for path in unmerged:
            print(f"   {path}")
        return False

    markers = _git(["grep", "-n", "-E", r"^(<<<<<<<|>>>>>>>)", "--",
                    "*.py", "*.json", "*.md"])
    if markers.returncode == 0:
        print("❌ 检测到残留冲突标记:")
        print(markers.stdout)
        return False
    if markers.returncode not in (1,):
        print(f"❌ 检查冲突标记失败: {markers.stderr.strip()}")
        return False

    checked = _git(["diff", "--check"])
    if checked.returncode != 0:
        print("❌ diff 检查失败:")
        print(checked.stdout or checked.stderr)
        return False

    tracked = _git(["ls-files", "*.py"])
    files = [x.strip() for x in tracked.stdout.splitlines() if x.strip()]
    if files:
        import tempfile
        env = os.environ.copy()
        with tempfile.TemporaryDirectory(prefix="ga-sync-pycache-") as cache:
            env["PYTHONPYCACHEPREFIX"] = cache
            compiled = subprocess.run(
                [sys.executable, "-m", "py_compile", *files],
                cwd=PROJECT_DIR, capture_output=True, text=True, env=env,
            )
        if compiled.returncode != 0:
            print("❌ Python 语法检查失败:")
            print(compiled.stderr or compiled.stdout)
            return False
    return True


def cmd_sync():
    """安全同步 origin/main；冲突不自动拼接，验证通过后才安装。"""
    import time

    os.chdir(PROJECT_DIR)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if _merge_in_progress():
        print("❌ 当前已有未完成的 git merge，请先处理或 git merge --abort")
        return 1

    backup_branch = f"backup/ga-sync-{stamp}"
    branch_result = _git(["branch", backup_branch, "HEAD"])
    if branch_result.returncode != 0:
        print("❌ 无法创建保护分支:", branch_result.stderr.strip())
        return 1
    patch_path = os.path.join("/tmp", f"ga-sync-tracked-{stamp}.patch")
    patch = _git(["diff", "HEAD", "--binary"])
    try:
        with open(patch_path, "w", encoding="utf-8") as fp:
            fp.write(patch.stdout)
    except OSError as exc:
        print(f"❌ 无法保存 tracked diff: {exc}")
        return 1
    print(f"🛟 保护点: {backup_branch}")
    print(f"🛟 已保存 tracked diff: {patch_path}")

    has_local = _has_tracked_changes()
    if has_local:
        print("📦 暂存已跟踪本地修改...")
        stashed = _git(["stash", "push", "-m", f"ga sync tracked changes {stamp}"])
        print(stashed.stdout or stashed.stderr)
        if stashed.returncode != 0:
            print("❌ stash 失败，已停止。")
            return 1
    else:
        print("📦 无已跟踪本地修改；未跟踪文件原样保留")

    print("🔄 同步 origin/main...")
    fetched = _git(["fetch", "origin", "main"])
    if fetched.returncode != 0:
        print(fetched.stderr)
        if has_local:
            restored = _git(["stash", "pop"])
            if restored.returncode != 0:
                print("⚠️ 网络更新失败且本地修改恢复产生冲突；stash 保留，请手动处理。")
                _print_manual_conflict_help(backup_branch, patch_path)
        return 1

    merged = _git(["merge", "--no-edit", "origin/main"])
    print(merged.stdout or merged.stderr)
    if merged.returncode != 0:
        if _merge_in_progress():
            print("❌ origin/main 合并冲突；不自动改写代码，正在中止上游合并。")
            aborted = _git(["merge", "--abort"])
            if aborted.returncode != 0:
                print("❌ 无法中止上游合并；未恢复 stash，避免进一步扩大冲突。")
                _print_manual_conflict_help(backup_branch, patch_path)
                return 1
        if has_local:
            restored = _git(["stash", "pop"])
            print(restored.stdout or restored.stderr)
            if restored.returncode != 0:
                print("⚠️ 本地修改恢复产生冲突；stash 保留，请手动处理。")
                _print_manual_conflict_help(backup_branch, patch_path)
                return 1
        print("❌ 上游合并失败；未报告成功。")
        return 1

    if has_local:
        print("📦 恢复本地修改...")
        restored = _git(["stash", "pop"])
        print(restored.stdout or restored.stderr)
        if restored.returncode != 0 or _unmerged_files():
            print("❌ 恢复本地修改产生冲突；不自动拼接，stash 保留。")
            _print_manual_conflict_help(backup_branch, patch_path)
            return 1

    print("🔍 验证合并后的最终代码...")
    if not _validate_sync_result():
        print("❌ 验证失败，未执行 pip install；保护点和未跟踪文件均保留。")
        return 1

    print("📦 pip install...")
    installed = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", "."],
        capture_output=True, text=True, cwd=PROJECT_DIR,
    )
    print(installed.stdout[-1000:] if installed.stdout else "")
    if installed.returncode != 0:
        print(installed.stderr[-2000:] if installed.stderr else "pip install failed")
        return 1
    print("✅ ga sync 完成：已安全合并 origin/main 并通过代码验证")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="ga",
        description="GenericAgent 全局命令入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            示例:
              ga gui               启动桌面 GUI
              ga web               启动 Web 增强版
              ga web --native      启动 Web 基础版(桌面壳)
              ga tui               启动终端 TUI (v1)
              ga tui2              启动终端 TUI (v2 增强版)
              ga pet               启动桌面宠物 v2
              ga launch            启动 webview 桌面壳
              ga janitor           查看会话归档计划（加 --apply 执行）

              ga sync              安全更新（stash+拉取+恢复，不怕本地改动）
              ga list              列出所有命令
        """),
    )
    parser.add_argument("command", nargs="?", help="命令名")
    parser.add_argument("args", nargs="*", help="子命令参数")
    parser.add_argument("-v", "--version", action="store_true", help="显示版本")

    args, unknown = parser.parse_known_args()

    if args.version:
        print("GenericAgent v0.1.0")
        return

    cmd = args.command

    if not cmd or cmd == "help":
        parser.print_help()
        print("\n--- 命令列表 ---")
        cmd_list()
        return

    if cmd == "list":
        cmd_list()
        return

    if cmd == "status":
        cmd_status()
        return

    if cmd == "update":
        cmd_update()
        return

    if cmd == "sync":
        raise SystemExit(cmd_sync())
        return

    if cmd not in COMMANDS:
        print(f"❌ 未知命令: {cmd}")
        print(f"   使用 'ga list' 查看可用命令")
        sys.exit(1)

    info = COMMANDS[cmd]

    # 内置命令走内部逻辑
    if info.get("internal"):
        print(f"❌ 命令 {cmd} 没有配置启动命令")
        sys.exit(1)

    extra = list(args.args) + unknown

    # === 处理命令特有 flags ===
    cmd_parts = list(info["cmd"])

    # 处理 flags (如 --native)
    flags = info.get("flags", {})
    for flag_name, flag_info in flags.items():
        if flag_name in extra:
            cmd_parts = list(flag_info["cmd"])
            extra.remove(flag_name)
            break

    launch_frontend(cmd_parts, extra if extra else None)


if __name__ == "__main__":
    main()
