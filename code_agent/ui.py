from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from langgraph.types import Command

from code_agent.config import load_project_environment
from code_agent.developer_agent import DeveloperIntent, classify_developer_request
from code_agent.github_mcp import GitHubMCPClient
from code_agent.github_workflow import (
    GitHubPublishWorkflow,
    GitPublisher,
    PullRequestDraftGenerator,
    default_branch_name,
    default_commit_message,
)
from code_agent.provider_factory import build_providers
from code_agent.repo_tools import RepositoryTools


ASSET_DIRECTORY = Path(__file__).parent / "web"
load_project_environment()


@dataclass
class PendingPublish:
    approval_id: str
    workflow: GitHubPublishWorkflow
    config: dict


@dataclass
class AgentSession:
    tools: RepositoryTools
    providers: list
    repository_context: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_code_request: str = ""
    last_provider: object | None = None
    pending_publish: PendingPublish | None = None


class AgentUIHandler(BaseHTTPRequestHandler):
    server_version = "DeveloperAgent/0.1"

    def log_message(self, format: str, *args) -> None:
        return

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("Request is too large")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self._send_json(200, {"ok": True})
            return
        assets = {"/": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
        filename = assets.get(self.path)
        if not filename:
            self.send_error(404)
            return
        path = ASSET_DIRECTORY / filename
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        try:
            if self.path == "/api/session":
                self._create_session()
            elif self.path == "/api/chat":
                self._chat()
            elif self.path == "/api/approval":
                self._approval()
            else:
                self.send_error(404)
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})

    def _create_session(self) -> None:
        data = self._json_body()
        repository = str(data.get("repository", "")).strip()
        if not repository:
            raise ValueError("A repository path is required")

        tools = RepositoryTools(repository)
        providers = build_providers(
            provider=str(data.get("provider", "auto")),
            gemini_model=data.get("gemini_model") or None,
            groq_model=data.get("groq_model") or None,
            local_model=data.get("local_model") or None,
            show_thinking=bool(data.get("show_thinking", True)),
        )
        session = AgentSession(
            tools=tools,
            providers=providers,
            repository_context=tools.initial_context(),
        )
        session_id = uuid.uuid4().hex
        self.server.sessions[session_id] = session  # type: ignore[attr-defined]
        self._send_json(
            201,
            {
                "session_id": session_id,
                "repository": str(tools.root),
                "models": [provider.model for provider in providers],
            },
        )

    def _session(self, data: dict) -> AgentSession | None:
        session_id = str(data.get("session_id", ""))
        return self.server.sessions.get(session_id)  # type: ignore[attr-defined]

    def _start_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        started = time.perf_counter()
        connected = True

        def emit(event: dict) -> None:
            nonlocal connected
            if not connected:
                return
            event.setdefault("elapsed_ms", round((time.perf_counter() - started) * 1000))
            try:
                self.wfile.write((json.dumps(event) + "\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                connected = False

        return started, emit

    @staticmethod
    def _run_code_agent(session: AgentSession, message: str, emit) -> bool:
        for index, provider in enumerate(session.providers):
            if index > 0:
                session.repository_context = session.tools.initial_context()
            emit(
                {
                    "type": "status",
                    "text": f"Code Agent is working with {provider.model}",
                    "model": provider.model,
                }
            )
            try:
                result = provider.solve(
                    request=message,
                    repository_context=session.repository_context,
                    tools=session.tools.functions(),
                )
                emit(
                    {
                        "type": "answer",
                        "text": result,
                        "model": f"Code Agent · {provider.model}",
                    }
                )
                session.last_code_request = message
                session.last_provider = provider
                return True
            except Exception as exc:
                has_fallback = index < len(session.providers) - 1
                emit(
                    {
                        "type": "status" if has_fallback else "error",
                        "text": str(exc),
                    }
                )
        return False

    @staticmethod
    def _prepare_publish(session: AgentSession, request: str, emit) -> None:
        if session.pending_publish:
            raise ValueError("A GitHub approval is already pending")
        provider = session.last_provider or session.providers[0]
        publisher = GitPublisher(
            session.tools.root,
            on_event=lambda text: emit({"type": "status", "text": text}),
        )
        workflow = GitHubPublishWorkflow(
            publisher=publisher,
            draft_generator=PullRequestDraftGenerator(provider.chat_model),
            github_mcp=GitHubMCPClient(),
            branch=default_branch_name(),
            message=default_commit_message(request),
            on_event=lambda text: emit({"type": "status", "text": text}),
        )
        config = {"configurable": {"thread_id": uuid.uuid4().hex}}
        result = workflow.graph.invoke({"request": request}, config=config)
        interruptions = result.get("__interrupt__", [])
        if not interruptions:
            raise RuntimeError("GitHub workflow did not reach its approval boundary")
        approval_id = uuid.uuid4().hex
        session.pending_publish = PendingPublish(
            approval_id=approval_id,
            workflow=workflow,
            config=config,
        )
        emit(
            {
                "type": "approval_required",
                "approval_id": approval_id,
                **interruptions[0].value,
            }
        )

    def _chat(self) -> None:
        data = self._json_body()
        message = str(data.get("message", "")).strip()
        session = self._session(data)
        if session is None:
            self._send_json(404, {"error": "Session not found; reconnect the repository"})
            return
        if not message:
            self._send_json(400, {"error": "A message is required"})
            return
        if not session.lock.acquire(blocking=False):
            self._send_json(409, {"error": "The agent is already working"})
            return

        started, emit = self._start_stream()

        session.tools.on_event = emit
        for provider in session.providers:
            if hasattr(provider, "on_event"):
                provider.on_event = emit

        try:
            intent = classify_developer_request(message)
            route = (
                "Code Agent → GitHub Agent"
                if intent == DeveloperIntent.CODE_AND_GITHUB
                else "GitHub Agent"
                if intent in {DeveloperIntent.GITHUB, DeveloperIntent.GITHUB_UNSUPPORTED}
                else "Code Agent"
            )
            emit({"type": "route", "text": f"Developer Agent routed this to {route}"})
            if intent == DeveloperIntent.GITHUB_UNSUPPORTED:
                emit(
                    {
                        "type": "answer",
                        "model": "GitHub Agent",
                        "text": (
                            "This GitHub Agent is restricted to creating a new branch, "
                            "staging approved files, committing locally, pushing that "
                            "branch, and creating one approved draft PR. It cannot perform "
                            "the requested GitHub operation."
                        ),
                    }
                )
                return
            code_completed = True
            if intent != DeveloperIntent.GITHUB:
                code_completed = self._run_code_agent(session, message, emit)
            if code_completed and intent != DeveloperIntent.CODE:
                publish_request = session.last_code_request or message
                self._prepare_publish(session, publish_request, emit)
            elif not code_completed:
                emit({"type": "error", "text": "No Code Agent provider completed the request"})
            session.repository_context = session.tools.initial_context()
        except Exception as exc:
            emit({"type": "error", "text": str(exc)})
        finally:
            emit({"type": "done", "elapsed_ms": round((time.perf_counter() - started) * 1000)})
            session.tools.on_event = None
            session.lock.release()

    def _approval(self) -> None:
        data = self._json_body()
        session = self._session(data)
        if session is None:
            self._send_json(404, {"error": "Session not found; reconnect the repository"})
            return
        pending = session.pending_publish
        if pending is None or str(data.get("approval_id", "")) != pending.approval_id:
            self._send_json(409, {"error": "Approval is missing, stale, or already handled"})
            return
        if not session.lock.acquire(blocking=False):
            self._send_json(409, {"error": "The Developer Agent is already working"})
            return

        started, emit = self._start_stream()
        pending.workflow.on_event = lambda text: emit({"type": "status", "text": text})
        pending.workflow.publisher.on_event = lambda text: emit(
            {"type": "status", "text": text}
        )
        try:
            decision = dict(data.get("decision") or {})
            final = pending.workflow.graph.invoke(
                Command(resume=decision), config=pending.config
            )
            emit({"type": "answer", "text": final["result"], "model": "GitHub Agent"})
            session.repository_context = session.tools.initial_context()
        except Exception as exc:
            emit({"type": "error", "text": str(exc)})
        finally:
            session.pending_publish = None
            emit(
                {
                    "type": "done",
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                }
            )
            session.lock.release()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Developer Agent web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    server = ThreadingHTTPServer((args.host, args.port), AgentUIHandler)
    server.sessions = {}  # type: ignore[attr-defined]
    url = f"http://{args.host}:{args.port}"
    print(f"Developer Agent UI running at {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping UI.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
