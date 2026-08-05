#!/bin/bash
# Firefox tmwd_cdp_bridge 启动脚本
# 用途：启动geckodriver+Firefox+安装扩展+连接TMWebDriver master
# 配合 TMWebDriver master 使用，web_scan/web_execute_js 通过 Firefox 工作
# 用法：bash start_firefox_bridge.sh

set -e

GA_ROOT="${GA_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG_DIR="/tmp/ga_firefox_bridge"
mkdir -p "$LOG_DIR"

echo "=== Firefox Bridge Startup ==="
echo "GA_ROOT: $GA_ROOT"
echo "Logs: $LOG_DIR"

GECKODRIVER_BIN="${GECKODRIVER_BIN:-$(command -v geckodriver || true)}"
if [ -z "$GECKODRIVER_BIN" ] && [ -x /snap/bin/geckodriver ]; then
    GECKODRIVER_BIN=/snap/bin/geckodriver
fi
if [ -z "$GECKODRIVER_BIN" ]; then
    echo "ERROR: geckodriver not found; set GECKODRIVER_BIN or install geckodriver" >&2
    exit 1
fi
DEFAULT_URL="${GA_FIREFOX_BRIDGE_URL:-https://www.bing.com/}"

# 1. 先检查 TMWebDriver 是否已经有可用标签；已有则不再弹窗/不再开新示例页
echo "[1/5] Checking existing TMWebDriver tabs..."
EXISTING_TMWD_COUNT=$(cd "$GA_ROOT" && python3 - <<'PYEOF'
import sys
sys.path.insert(0, '.')
try:
    from TMWebDriver import TMWebDriver
    d = TMWebDriver()
    print(len(d.get_all_sessions()))
except Exception:
    print(0)
PYEOF
)
if [ "${EXISTING_TMWD_COUNT:-0}" -gt 0 ]; then
    echo "  → Existing TMWebDriver tabs: $EXISTING_TMWD_COUNT; reuse current browser, no new tab/window."
    exit 0
fi

# 2. 确认 TMWebDriver master 在运行
echo "[2/5] Checking TMWebDriver master..."
if ! ss -tlnp 2>/dev/null | grep -q 18765; then
    echo "  → TMWebDriver master not running, starting..."
    cd "$GA_ROOT" && nohup python3 -c "
import sys, time
sys.path.insert(0, '.')
from TMWebDriver import TMWebDriver
d = TMWebDriver(host='127.0.0.1', port=18765)
print('TMWebDriver master started', flush=True)
import threading
threading.Event().wait()
" > "$LOG_DIR/tmwd_master.log" 2>&1 &
    MASTER_PID=$!
    sleep 3
    echo "  → Master started (PID: $MASTER_PID)"
else
    echo "  → TMWebDriver master already running"
fi

# 3. 复用 9222 上已有 geckodriver；没有才启动。新启动默认 headless，避免弹窗。
echo "[3/5] Checking geckodriver on port 9222..."
GECKO_PID=$(ss -tlnp 2>/dev/null | awk '/127\.0\.0\.1:9222/ && /geckodriver/ {print}' | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | sort -u | head -1)
STARTED_NEW_GECKO=0
if [ -n "$GECKO_PID" ]; then
    echo "  → Reusing geckodriver PID: $GECKO_PID"
else
    echo "  → Starting geckodriver on port 9222..."
    "$GECKODRIVER_BIN" --port 9222 --log info > "$LOG_DIR/geckodriver.log" 2>&1 &
    GECKO_PID=$!
    STARTED_NEW_GECKO=1
    echo "  → geckodriver PID: $GECKO_PID"
    sleep 3
fi
echo "$GECKO_PID" > /tmp/ga_gecko_pid

if ! ss -tlnp 2>/dev/null | grep -q 9222; then
    echo "  → ERROR: geckodriver failed to start"
    tail -10 "$LOG_DIR/geckodriver.log" 2>/dev/null || true
    exit 1
fi

# 4. 通过 WebDriver API 复用已有 session；无 session 才创建 Firefox。已有窗口时只开新标签页，不拉新实例。
echo "[4/5] Preparing Firefox session and addon..."
GA_ROOT="$GA_ROOT" STARTED_NEW_GECKO="$STARTED_NEW_GECKO" python3 << 'PYEOF'
import requests, json, time, sys, os
from pathlib import Path

base_url = "http://127.0.0.1:9222"
ext_path = str(Path(os.environ["GA_ROOT"]) / "assets" / "tmwd_cdp_bridge")
session_file = Path("/tmp/ga_firefox_session.json")
default_url = os.environ.get("GA_FIREFOX_BRIDGE_URL", "https://www.bing.com/")
headless = os.environ.get("GA_FIREFOX_BRIDGE_HEADLESS", "1").lower() not in ("0", "false", "no", "off")
started_new_gecko = os.environ.get("STARTED_NEW_GECKO") == "1"

def webdriver(method, path, **kwargs):
    r = requests.request(method, base_url + path, timeout=kwargs.pop("timeout", 15), **kwargs)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    if r.status_code >= 400 or (isinstance(data, dict) and data.get("value", {}).get("error")):
        raise RuntimeError(f"{method} {path} failed: HTTP {r.status_code} {data}")
    return data

def saved_session_id():
    if not session_file.exists():
        return None
    try:
        sid = json.loads(session_file.read_text()).get("session_id")
        if not sid:
            return None
        webdriver("GET", f"/session/{sid}/url", timeout=5)
        return sid
    except Exception:
        return None

try:
    session_id = saved_session_id()
    created_session = False
    if session_id:
        print(f"Reusing WebDriver session: {session_id}")
    else:
        # Geckodriver only allows one active session. If status says session already started but
        # our saved id is stale/missing, ask user to restart that geckodriver rather than spawn windows.
        st = webdriver("GET", "/status", timeout=5).get("value", {})
        if st.get("ready") is False and not started_new_gecko:
            raise RuntimeError("geckodriver already has a session but /tmp/ga_firefox_session.json is stale; restart only the 9222 geckodriver or restore the session file")
        args = ["-nosandbox"]
        if headless:
            args.append("-headless")
        r = webdriver("POST", "/session", json={
            "capabilities": {
                "alwaysMatch": {
                    "browserName": "firefox",
                    "acceptInsecureCerts": True,
                    "moz:firefoxOptions": {"args": args}
                }
            }
        }, timeout=15)
        data = r
        session_id = data["value"]["sessionId"]
        caps = data["value"].get("capabilities", {})
        print(f"Session created: {session_id}")
        print(f"Browser: {caps.get('browserName')} {caps.get('browserVersion')} headless={headless}")
        created_session = True

    # Install addon (temporary, persists for this browser session). Reinstall is harmless.
    try:
        r = webdriver("POST", f"/session/{session_id}/moz/addon/install", json={
            "path": ext_path,
            "temporary": True
        }, timeout=10)
        print(f"Addon installed: {r.get('value')}")
    except Exception as e:
        print(f"Addon install warning: {e}")

    # If reusing a visible/controlled Firefox session, open a new tab in that same browser window;
    # if this is a brand-new headless session, navigate the initial tab.
    if created_session:
        webdriver("POST", f"/session/{session_id}/url", json={"url": default_url}, timeout=15)
        print(f"Navigated initial tab to {default_url}")
    else:
        try:
            r = webdriver("POST", f"/session/{session_id}/window/new", json={"type": "tab"}, timeout=10)
            handle = r.get("value", {}).get("handle")
            if handle:
                webdriver("POST", f"/session/{session_id}/window", json={"handle": handle}, timeout=10)
            webdriver("POST", f"/session/{session_id}/url", json={"url": default_url}, timeout=15)
            print(f"Opened new tab in existing Firefox: {default_url}")
        except Exception as e:
            print(f"New-tab fallback: {e}; navigating current tab")
            webdriver("POST", f"/session/{session_id}/url", json={"url": default_url}, timeout=15)

    info = {
        "session_id": session_id,
        "geckodriver_pid": int(Path("/tmp/ga_gecko_pid").read_text().strip()) if Path("/tmp/ga_gecko_pid").exists() else 0,
        "created_at": time.time(),
        "headless": headless if created_session else None
    }
    session_file.write_text(json.dumps(info))
    print("Session info saved")

    r = webdriver("POST", f"/session/{session_id}/execute/sync", json={
        "script": "return 'Firefox bridge OK: ' + navigator.userAgent.substring(0, 50);",
        "args": []
    }, timeout=10)
    print(f"Test: {r.get('value', '')}")

except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)
PYEOF

echo ""
echo "[5/5] Verifying TMWebDriver connection..."
sleep 3
cd "$GA_ROOT" && python3 -c "
import sys
sys.path.insert(0, '.')
from TMWebDriver import TMWebDriver
d = TMWebDriver()
sessions = d.get_all_sessions()
print(f'TMWebDriver sessions: {len(sessions)}')
for s in sessions:
    print(f'  [{s[\"id\"]}] {s.get(\"title\",\"\")} - {s.get(\"url\",\"\")[:60]}')
if sessions:
    print('✅ Firefox bridge is working!')
else:
    print('❌ No sessions detected')
"

echo ""
echo "=== Firefox Bridge Startup Complete! ==="
echo "  geckodriver: http://127.0.0.1:9222"
echo "  TMWebDriver: ws://127.0.0.1:18765"
echo "  Firefox PID: $(pgrep -f 'firefox.*marionette' | head -1 || echo 'N/A')"
echo ""
echo "To stop: kill the geckodriver PID listening on 127.0.0.1:9222"
echo "Logs: $LOG_DIR/"
