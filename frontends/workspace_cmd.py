"""Linux workspace symlinks shared by TUI v2/v3; never delete real targets."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from typing import Optional


# --------------------------------------------------------------------------- #
# 路径基准(与 plugins/project_mode.py 的 _TEMP 保持一致)
# --------------------------------------------------------------------------- #
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _temp_root() -> str:
    return os.path.join(_REPO_ROOT, "temp")


def _projects_root() -> str:
    return os.path.join(_temp_root(), "projects")


def _anchor_path() -> str:
    """激活锚,pid 键控,与插件 `_ANCHOR` 同。"""
    return os.path.join(_temp_root(), f".active_project.{os.getpid()}")


def _registry_path() -> str:
    return os.path.join(_temp_root(), "workspaces.json")


_REGISTRY_VERSION = 1


# --------------------------------------------------------------------------- #
# 命名
# --------------------------------------------------------------------------- #
def _norm_abspath(p: str) -> str:
    """规范化绝对路径用于 hash:abspath + normcase；不走 realpath，避免解析 symlink 带来的意外。"""
    return os.path.normcase(os.path.abspath(p))


def _ws_name(abs_path: str) -> str:
    base = os.path.basename(abs_path.rstrip("/\\")) or "ws"
    digest = hashlib.blake2b(_norm_abspath(abs_path).encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def _link_path(name: str) -> str:
    return os.path.join(_projects_root(), name)


# --------------------------------------------------------------------------- #
# junction / symlink 跨平台封装(reparse 安全)
# --------------------------------------------------------------------------- #
def make_dir_link(target_abs: str, link_path: str) -> bool:
    """Create a Linux directory symlink without touching the target."""
    try:
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
        os.symlink(os.path.abspath(target_abs), link_path, target_is_directory=True)
        return True
    except OSError as e:
        sys.stderr.write(f"[workspace] symlink failed: {e}\n")
        return False


def is_dir_link(path: str) -> bool:
    return os.path.islink(path)


def link_target(path: str) -> Optional[str]:
    try:
        return os.readlink(path)
    except OSError:
        return None


def remove_dir_link(path: str) -> bool:
    """Unlink only symlinks; never remove a real directory or its contents."""
    if not is_dir_link(path):
        return False
    try:
        os.unlink(path)
        return True
    except OSError as e:
        sys.stderr.write(f"[workspace] remove link {path} failed: {e}\n")
        return False


# --------------------------------------------------------------------------- #
# 注册表 temp/workspaces.json(本功能私有;v2/v3 可能并发 -> 原子写)
# --------------------------------------------------------------------------- #
def registry_load() -> dict:
    try:
        with open(_registry_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("version") == _REGISTRY_VERSION:
            items = data.get("items")
            if isinstance(items, dict):
                return items
    except (OSError, ValueError):
        pass
    return {}


def _registry_save(items: dict) -> None:
    path = _registry_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": _REGISTRY_VERSION, "items": items},
                      fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as e:
        sys.stderr.write(f"[workspace] registry save failed: {e}\n")


# --------------------------------------------------------------------------- #
# 会话→工作区映射 temp/session_workspaces.json — 让 /continue 即时恢复，不必先
# 聊一轮在日志留 PROJECT MODE 块。key=会话日志名, value=workspace 真实路径,
# ""=已 off（区别于缺 key=无记录→回退扫日志）。手动操作触发、极低频，照搬注册
# 表原子写、不加锁。
# --------------------------------------------------------------------------- #
def _session_map_path() -> str:
    return os.path.join(_temp_root(), "session_workspaces.json")


def _session_map_load() -> dict:
    try:
        with open(_session_map_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("version") == _REGISTRY_VERSION:
            items = data.get("items")
            if isinstance(items, dict):
                return items
    except (OSError, ValueError):
        pass
    return {}


def _session_map_save(items: dict) -> None:
    path = _session_map_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": _REGISTRY_VERSION, "items": items},
                      fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as e:
        sys.stderr.write(f"[workspace] session map save failed: {e}\n")


def session_ws_set(log_path: str, target: str) -> None:
    """记录会话绑定的 workspace 路径；target="" 表示该会话已 off。"""
    key = os.path.basename(log_path or "")
    if not key:
        return
    items = _session_map_load()
    items[key] = target or ""
    _session_map_save(items)


def session_ws_get(log_path: str):
    """路径=绑定 / ""=已 off / None=无记录（调用方回退扫日志）。"""
    key = os.path.basename(log_path or "")
    return _session_map_load().get(key) if key else None


def session_map_prune() -> None:
    """删掉日志文件已不存在的孤儿条目（启动时调一次）。"""
    items = _session_map_load()
    logdir = os.path.join(_temp_root(), "model_responses")
    alive = {k: v for k, v in items.items() if os.path.isfile(os.path.join(logdir, k))}
    if len(alive) != len(items):
        _session_map_save(alive)


def registry_upsert(name: str, abs_path: str) -> None:
    items = registry_load()
    items[name] = {"path": os.path.abspath(abs_path), "last_used": int(time.time())}
    _registry_save(items)


def registry_remove(name: str) -> None:
    items = registry_load()
    if items.pop(name, None) is not None:
        _registry_save(items)


def _mem_lines(link: str) -> int:
    """project_memory.md 行数(经 junction 读真实文件);读不到返回 0。"""
    mp = os.path.join(link, "project_memory.md")
    try:
        with open(mp, encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def registry_list() -> list[dict]:
    """供 picker 候选列表:[{name, path, last_used, mem_lines, dangling}],按最近使用倒序。"""
    out = []
    for name, ent in registry_load().items():
        path = (ent or {}).get("path") or ""
        out.append({
            "name": name,
            "path": path,
            "last_used": int((ent or {}).get("last_used") or 0),
            "mem_lines": _mem_lines(_link_path(name)) if path else 0,
            "dangling": not (path and os.path.isdir(path)),
        })
    out.sort(key=lambda x: x["last_used"], reverse=True)
    return out


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def validate_path(abs_path: str) -> tuple[bool, str]:
    if not abs_path or not abs_path.strip():
        return False, "路径为空"
    p = abs_path.strip().strip('"').strip("'")
    if not os.path.isabs(p):
        return False, "需要绝对路径"
    if not os.path.exists(p):
        return False, f"路径不存在: {p}"
    if not os.path.isdir(p):
        return False, "不是目录"
    if _norm_abspath(p).startswith(_norm_abspath(_temp_root())):
        return False, "该路径已在 temp 内,无需 workspace"
    return True, ""


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def prepare(abs_path: str) -> dict:
    """准备 workspace,但不写进程级激活锚。返回:
       {ok, name, link, target, mem_text, warning, error}。
    流程:校验 -> name -> 幂等建链 -> 确保 project_memory.md 存在 -> 注册 -> 回读记忆。
    TUI 多会话隔离使用本函数,避免多个 session 争用同一个 pid 锚。"""
    p = abs_path.strip().strip('"').strip("'") if abs_path else ""
    ok, msg = validate_path(p)
    if not ok:
        return {"ok": False, "error": msg}
    target = os.path.abspath(p)
    name = _ws_name(target)
    link = _link_path(name)
    warning = ""

    # 幂等建链
    if os.path.lexists(link):
        if is_dir_link(link):
            cur = link_target(link)
            if cur and _norm_abspath(cur) == _norm_abspath(target):
                pass  # 已指向同一目标 -> 复用
            else:
                remove_dir_link(link)
                if not make_dir_link(target, link):
                    return {"ok": False, "error": "重建 junction 失败(见 stderr)"}
        else:
            # 极罕见:同名真实目录占位(其它 UI 的普通项目)。绝不覆盖。
            return {"ok": False,
                    "error": f"{link} 已是真实目录(可能是其它项目),拒绝覆盖"}
    else:
        if not make_dir_link(target, link):
            return {"ok": False, "error": "创建 junction 失败(见 stderr)"}

    # 确保 project_memory.md 存在(经 junction 落到真实仓库根)
    mem_path = os.path.join(link, "project_memory.md")
    if not os.path.isfile(mem_path):
        try:
            open(mem_path, "a", encoding="utf-8").close()
        except OSError as e:
            warning = f"无法创建 project_memory.md: {e}"

    mem_text = ""
    try:
        with open(mem_path, encoding="utf-8", errors="replace") as fh:
            mem_text = fh.read()
    except OSError:
        pass

    registry_upsert(name, target)

    return {"ok": True, "name": name, "link": link, "target": target,
            "mem_text": mem_text, "warning": warning, "error": ""}


def activate(abs_path: str) -> dict:
    """设定并激活进程级 workspace。保留给旧 SOP / 非多会话 UI 使用。"""
    r = prepare(abs_path)
    if not r.get("ok"):
        return r
    try:
        with open(_anchor_path(), "w", encoding="utf-8") as fh:
            fh.write(r["name"])
    except OSError as e:
        r = dict(r)
        r.update({"ok": False, "error": f"写激活锚失败: {e}"})
    return r


def deactivate() -> bool:
    """仅删激活锚;junction 与文件保留。返回是否原本处于激活态。"""
    anchor = _anchor_path()
    if os.path.isfile(anchor):
        try:
            os.remove(anchor)
            return True
        except OSError as e:
            sys.stderr.write(f"[workspace] deactivate failed: {e}\n")
    return False


def current() -> Optional[dict]:
    """当前激活的 workspace:{name, path};未激活返回 None。"""
    anchor = _anchor_path()
    try:
        name = open(anchor, encoding="utf-8").read().strip()
    except OSError:
        return None
    if not name:
        return None
    ent = registry_load().get(name) or {}
    return {"name": name, "path": ent.get("path") or ""}


def is_dangling(name: str) -> bool:
    """junction 指向的真实目标是否已失效(被删/盘断开)。"""
    link = _link_path(name)
    if not is_dir_link(link):
        return True
    t = link_target(link)
    return not (t and os.path.isdir(t))


def remove(name: str) -> None:
    """显式注销:删 junction(不动真实文件)+ 删注册表条目。若正激活该项目则一并删锚。"""
    link = _link_path(name)
    if is_dir_link(link):
        remove_dir_link(link)
    registry_remove(name)
    cur = current()
    if cur and cur["name"] == name:
        deactivate()


# project_mode 插件注入的标记:`[PROJECT MODE: <name>]`(见 _build_injection)。
# 它随用户消息写进 model_responses 日志,故可据此判断被 /continue 的会话当时
# 在哪个 workspace。
_PM_RE = re.compile(r"\[PROJECT MODE:\s*([^\]\n]+?)\s*\]")


def workspace_from_log(log_path: str) -> Optional[dict]:
    """扫一份 model_responses 日志,返回它最后激活的 workspace {name, path};
    仅当该 name 是**已注册的 workspace**(在 workspaces.json 里)才返回——
    普通 SOP 项目(无 hash、不在注册表)一律忽略。"""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None
    names = _PM_RE.findall(content)
    if not names:
        return None
    name = names[-1].strip()        # 最后一次激活胜出
    ent = registry_load().get(name)
    if not ent or not ent.get("path"):
        return None
    return {"name": name, "path": ent["path"]}


def cleanup() -> None:
    """v2/v3 启动时调一次:清理 temp/projects/ 下**悬空或未注册**的 junction。
    安全纪律:只处理 is_dir_link 确认的链接;真实目录(其它 UI 的普通项目)一律跳过。"""
    proot = _projects_root()
    if not os.path.isdir(proot):
        return
    registered = set(registry_load().keys())
    try:
        entries = os.listdir(proot)
    except OSError:
        return
    for nm in entries:
        link = os.path.join(proot, nm)
        if not is_dir_link(link):
            continue  # 真实目录 -> 不碰
        t = link_target(link)
        dangling = not (t and os.path.isdir(t))
        if dangling or nm not in registered:
            remove_dir_link(link)
