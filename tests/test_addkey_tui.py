# -*- coding: utf-8 -*-
"""Regression: TUI `/addkey` adds a provider without leaking the key.

Drives the real GenericAgentTUI headlessly against a throwaway mykey.py.

The security property is the point of the test: `/addkey` must NOT route the
draft through the agent (chat messages are replayed to the LLM and written to
the session log), and the key must not survive in the input history.
"""
import asyncio, os, shutil, sys, tempfile

ROOT = "/home/pushuai/GenericAgent"
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "frontends"))

import tuiapp_v2 as T          # noqa: E402
import agentmain               # noqa: E402
import mykey_admin             # noqa: E402

PASS = FAIL = 0
def check(name, ok, detail=""):
    global PASS, FAIL
    if ok: PASS += 1; print(f"  ✅ {name}")
    else:  FAIL += 1; print(f"  ❌ {name}  {detail}")

RAW_KEY = "sk-abcdefghijklmnop"

class Dummy(agentmain.GenericAgent):
    def run(self):
        import time
        while True: time.sleep(.5)

def prewrite(tmp):
    body = ("native_config = {\n    'aihub1': {\n        'name': 'a',\n"
            "        'apikey': \"k1\",\n        'apibase': \"https://a.example\",\n"
            "        'model': \"gpt-5\",\n        'protocol': \"oai\",\n    },\n}\n")
    with open(os.path.join(tmp, "mykey.py"), "w", encoding="utf-8") as f:
        f.write(body)

async def main():
    tmp = tempfile.mkdtemp(prefix="addkey_")
    prewrite(tmp)
    mykey_admin.mykey_path = lambda root="": os.path.join(tmp, "mykey.py")

    app = T.GenericAgentTUI(agent_factory=Dummy)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        print("[1] /addkey arms the form and masks the key on the card")
        app._cmd_addkey([], raw="/addkey")
        await pilot.pause()
        sess = app.current
        check("form armed", bool(sess.addkey_pending))
        check("draft consumed by the form", app._take_addkey_draft(
            f"name: my-new\napikey: {RAW_KEY}\nabibase: x"[:0] +
            f"name: my-new\napikey: {RAW_KEY}\napibase: https://new.example/v1\nmodel: gpt-5.6-x\nprotocol: claude"))
        await pilot.pause()
        check("pending cleared", sess.addkey_pending is None)
        card = [m for m in sess.messages if m.kind == "choice"][-1]
        check("masked on the card", RAW_KEY not in card.content and "sk-a" in card.content)
        check("no message leaks the key",
              not any(RAW_KEY in (m.content or "") for m in sess.messages))
        check("input history not polluted",
              not any(RAW_KEY in h for h in app.query_one("#input")._input_history))

        print("[2] confirming writes one entry into native_config")
        card.on_select("yes")
        await pilot.pause()
        text = open(os.path.join(tmp, "mykey.py"), encoding="utf-8").read()
        check("wrote a new entry", "'claude1'" in text, text[:200])
        check("still one native_config dict", text.count("native_config = {") == 1)
        check("original entry preserved", "'aihub1'" in text)
        check("backup taken", any(".bak-" in f for f in os.listdir(tmp)))
        import ast; ast.parse(text)
        check("file parses", True)

        print("[3] Esc abandons the form")
        app._cmd_addkey([], raw="/addkey")
        await pilot.pause()
        check("re-armed", bool(sess.addkey_pending))
        app.action_escape()
        await pilot.pause()
        check("disarmed by Esc", sess.addkey_pending is None)

        print("[4] bad draft is rejected without writing")
        before = open(os.path.join(tmp, "mykey.py"), encoding="utf-8").read()
        app._cmd_addkey([], raw="/addkey")
        await pilot.pause()
        app._take_addkey_draft("name: x\napikey: k")     # missing apibase/model
        await pilot.pause()
        check("nothing written", open(os.path.join(tmp, "mykey.py"), encoding="utf-8").read() == before)
        check("form stays armed after a bad draft", bool(sess.addkey_pending))

    shutil.rmtree(tmp, ignore_errors=True)

asyncio.run(main())
print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
