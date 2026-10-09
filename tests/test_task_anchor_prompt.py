import unittest
from types import SimpleNamespace

import agentmain
from ga import GenericAgentHandler


class TaskAnchorPromptTests(unittest.TestCase):
    def test_loaded_prompts_define_read_only_anchor_and_progress_notes(self):
        original = agentmain.lang_suffix
        try:
            for suffix, required in [
                ('', ['唯一权威锚点', '对模型只读', '禁止写入任何 task_anchor 标签', '包括追加要求']),
                ('_en', ['sole authoritative anchor', 'read-only to the model', 'Never put task_anchor tags', 'including later additions']),
            ]:
                agentmain.lang_suffix = suffix
                prompt = agentmain.get_system_prompt()
                for phrase in required:
                    self.assertIn(phrase, prompt)
        finally:
            agentmain.lang_suffix = original
        tool = next(t['function'] for t in agentmain.TOOLS_SCHEMA
                    if t['function']['name'] == 'update_working_checkpoint')
        description = tool['parameters']['properties']['key_info']['description']
        self.assertIn('NOT the task anchor', description)
        self.assertIn('never create, copy, rewrite, or nest task_anchor tags', description)
        self.assertIn('including mid-task additions', description)

    def test_checkpoint_progress_keeps_one_framework_anchor(self):
        handler = GenericAgentHandler(SimpleNamespace(verbose=False), original_task='Deliver six comparisons')
        handler.current_turn = 2
        progress = 'Currently executing the later user request: use 85% memory. Pending: six comparisons.'
        update = handler.do_update_working_checkpoint({'key_info': progress}, None)
        next(update)
        with self.assertRaises(StopIteration) as done:
            next(update)
        prompt = done.exception.value.next_prompt
        opening = '<' + 'task_anchor>'
        self.assertEqual(prompt.count(opening), 1)
        self.assertIn(opening + 'Deliver six comparisons', prompt)
        self.assertIn(progress, prompt)
        self.assertEqual(handler.original_task, 'Deliver six comparisons')


if __name__ == '__main__':
    unittest.main()
