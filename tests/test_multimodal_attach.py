# -*- coding: utf-8 -*-
"""Regression: pasted images must reach the model as image bytes.

The bug: `on_input_area_submitted` expanded `[Image #N]` into a bare path and
then ran `re.findall(r"\\[Image #\\d+: (.*?)\\]", text)` over the *expanded*
text, which can never match; `put_task` was called without `images=` anyway.
So the multimodal backend never received an image block.

These checks fail if either half of that wiring regresses.
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "frontends"))

import tuiapp_v2 as T  # noqa: E402


class _Fake(  # mimics the attribute surface collect_image_paths needs
    object
):
    pass


def _area_with(pastes):
    a = _Fake()
    a._pastes = dict(pastes)
    a._IMAGE_RE = T.InputArea._IMAGE_RE  # class attribute is enough here
    a._PLACEHOLDER_RES = T.InputArea._PLACEHOLDER_RES
    return a


class CollectImagePathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.img = os.path.join(self.tmp.name, "shot.png")
        with open(self.img, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    def test_image_placeholder_resolves_to_existing_path(self):
        raw = "look at this [Image #1] please"
        got = T.InputArea.collect_image_paths(_area_with({1: self.img}), raw)
        self.assertEqual(got, [self.img])

    def test_expanded_text_alone_would_yield_nothing(self):
        """Documents the original bug: after expansion the marker is gone."""
        import re
        area = _area_with({1: self.img})
        raw = "[Image #1]"
        self.assertEqual(T.InputArea.expand_placeholders(area, raw), self.img)
        # the old, buggy extraction ran on the expanded text and found nothing
        self.assertEqual(re.findall(r"\[Image #\d+: (.*?)\]", self.img), [])

    def test_missing_file_and_duplicates_are_dropped(self):
        raw = "[Image #1] [Image #2] [Image #1]"
        got = T.InputArea.collect_image_paths(
            _area_with({1: self.img, 2: os.path.join(self.tmp.name, "gone.png")}), raw)
        self.assertEqual(got, [self.img])

    def test_submit_passes_images_to_put_task(self):
        src = open(os.path.join(ROOT, "frontends", "tuiapp_v2.py"),
                   encoding="utf-8").read()
        self.assertIn("collect_image_paths(value)", src)
        self.assertIn('put_task(text, source="user", images=', src)


class NativeImagePayloadTest(unittest.TestCase):
    """The exact block agentmain builds must survive conversion to the
    wire payload as a data: URL — that is what makes the model actually see
    the picture."""

    def test_claude_style_block_becomes_image_url(self):
        import base64
        import llmcore
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "shot.png")
            raw = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
            with open(p, "wb") as f:
                f.write(raw)
            # mirrors agentmain.run's block construction verbatim
            blocks = [{"type": "text", "text": "what is this?"}]
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.b64encode(raw).decode()}})
            out = llmcore._msgs_claude2oai([{"role": "user", "content": blocks}])
        parts = out[0]["content"]
        imgs = [p for p in parts if p.get("type") == "image_url"]
        self.assertEqual(len(imgs), 1, out)
        url = imgs[0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"), url[:40])
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), raw)


class LocalOcrOnlyTest(unittest.TestCase):
    def test_ocr_never_rotates_cloud_models(self):
        src = open(os.path.join(ROOT, "media_api.py"), encoding="utf-8").read()
        self.assertNotIn("VISION_FALLBACKS", src)
        self.assertNotIn("resolve_session", src)
        self.assertIn("_LOCAL_OCR_CHAIN", src)


if __name__ == "__main__":
    unittest.main()
