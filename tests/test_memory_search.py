import tempfile
import unittest
from pathlib import Path

from memory_search import MemorySearch


class MemorySearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.memory = self.root / "memory"
        self.memory.mkdir()
        self.index = self.root / "sidecar"

    def tearDown(self):
        self.tmp.cleanup()

    def lexical_search(self):
        search = MemorySearch(memory_dir=self.memory, index_dir=self.index)
        # Exercise the documented fallback regardless of whether zvec is
        # installed in the test environment.
        search._zvec = None
        return search

    def test_external_root_fallback_and_source_verification(self):
        source = self.memory / "L3_demo.md"
        source.write_text("verifiable alpha source\n", encoding="utf-8")
        search = self.lexical_search()

        self.assertEqual(search.rebuild(), 1)
        hit = search.search("verifiable alpha", k=1, layers=["L3"])
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["backend"], "lexical-fallback")
        self.assertTrue(hit[0]["verified"])
        self.assertEqual(hit[0]["path"], "memory/L3_demo.md")

        # A direct stale-sidecar read must expose the changed source and never
        # report it as verified.  Normal search updates first and excludes the
        # old query, demonstrated below.
        source.write_text("changed source text\n", encoding="utf-8")
        docs = search._read_json(search.index / "documents.json", [])
        stale = search._lexical("verifiable alpha", docs, 1, {"L3"})
        self.assertEqual(len(stale), 1)
        self.assertFalse(stale[0]["verified"])
        self.assertEqual(stale[0]["text"], "changed source text")
        self.assertEqual(search.search("verifiable alpha", k=1, layers=["L3"]), [])

    def test_fallback_incrementally_removes_deleted_source(self):
        source = self.memory / "global_mem.txt"
        source.write_text("removable memory token\n", encoding="utf-8")
        search = self.lexical_search()
        search.rebuild()
        self.assertTrue(search.search("removable token", k=1, layers=["L2"]))

        source.unlink()
        self.assertEqual(search.update(), 0)
        self.assertEqual(search.search("removable token", k=1, layers=["L2"]), [])


if __name__ == "__main__":
    unittest.main()
