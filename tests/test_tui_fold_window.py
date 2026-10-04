"""Global fold must not append history outside the mounted 20-turn window."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "frontends"))
import tuiapp_v2 as tui
from rich.text import Text
from textual.app import App
from textual.containers import VerticalScroll


class FoldProbe(App):
    fold_mode = True
    _segment_sig = staticmethod(tui.GenericAgentTUI._segment_sig)
    _recent_messages = staticmethod(tui.GenericAgentTUI._recent_messages)
    _assistant_segments = tui.GenericAgentTUI._assistant_segments
    _mount_message = tui.GenericAgentTUI._mount_message
    _mount_assistant_segments = tui.GenericAgentTUI._mount_assistant_segments
    _sync_spinner_widget = tui.GenericAgentTUI._sync_spinner_widget
    _remount_assistant_message = tui.GenericAgentTUI._remount_assistant_message
    action_toggle_fold = tui.GenericAgentTUI.action_toggle_fold

    def compose(self):
        yield VerticalScroll(id="messages")

    def _messages_width(self):
        return 100

    def _render_md(self, text, width):
        # Markdown typography is unrelated; real segmentation and DOM mounting
        # are retained, without requiring a live model or agent credentials.
        return Text(text)


def test_global_fold_preserves_recent_window_and_message_order():
    async def scenario():
        app = FoldProbe()
        messages = []
        for i in range(25):
            messages.append(tui.ChatMessage("user", f"QUESTION_{i}"))
            content = "".join(
                f"\n**LLM Running (Turn {n}) ...**\n\nBODY_{i}_{n}\n"
                for n in range(1, 4)
            )
            messages.append(tui.ChatMessage("assistant", content))
        app.current = SimpleNamespace(messages=messages)
        async with app.run_test(size=(100, 40)) as pilot:
            container = app.query_one("#messages", VerticalScroll)
            recent = app._recent_messages(app.current)
            for message in recent:
                app._mount_message(container, message)
            await pilot.pause()
            for _ in range(4):
                app.action_toggle_fold()
                await pilot.pause()
                visible_widgets = {
                    id(w) for m in recent
                    for w in [m._role_widget, m._body_widget, *m._segment_widgets]
                    if w is not None
                }
                assert all(id(w) in visible_widgets for w in container.children), \
                    "fold appended unmounted historical messages after latest answer"
                assert all(not m._segment_widgets for m in messages[:10]
                           if m.role == "assistant")
                positions = [list(container.children).index(m._role_widget) for m in recent]
                assert positions == sorted(positions)
                assert container.children[-1] is recent[-1]._segment_widgets[-1]
                assistants = [m for m in messages if m.role == "assistant"]
                assert len({-1 in m._toggled_folds for m in assistants}) == 1
            # A detached stale anchor must also not permit an append/remount.
            old = messages[1]
            old._role_widget = tui.SelectableStatic("detached")
            before = list(container.children)
            app._remount_assistant_message(old)
            await pilot.pause()
            assert list(container.children) == before

    asyncio.run(scenario())
