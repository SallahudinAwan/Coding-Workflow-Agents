import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from code_agent.repo_tools import RepositoryTools
from code_agent.ui import AgentSession, AgentUIHandler


class FakeUIProvider:
    model = "ui-test-model"

    def __init__(self):
        self.on_event = None

    def solve(self, request, repository_context, tools):
        self.on_event({"type": "thinking", "text": "Inspecting the repository"})
        list_files = next(tool for tool in tools if tool.__name__ == "list_files")
        list_files("*")
        return "Task complete"


class AgentUITests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AgentUIHandler)
        self.server.sessions = {}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_serves_interface_and_creates_repository_session(self):
        with urllib.request.urlopen(self.base_url) as response:
            html = response.read().decode("utf-8")
        self.assertIn("Code Agent", html)
        self.assertIn("Show model thinking", html)

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "README.md").write_text("Test repository", encoding="utf-8")
            request = urllib.request.Request(
                f"{self.base_url}/api/session",
                data=json.dumps(
                    {
                        "repository": directory,
                        "provider": "ollama",
                        "local_model": "qwen-test",
                        "show_thinking": True,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                session = json.loads(response.read().decode("utf-8"))

        self.assertIn("session_id", session)
        self.assertEqual(session["models"], ["qwen-test"])

    def test_chat_stream_contains_thinking_tools_answer_and_time(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "README.md").write_text("Test repository", encoding="utf-8")
            tools = RepositoryTools(directory)
            self.server.sessions["stream-test"] = AgentSession(
                tools=tools,
                providers=[FakeUIProvider()],
                repository_context=tools.initial_context(),
            )
            request = urllib.request.Request(
                f"{self.base_url}/api/chat",
                data=json.dumps(
                    {"session_id": "stream-test", "message": "inspect it"}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                events = [
                    json.loads(line)
                    for line in response.read().decode("utf-8").splitlines()
                ]

        event_types = [event["type"] for event in events]
        self.assertIn("thinking", event_types)
        self.assertIn("tool_call", event_types)
        self.assertIn("tool_result", event_types)
        self.assertIn("answer", event_types)
        self.assertEqual(event_types[-1], "done")
        self.assertTrue(all("elapsed_ms" in event for event in events))


if __name__ == "__main__":
    unittest.main()
