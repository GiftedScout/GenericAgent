# -*- coding: utf-8 -*-
"""End-to-end regression: pasting an image in the real TUI must hand the
image *path* to the agent's task queue (which is what makes the multimodal
backend receive an image block).

Drives GenericAgentTUI headlessly, types the `[Image #N]` placeholder the
paste handler would insert, presses Enter, and inspects the queue item the
agent actually received.  Before the fix this queue item carried
`images == []` because the image regex was run on already-expanded text.
"""
import asyncio
import os
import sys
import tempfile

ROOT = "/home/pushuai/GenericAgent"
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "frontends"))

import tuiapp_v2 as T          # noqa: E402
import agentmain               # noqa: E402

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


class Dummy(agentmain.GenericAgent):
    """Swallow the run loop; we only inspect what put_task() received."""
    def run(self):
        import time
        while True:
            time.sleep(.5)


async def main():
    tmp = tempfile.mkdtemp(prefix="mm_attach_")
    img = os.path.join(tmp, "shot.png")
    # 1x1 PNG — real bytes so isfile()/content checks are honest.
    with open(img, "wb") as f:
        f.write(bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000a49444154789c63000100000500010d0a2db40000"
            "000049454e44ae426082"))

    app = T.GenericAgentTUI(agent_factory=Dummy)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        sess = app.current

        inp = app.query_one("#input")
        inp._paste_counter += 1
        inp._pastes[1] = img
        inp.text = "[Image #1] what is in this picture?"

        await pilot.press("enter")
        await pilot.pause()

        # The agent thread is a Dummy; read the queued task dict directly.
        item = sess.agent.task_queue.get_nowait()
        check("query reaches agent", "what is in this picture?" in item["query"], item["query"])
        check("query text has the raw path (placeholder expanded)",
              img in item["query"], item["query"])
        check("images carries the real path", item.get("images") == [img], item.get("images"))
        check("message bubble mirrors the image", any(
            m.image_paths == [img] for m in sess.messages), [m.image_paths for m in sess.messages])

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


asyncio.run(main())
print(f"\n=== {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
