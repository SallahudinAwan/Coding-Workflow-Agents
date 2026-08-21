import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from code_agent.repo_tools import RepositoryTools
from code_agent.ui import AgentSession, AgentUIHandler, PendingPublish


class FakeUIProvider:
    model = "ui-test-model"
    chat_model = object()

    def __init__(self):
        self.on_event = None
        self.requests = []

    def solve(self, request, repository_context, tools):
        self.requests.append(request)
        self.on_event({"type": "thinking", "text": "Inspecting the repository"})
        list_files = next(tool for tool in tools if tool.__name__ == "list_files")
        list_files("*")
        return "Task complete"


class FakeApprovalGraph:
    def invoke(self, _command, config):
        self.config = config
        return {"result": "Published approved branch and draft PR"}


class FakeApprovalWorkflow:
    def __init__(self):
        self.graph = FakeApprovalGraph()
        self.on_event = None
        self.publisher = type("Publisher", (), {"on_event": None})()


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
        self.assertIn("Developer Agent", html)
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
        self.assertIn("route", event_types)
        self.assertEqual(event_types[-1], "done")
        self.assertTrue(all("elapsed_ms" in event for event in events))

    def test_approval_endpoint_resumes_pending_github_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = RepositoryTools(directory)
            workflow = FakeApprovalWorkflow()
            self.server.sessions["approval-session"] = AgentSession(
                tools=tools,
                providers=[FakeUIProvider()],
                repository_context=tools.initial_context(),
                pending_publish=PendingPublish(
                    approval_id="approval-1",
                    workflow=workflow,
                    config={"configurable": {"thread_id": "thread-1"}},
                ),
            )
            request = urllib.request.Request(
                f"{self.base_url}/api/approval",
                data=json.dumps(
                    {
                        "session_id": "approval-session",
                        "approval_id": "approval-1",
                        "decision": {
                            "approve_branch": True,
                            "approve_stage": True,
                            "approve_commit": True,
                            "approve_push": True,
                            "approve_pr": True,
                        },
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                events = [
                    json.loads(line)
                    for line in response.read().decode("utf-8").splitlines()
                ]

        self.assertEqual(events[-1]["type"], "done")
        self.assertTrue(
            any(
                event.get("model") == "GitHub Agent" and "draft PR" in event["text"]
                for event in events
            )
        )
        self.assertIsNone(self.server.sessions["approval-session"].pending_publish)

    def test_publish_request_routes_only_to_github_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = FakeUIProvider()
            tools = RepositoryTools(directory)
            self.server.sessions["route-session"] = AgentSession(
                tools=tools,
                providers=[provider],
                repository_context=tools.initial_context(),
            )
            request = urllib.request.Request(
                f"{self.base_url}/api/chat",
                data=json.dumps(
                    {"session_id": "route-session", "message": "Push these changes to GitHub"}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with patch.object(AgentUIHandler, "_prepare_publish") as prepare:
                with urllib.request.urlopen(request) as response:
                    events = [
                        json.loads(line)
                        for line in response.read().decode("utf-8").splitlines()
                    ]

        self.assertEqual(provider.requests, [])
        prepare.assert_called_once()
        self.assertTrue(any(event["type"] == "route" for event in events))

    def test_unsupported_github_request_is_refused_without_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = FakeUIProvider()
            tools = RepositoryTools(directory)
            self.server.sessions["restricted-session"] = AgentSession(
                tools=tools,
                providers=[provider],
                repository_context=tools.initial_context(),
            )
            request = urllib.request.Request(
                f"{self.base_url}/api/chat",
                data=json.dumps(
                    {"session_id": "restricted-session", "message": "Merge pull request 42"}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                events = [
                    json.loads(line)
                    for line in response.read().decode("utf-8").splitlines()
                ]

        self.assertEqual(provider.requests, [])
        answer = next(event for event in events if event["type"] == "answer")
        self.assertIn("restricted", answer["text"])


if __name__ == "__main__":
    unittest.main()
