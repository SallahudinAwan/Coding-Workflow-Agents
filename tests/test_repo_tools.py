import tempfile
import unittest
from pathlib import Path

from code_agent.repo_tools import RepositoryTools


class RepositoryToolsTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text(
            "def hello():\n    return 'hello'\n", encoding="utf-8"
        )
        self.tools = RepositoryTools(self.root)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_lists_reads_and_searches_files(self):
        self.assertEqual(self.tools.list_files("*.py"), "src/app.py")
        self.assertIn("return 'hello'", self.tools.read_file("src/app.py"))
        self.assertIn("src/app.py:2", self.tools.search_code("hello"))

    def test_replaces_unique_text(self):
        result = self.tools.replace_in_file("src/app.py", "'hello'", "'hi'")
        self.assertIn("Updated", result)
        self.assertIn("'hi'", (self.root / "src" / "app.py").read_text())

    def test_write_creates_new_file(self):
        result = self.tools.write_file("src/new.py", "value = 1\n")
        self.assertIn("Created", result)
        self.assertEqual((self.root / "src" / "new.py").read_text(), "value = 1\n")

    def test_write_refuses_to_overwrite_existing_file(self):
        original = (self.root / "src" / "app.py").read_text()
        result = self.tools.write_file("src/app.py", "replacement")
        self.assertIn("refusing to overwrite", result)
        self.assertEqual((self.root / "src" / "app.py").read_text(), original)

    def test_replace_refuses_whole_file_rewrite(self):
        original = (self.root / "src" / "app.py").read_text()
        result = self.tools.replace_in_file("src/app.py", original, "replacement")
        self.assertIn("too large", result)
        self.assertEqual((self.root / "src" / "app.py").read_text(), original)

    def test_replace_preserves_windows_line_endings(self):
        path = self.root / "src" / "windows.py"
        path.write_bytes(b"first\r\nsecond\r\nthird\r\n")
        result = self.tools.replace_in_file(
            "src/windows.py", "second\nthird", "changed\nthird"
        )
        self.assertIn("Updated", result)
        self.assertEqual(path.read_bytes(), b"first\r\nchanged\r\nthird\r\n")

    def test_write_cannot_escape_repository(self):
        with self.assertRaises(ValueError):
            self.tools.write_file("../outside.txt", "no")

    def test_git_metadata_is_protected(self):
        with self.assertRaises(ValueError):
            self.tools.write_file(".git/config", "no")

    def test_initial_context_contains_tree(self):
        self.assertIn("src/app.py", self.tools.initial_context())

    def test_function_wrappers_emit_tool_timing_events(self):
        events = []
        self.tools.on_event = events.append
        read_file = next(
            function
            for function in self.tools.functions()
            if function.__name__ == "read_file"
        )
        result = read_file("src/app.py")

        self.assertIn("hello", result)
        self.assertEqual(events[0]["type"], "tool_call")
        self.assertEqual(events[0]["name"], "read_file")
        self.assertEqual(events[1]["type"], "tool_result")
        self.assertEqual(events[1]["id"], events[0]["id"])
        self.assertIn("duration_ms", events[1])

if __name__ == "__main__":
    unittest.main()
