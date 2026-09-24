"""
ljqCtrl — 跨平台 GUI 键鼠/窗口/截图控制
  Windows: 委托 ljqCtrl_win.py (win32api + WGC 后台截图)
  Linux:   X11 = xdotool+mss; GNOME Wayland = Screenshot portal + uinput pointer
CRITICAL: 严禁在此工具链中 import pyautogui。
ljqCtrl Quick Reference:
- dpi_scale: float (Logical = Physical * dpi_scale; Ubuntu X11 通常为 1.0)
- ListWindows(name=None) -> [dict]: 枚举可见窗口 {'id','title','rect':(l,t,r,b),'visible'}
- Activate(hwnd_or_name): 激活窗口 (name=标题子串), 操作前必做
- Click(x, y, check=True): 物理坐标; check=True → 自动比前后像素变化
- SetCursorPos(z); MouseClick(staytime); MouseDClick(staytime)
- Press(cmd, staytime=0): 键盘快捷键 (e.g. 'ctrl+v')
- GrabWindow(hwnd_or_name) -> PIL Image: 窗口客户区截图 (先激活, 不含标题栏)
- GrabWindowBg(hwnd_or_name): Linux 同 GrabWindow (无后台截图)
- FindBlock(fn, wrect=None, threshold=0.8) -> (obj_center, max_val)
- ScreenCapAt(x, y, r=100) -> PIL Image
"""
import sys

if sys.platform == 'win32':
    from ljqCtrl_win import *  # noqa: F401,F403
    from ljqCtrl_win import ListWindows  # noqa: F401
else:
    import os, time, re, subprocess
    import numpy as np
    from PIL import Image
    import cv2

    print('[TIPS] always use physical coordinates! (Linux/X11)')

    def _xdotool(*args, check=True):
        r = subprocess.run(['xdotool', *[str(a) for a in args]], capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f'xdotool {" ".join(map(str, args[:4]))} failed: {r.stderr.strip()[:200]}')
        return r.stdout.strip()

    # ---------- 屏幕几何 ----------
    _wayland = os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland'
    if _wayland:
        swidth, sheight = 0, 0  # 等待用户授权的 Portal 图像确定实际像素尺寸
    else:
        _geo = _xdotool('getdisplaygeometry').split()
        swidth, sheight = int(_geo[0]), int(_geo[1])
    dpi_scale = 1.0
    cwidth, cheight = swidth, sheight
    if _wayland:
        print('Wayland: 截图坐标需先调用 _grab() 初始化（Portal 可能要求用户授权）')
    else:
        print('Screen width & height:', swidth, sheight)
        print('dpi_scale:', dpi_scale)

    # ---------- 截图 (GNOME Wayland: portal; X11: mss) ----------
    def _portal_grab():
        import dbus, dbus.mainloop.glib
        from gi.repository import GLib
        from urllib.parse import urlparse, unquote
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        desktop = bus.get_object('org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop')
        shot = dbus.Interface(desktop, 'org.freedesktop.portal.Screenshot')
        result, loop = {}, GLib.MainLoop()
        token = 'ga_' + os.urandom(8).hex()
        request_path = '/org/freedesktop/portal/desktop/request/' + bus.get_unique_name()[1:].replace('.', '_') + '/' + token
        def respond(code, values):
            result['code'], result['values'] = int(code), values
            loop.quit()
        receiver = bus.add_signal_receiver(respond, signal_name='Response',
            dbus_interface='org.freedesktop.portal.Request', path=request_path)
        timer = None
        try:
            handle = shot.Screenshot('', {'handle_token': dbus.String(token),
                'interactive': dbus.Boolean(False)}, timeout=10)
            if str(handle) != request_path:
                raise RuntimeError(f'portal request path mismatch: {handle}')
            timer = GLib.timeout_add_seconds(20, lambda: (loop.quit(), False)[1])
            loop.run()
            if result.get('code') != 0:
                raise RuntimeError(f'截图未获授权或超时 (portal response={result.get("code")})')
            uri = str(result['values']['uri'])
            parsed = urlparse(uri)
            if parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost'):
                raise RuntimeError('portal 未返回本地截图文件')
            with Image.open(unquote(parsed.path)) as source:
                img = source.convert('RGB')
            return img
        finally:
            receiver.remove()
            if timer is not None:
                GLib.source_remove(timer) if GLib.main_context_default().find_source_by_id(timer) else None

    def _grab(bbox=None):
        if _wayland:
            img = _portal_grab()
            global swidth, sheight, cwidth, cheight
            swidth, sheight = img.size  # portal 输出像素，与 Xwayland 虚拟几何可能不同
            cwidth, cheight = img.size
            if bbox is not None:
                l, t, r, b = [int(v) for v in bbox]
                img = img.crop((max(0, l), max(0, t), min(swidth, r), min(sheight, b)))
        else:
            import mss
            if bbox is None: bbox = (0, 0, swidth, sheight)
            l, t, r, b = [int(v) for v in bbox]
            with mss.mss() as sct:
                raw = sct.grab({'left': l, 'top': t, 'width': max(r-l, 1), 'height': max(b-t, 1)})
                img = Image.frombytes('RGB', raw.size, raw.rgb)
        if img.getextrema() == ((0, 0), (0, 0), (0, 0)):
            raise RuntimeError('截图全黑，拒绝返回无法识别的图像')
        return img

    # ---------- 窗口枚举/激活 ----------
    # xwininfo -root -tree 行格式: '  0x28022c5 "标题": ("wmclass" "WmClass")  2566x2002+1582+320  +1582+320'
    # 顶层窗口 = root 的直接子窗口, 恰为 5 个空格缩进; 更深处是子窗, 跳过
    _XW_RE = re.compile(r'^     (0x[0-9a-f]+)(?: "([^"]*)":|\(has no name\)) .*?(\d+)x(\d+)\+(-?\d+)\+(-?\d+)')
    def ListWindows(name=None):
        """枚举顶层可见窗口。X11/Xwayland 窗口可见; 纯Wayland原生窗口不在列表(需 hyprctl/swaymsg 另查)"""
        r = subprocess.run(['xwininfo', '-root', '-tree', '-stats'], capture_output=True, text=True)
        rows, seen = [], set()
        for line in r.stdout.splitlines():
            m = _XW_RE.search(line)
            if not m: continue
            wid = int(m.group(1), 16)
            title = m.group(2) or ''
            w, h, l, t = int(m.group(3)), int(m.group(4)), int(m.group(5)), int(m.group(6))
            if wid in seen or w < 40 or h < 40: continue  # 过滤 1x1 代理子窗
            seen.add(wid)
            if name and name not in title: continue
            rows.append({'id': wid, 'title': title, 'rect': (l, t, l + w, t + h)})
        return rows
    def FindWindow(cls, name):
        """按标题子串/正则找窗口 → id (int); 无则 0。cls 忽略(X11无类名快速查)"""
        out = _xdotool('search', '--name', re.escape(name) if name else '', check=False)
        ids = [int(x) for x in out.split() if x.isdigit()]
        return ids[0] if ids else 0
    def GetForegroundTitle():
        try: return _xdotool('getactivewindow', 'getwindowname')
        except Exception: return ''
    def Activate(hwnd_or_name):
        """激活窗口到前台。传 id(int) 或标题子串(str)。"""
        if isinstance(hwnd_or_name, str):
            wid = FindWindow(None, hwnd_or_name)
            assert wid, f'窗口未找到: {hwnd_or_name}'
            hwnd_or_name = wid
        _xdotool('windowactivate', '--sync', hwnd_or_name, check=False)
        _xdotool('windowfocus', hwnd_or_name, check=False)
        time.sleep(0.2)
    activate = Activate

    # ---------- 鼠标 ----------
    _mouse_fd = None
    def _mouse():
        """创建纯指针设备；ydotool 的键盘+鼠标混合设备在 GNOME 下被当成键盘。"""
        global _mouse_fd
        if _mouse_fd is None:
            import fcntl, struct, atexit
            fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
            try:
                for code in (1, 3): fcntl.ioctl(fd, 0x40045564, code)  # EV_KEY, EV_ABS
                fcntl.ioctl(fd, 0x40045565, 272)  # BTN_LEFT
                for code in (0, 1): fcntl.ioctl(fd, 0x40045567, code)  # ABS_X, ABS_Y
                # uinput_user_dev: setup 包含 ABS_X/Y 范围
                name = b'GA Wayland Pointer'
                setup = struct.pack('80sHHHHI' + 'i'*64*4, name, 3, 0x2333, 0x6666, 1, 0,
                    *([65535, 65535] + [0]*62), *([0]*64), *([0]*64), *([0]*64))
                os.write(fd, setup)
                fcntl.ioctl(fd, 0x5501)  # UI_DEV_CREATE
            except Exception:
                os.close(fd)
                raise
            _mouse_fd = fd
            def close_mouse():
                fcntl.ioctl(fd, 0x5502)  # UI_DEV_DESTROY
                os.close(fd)
            atexit.register(close_mouse)
            time.sleep(0.7)  # 等待 GNOME/libinput 识别设备
        return _mouse_fd
    def _mouse_event(typ, code, value):
        import struct
        os.write(_mouse(), struct.pack('llHHi', 0, 0, typ, code, value))
        os.write(_mouse(), struct.pack('llHHi', 0, 0, 0, 0, 0))  # SYN_REPORT
    def MouseDown():
        if _wayland: _mouse_event(1, 272, 1)
        else: _xdotool('mousedown', 1)
    def MouseUp():
        if _wayland: _mouse_event(1, 272, 0)
        else: _xdotool('mouseup', 1)
    def MouseClick(staytime=0.05):
        if _wayland:
            MouseDown(); time.sleep(max(staytime, 0.02)); MouseUp()
        else: _xdotool('click', 1)
        time.sleep(staytime)
    def MouseDClick(staytime=0.05):
        if _wayland: MouseClick(staytime); MouseClick(staytime)
        else:
            _xdotool('click', '--repeat', 2, '--delay', '50', 1)
            time.sleep(staytime)
    def SetCursorPos(z):
        if _wayland:
            x, y = [int(v) for v in z]
            if not (0 <= x < swidth and 0 <= y < sheight):
                raise ValueError(f'坐标不在 Portal 截图范围内: {(x, y)} / {(swidth, sheight)}')
            _mouse_event(3, 0, round(x * 65535 / swidth))
            _mouse_event(3, 1, round(y * 65535 / sheight))
        else:
            z = tuple(map(lambda v: int(v * dpi_scale), z))
            _xdotool('mousemove', '--sync', z[0], z[1])
        time.sleep(0.05)

    def ScreenCapAt(x, y, r=100):
        """物理坐标(x,y)为中心±r的屏幕截图 → PIL Image"""
        return _grab((x - r, y - r, x + r, y + r))

    def Click(x, y=None, check=True):
        if type(x) is type(tuple()): x, y = int(x[0]), int(x[1])
        if check: before, fg_before = ScreenCapAt(x, y), GetForegroundTitle()
        SetCursorPos((x, y))
        MouseClick()
        if check:
            time.sleep(0.5)
            after = ScreenCapAt(x, y)
            b, a = np.array(before), np.array(after)
            diff = np.sum(np.any(b != a, axis=2))
            total = b.shape[0] * b.shape[1]
            fg_after = GetForegroundTitle()
            fg_changed = fg_before != fg_after
            print(f'[Click check] {diff}/{total} px changed ({diff/total*100:.1f}%) | fg: "{fg_after}" {"⚠️CHANGED" if fg_changed else ""}')
            return after
    click = Click

    # ---------- 键盘 ----------
    _XKEY = {
        'backspace': 'BackSpace', 'tab': 'Tab', 'enter': 'Return', 'return': 'Return',
        'shift': 'shift', 'ctrl': 'ctrl', 'control': 'ctrl', 'alt': 'alt', 'alt_gr': 'AltGr',
        'esc': 'Escape', 'escape': 'Escape', 'space': 'space',
        'page_up': 'Page_Up', 'page_down': 'Page_Down', 'end': 'End', 'home': 'Home',
        'left_arrow': 'Left', 'up_arrow': 'Up', 'right_arrow': 'Right', 'down_arrow': 'Down',
        'left': 'Left', 'up': 'Up', 'right': 'Right', 'down': 'Down',
        'del': 'Delete', 'delete': 'Delete', 'ins': 'Insert', 'insert': 'Insert',
        'print_screen': 'Print', 'print': 'Print', 'pause': 'Pause', 'clear': 'Clear',
        'caps_lock': 'Caps_Lock', 'num_lock': 'Num_Lock', 'scroll_lock': 'Scroll_Lock',
        'menu': 'Menu', 'win': 'super', 'command': 'super', 'super': 'super',
        'left_shift': 'shift_L', 'right_shift': 'shift_R', 'right_shift ': 'shift_R',
        'left_control': 'ctrl_L', 'right_control': 'ctrl_R',
        'left_alt': 'alt_L', 'right_alt': 'alt_R',
        'left_super': 'super_L', 'right_super': 'super_R',
        'plus': 'plus', 'minus': 'minus', 'asterisk': 'asterisk', 'slash': 'slash',
        'comma': 'comma', 'period': 'period', 'semicolon': 'semicolon',
        'apostrophe': 'apostrophe', 'bracketleft': 'bracketleft', 'bracketright': 'bracketright',
        'backslash': 'backslash', 'grave': 'grave', 'equal': 'equal',
    }
    def _xkey(k):
        if k in _XKEY: return _XKEY[k]
        if re.fullmatch(r'numpad_\d', k): return k.replace('_', '')
        return k
    def Press(cmd, staytime=0):
        if type(cmd) is list: cmds = [x.lower() for x in cmd]
        else: cmds = cmd.lower().split('+')
        keys = [_xkey(z) for z in cmds]
        if _wayland:
            # xdotool 仅送到 Xwayland；从当前 X keymap 换算 Linux evdev 扫描码，
            # 再通过 ydotool 的 keyboard 设备输入原生 Wayland 窗口。
            mapping = {}
            for line in subprocess.check_output(['xmodmap', '-pke'], text=True).splitlines():
                m = re.match(r'keycode\s+(\d+)\s+=\s+(.*)', line)
                if m:
                    for name in m.group(2).split():
                        if name != 'NoSymbol': mapping.setdefault(name.lower(), int(m.group(1)) - 8)
            aliases = {'ctrl': 'control_l', 'control': 'control_l', 'shift': 'shift_l',
                       'alt': 'alt_l', 'super': 'super_l', 'win': 'super_l',
                       'altgr': 'alt_r', 'space': 'space'}
            codes = []
            for key in keys:
                key = aliases.get(key.lower(), key.lower())
                if key not in mapping or mapping[key] < 0:
                    raise ValueError(f'当前键盘布局不支持按键: {key}')
                codes.append(mapping[key])
            seq = [f'{code}:1' for code in codes] + [f'{code}:0' for code in reversed(codes)]
            if staytime:
                subprocess.run(['ydotool', 'key', *seq[:len(codes)]], check=True)
                try: time.sleep(staytime)
                finally: subprocess.run(['ydotool', 'key', *seq[len(codes):]], check=True)
            else:
                subprocess.run(['ydotool', 'key', *seq], check=True)
        else:
            joined = '+'.join(keys)
            if staytime:
                _xdotool('keydown', joined, check=False)
                time.sleep(staytime)
                _xdotool('keyup', joined, check=False)
            else:
                _xdotool('key', joined)
    press = Press

    # ---------- 窗口截图 ----------
    def GrabWindow(hwnd_or_name):
        """窗口客户区截图(不含标题栏/边框), 先激活。截图内坐标偏移原点 = 客户区左上角(物理)"""
        if _wayland:
            raise NotImplementedError('Wayland 原生窗口不暴露 X11 窗口ID/几何；请用 _grab() 经 Portal 授权截图，再按画面坐标裁剪')
        if isinstance(hwnd_or_name, str):
            wid = FindWindow(None, hwnd_or_name)
            assert wid, f'窗口未找到: {hwnd_or_name}'
            hwnd_or_name = wid
        Activate(hwnd_or_name)
        g = {}
        for _ in range(5):  # 激活瞬间几何可能未就绪, 重试
            out = _xdotool('getwindowgeometry', '--shell', hwnd_or_name, check=False)
            g = dict(kv.split('=', 1) for kv in out.splitlines() if '=' in kv)
            if 'WIDTH' in g and int(g.get('WIDTH', 0)) > 0: break
            time.sleep(0.15)
        if 'WIDTH' not in g: raise RuntimeError(f'getwindowgeometry 失败: {hwnd_or_name}')
        l, t, w, h = int(g['X']), int(g['Y']), int(g['WIDTH']), int(g['HEIGHT'])
        return _grab((l, t, l + w, t + h))
    def GrabWindowBg(hwnd_or_name, timeout=5):
        """Linux: 无WGC后台截图, 等同 GrabWindow(需窗口可见)"""
        return GrabWindow(hwnd_or_name)

    def imshow(mt, sec=0):
        cv2.imshow('cc', mt); cv2.waitKey(sec)

    def GetWRect(sr):
        num = int(sr[-1])
        l, u, r, b = 0, 0, swidth, sheight
        if 'left' in sr: r = swidth // num
        if 'right' in sr: l = swidth * (num - 1) // num
        if 'top' in sr: b = sheight // num
        if 'bottom' in sr: u = sheight * (num - 1) // num
        return [l, u, r, b]

    def FindBlock(fn, wrect=None, verbose=0, threshold=0.8):
        tic = time.process_time()
        if wrect is not None and isinstance(wrect, Image.Image):
            scr, wrect = wrect, None
        else:
            if isinstance(wrect, str): wrect = GetWRect(wrect)
            scr = _grab(wrect)
        blc = Image.open(fn) if isinstance(fn, str) else fn
        T = cv2.cvtColor(np.array(blc), cv2.COLOR_RGB2BGR)
        B = cv2.cvtColor(np.array(scr), cv2.COLOR_RGB2BGR)
        tsh, tsw = T.shape[:2]
        if verbose: print('T.shape:', T.shape, '\t', 'B.shape:', B.shape)
        res = cv2.matchTemplate(B, T, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
        oj, oi = max_loc
        if wrect is None: wrect = [0, 0, scr.size[0], scr.size[1]]
        obj = (oj + wrect[0] + tsw // 2, oi + wrect[1] + tsh // 2)
        if verbose:
            print(f'Max match: {max_val:.4f} at ({oj}, {oi}) cost: {time.process_time() - tic:.3f}s')
        return obj, max_val

    if __name__ == '__main__':
        print('completed %.3f' % time.process_time())
