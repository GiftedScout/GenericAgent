"""
ljqCtrl — 跨平台 GUI 键鼠/窗口/截图控制
  Windows: 委托 ljqCtrl_win.py (win32api + WGC 后台截图)
  Linux:   xdotool + mss (X11/Xwayland); 纯Wayland原生窗口无法经X11激活, 此时用 grim+ydotool
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
    _geo = _xdotool('getdisplaygeometry').split()
    swidth, sheight = int(_geo[0]), int(_geo[1])
    dpi_scale = 1.0  # X11 坐标即物理像素
    cwidth, cheight = swidth, sheight
    print('Screen width & height:', swidth, sheight)
    print('dpi_scale:', dpi_scale)

    # ---------- 截图 (mss) ----------
    import mss
    _MSS = getattr(mss, 'MSS', mss.mss)
    _sct = None
    def _sct_get():
        global _sct
        if _sct is None: _sct = _MSS()
        return _sct
    def _grab(bbox=None):
        if bbox is None: bbox = (0, 0, swidth, sheight)
        l, t, r, b = [int(v) for v in bbox]
        w, h = max(r - l, 1), max(b - t, 1)
        raw = _sct_get().grab({'left': l, 'top': t, 'width': w, 'height': h})
        img = Image.frombytes('RGB', (w, h), raw.rgb, 'raw', 'RGB')
        # Wayland 安全边界检测: X11/Xwayland 下 XGetImage 不共享像素 → 全黑帧。
        # 静默返回黑图会让 FindBlock/Click check 产生无意义的假结果, 必须显式报错。
        if img.getextrema() == ((0, 0), (0, 0), (0, 0)):
            raise RuntimeError(
                '截图全黑: 当前 GNOME Wayland 会话不向 X11 客户端共享像素(mss/grim/PIL 均不可用)。'
                '键鼠/窗口操作仍可用; 截图需切换到 Xorg 会话, 或让用户手动截屏后传路径。')
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
    def MouseDown(): _xdotool('mousedown', 1)
    def MouseUp(): _xdotool('mouseup', 1)
    def MouseClick(staytime=0.05):
        _xdotool('click', 1); time.sleep(staytime)
    def MouseDClick(staytime=0.05):
        _xdotool('click', '--repeat', 2, '--delay', '50', 1); time.sleep(staytime)
    def SetCursorPos(z):
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
        keys = '+'.join(_xkey(z) for z in cmds)
        if staytime:
            _xdotool('keydown', keys, check=False)
            time.sleep(staytime)
            _xdotool('keyup', keys, check=False)
        else:
            _xdotool('key', keys)
    press = Press

    # ---------- 窗口截图 ----------
    def GrabWindow(hwnd_or_name):
        """窗口客户区截图(不含标题栏/边框), 先激活。截图内坐标偏移原点 = 客户区左上角(物理)"""
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
