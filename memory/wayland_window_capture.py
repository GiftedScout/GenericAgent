"""静默获取 GNOME Wayland 原生窗口帧。

只使用 xdg-desktop-portal ScreenCast 的 Window source，不请求远程控制，
也不回退到全屏 Screenshot。首次授权由用户在桌面对话框完成；之后使用
restore_token 持久复用。令牌文件只保存本地授权标识，权限限制为 0600。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from PIL import Image

_TOKEN_DIR = Path(os.environ.get("GA_WAYLAND_TOKEN_DIR", "~/.cache/genericagent")).expanduser()
_TOKEN_FILE = Path(os.environ["GA_WAYLAND_TOKEN_FILE"]).expanduser() if os.environ.get("GA_WAYLAND_TOKEN_FILE") else _TOKEN_DIR / "wayland-window.json"


def _window_exists(title: str) -> bool:
    """Use AT-SPI to confirm a visible native window exists before capture."""
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
        needle = title.casefold()
        desktop = Atspi.get_desktop(0)
        for app in desktop:
            try:
                for child in app:
                    if (child.get_role_name() == "frame"
                            and needle in (child.get_name() or "").casefold()):
                        return True
            except Exception:
                continue
    except Exception as exc:
        raise RuntimeError("Wayland窗口核验需要可用的 AT-SPI 会话") from exc
    return False


def _load_token(title: str) -> str:
    if not _TOKEN_FILE.exists():
        raise PermissionError(
            f"未找到 {title!r} 的持久窗口授权；请先由用户授权一次单窗口共享"
        )
    try:
        data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        token, saved_title = data["restore_token"], data["title"]
    except (OSError, ValueError, KeyError):
        # Compatibility with the temporary probe's plain UUID token.
        token = _TOKEN_FILE.read_text(encoding="ascii").strip()
        saved_title = title
    if not token or saved_title.casefold() != title.casefold():
        raise PermissionError(f"持久授权不是目标窗口 {title!r}")
    return token


def save_restore_token(title: str, token: str, path: Path | None = None) -> Path:
    """Persist a portal restore token after an explicit user authorization."""
    path = path or _TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"title": title, "restore_token": str(token)}, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _request(bus, portal, method, session=None, restore_token=None):
    import dbus
    import dbus.mainloop.glib
    from gi.repository import GLib

    unique = bus.get_unique_name()[1:].replace(".", "_")
    handle_token = "ga" + os.urandom(8).hex()
    expected = f"/org/freedesktop/portal/desktop/request/{unique}/{handle_token}"
    result, loop = {}, GLib.MainLoop()

    def response(code, values):
        result["code"], result["values"] = int(code), values
        loop.quit()

    receiver = bus.add_signal_receiver(response, signal_name="Response",
        dbus_interface="org.freedesktop.portal.Request", path=expected)
    timer = GLib.timeout_add_seconds(30, lambda: (loop.quit(), False)[1])
    try:
        options = {"handle_token": dbus.String(handle_token)}
        if method == "CreateSession":
            options["session_handle_token"] = dbus.String("sess" + os.urandom(8).hex())
            path = portal.CreateSession(options)
        elif method == "SelectSources":
            options.update(types=dbus.UInt32(2), multiple=dbus.Boolean(False),
                           cursor_mode=dbus.UInt32(1), persist_mode=dbus.UInt32(2))
            options["restore_token"] = dbus.String(restore_token)
            path = portal.SelectSources(session, options)
        else:
            path = portal.Start(session, "", options)
        if str(path) != expected:
            raise RuntimeError(f"Portal request path mismatch: {path}")
        loop.run()
    finally:
        receiver.remove()
        try:
            if GLib.main_context_default().find_source_by_id(timer):
                GLib.source_remove(timer)
        except Exception:
            pass
    if result.get("code") != 0:
        raise PermissionError(f"窗口共享请求被拒绝或超时 (response={result.get('code')})")
    return result["values"]


def capture_window(title: str, *, token_file: Path | None = None, timeout: float = 15) -> Image.Image:
    """Return one RGB frame for a named native window, without a full-screen flash.

    This intentionally requires a previously persisted token. It never opens a
    new selection dialog implicitly, which keeps OCR/computer-use polling silent.
    """
    global _TOKEN_FILE
    old_token_file = _TOKEN_FILE
    if token_file is not None:
        _TOKEN_FILE = Path(token_file).expanduser()
    try:
        if not _window_exists(title):
            raise LookupError(f"未找到原生窗口: {title}")
        token = _load_token(title)
        import dbus
        import dbus.mainloop.glib
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        desktop = bus.get_object("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop")
        portal = dbus.Interface(desktop, "org.freedesktop.portal.ScreenCast")
        session = None
        pipeline = None
        fd = None
        try:
            values = _request(bus, portal, "CreateSession")
            session = str(values["session_handle"])
            _request(bus, portal, "SelectSources", session=session, restore_token=token)
            values = _request(bus, portal, "Start", session=session)
            streams = values.get("streams") or []
            if not streams:
                raise RuntimeError("Portal未返回窗口流")
            node = int(streams[0][0])
            rawfd = portal.OpenPipeWireRemote(session, {}, timeout=15)
            fd = rawfd.take()
            Gst.init(None)
            pipeline = Gst.parse_launch(
                f"pipewiresrc fd={fd} path={node} do-timestamp=true ! "
                "videoconvert ! video/x-raw,format=RGB ! "
                "appsink name=out sync=false max-buffers=1 drop=true"
            )
            sink = pipeline.get_by_name("out")
            pipeline.set_state(Gst.State.PLAYING)
            sample = sink.emit("try-pull-sample", int(timeout * 1_000_000_000))
            if sample is None:
                raise RuntimeError("窗口流未产生帧（窗口可能未变化或PipeWire已断开）")
            structure = sample.get_caps().get_structure(0)
            width, height = structure.get_value("width"), structure.get_value("height")
            buffer = sample.get_buffer()
            ok, mapped = buffer.map(Gst.MapFlags.READ)
            if not ok:
                raise RuntimeError("无法读取窗口帧")
            try:
                stride = len(mapped.data) // height
                return Image.frombuffer("RGB", (width, height), bytes(mapped.data),
                                        "raw", "RGB", stride, 1).copy()
            finally:
                buffer.unmap(mapped)
        finally:
            if pipeline is not None:
                pipeline.set_state(Gst.State.NULL)
                pipeline.get_state(2 * Gst.SECOND)
                pipeline = None
            if fd is not None:
                os.close(fd)
            if session is not None:
                dbus.Interface(bus.get_object("org.freedesktop.portal.Desktop", session),
                               "org.freedesktop.portal.Session").Close()
    finally:
        _TOKEN_FILE = old_token_file
