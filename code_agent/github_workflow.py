from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from code_agent.github_mcp import GitHubMCPClient


MAX_DIFF_CHARACTERS = 120_000
MAX_PR_DIFF_CHARACTERS = 40_000


class GitPublishState(TypedDict, total=False):
    request: str
    proposal: dict
    approval: dict
    result: str


class GitPublisher:
    """Restricted local Git capability: branch, stage, commit, and push only."""

    def __init__(
        self,
        root: str | Path,
        on_event: Callable[[str], None] | None = None,
    ):
        self.root = Path(root).expanduser().resolve()
        self.on_event = on_event
        if not self.root.is_dir():
            raise ValueError(f"Repository does not exist: {self.root}")
        if self._git("rev-parse", "--is-inside-work-tree").strip() != "true":
            raise ValueError(f"Not a Git repository: {self.root}")

    def _emit(self, message: str) -> None:
        if self.on_event:
            self.on_event(message)

    def _run(self, arguments: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )

    def _git(self, *arguments: str, timeout: int = 30) -> str:
        result = self._run(list(arguments), timeout=timeout)
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(detail or f"git {' '.join(arguments)} failed")
        return result.stdout

    def current_branch(self) -> str:
        branch = self._git("branch", "--show-current").strip()
        if not branch:
            raise ValueError("Detached HEAD is not supported")
        return branch

    def github_repository(self, remote: str = "origin") -> tuple[str, str, str]:
        remote_url = self._git("remote", "get-url", remote).strip()
        if remote_url.startswith("git@github.com:"):
            path = remote_url.split(":", 1)[1]
        else:
            parsed = urlparse(remote_url)
            if parsed.hostname not in {"github.com", "www.github.com"}:
                raise ValueError("The origin remote must point to github.com")
            path = parsed.path.lstrip("/")
        path = path[:-4] if path.endswith(".git") else path
        parts = path.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError(f"Cannot identify owner/repository from origin: {remote_url}")
        return parts[0], parts[1], remote_url

    def _change_groups(self) -> list[dict]:
        raw = self._git("diff", "--name-status", "-z", "--find-renames", "HEAD", "--")
        parts = raw.split("\0")
        groups = []
        index = 0
        while index < len(parts) and parts[index]:
            status = parts[index]
            index += 1
            if status.startswith(("R", "C")):
                paths = [parts[index], parts[index + 1]]
                index += 2
                label = f"{paths[0]} -> {paths[1]}"
            else:
                paths = [parts[index]]
                index += 1
                label = paths[0]
            groups.append(
                {
                    "id": f"tracked-{len(groups)}",
                    "status": status,
                    "label": label,
                    "paths": paths,
                }
            )
        tracked_paths = {path for group in groups for path in group["paths"]}
        untracked = self._git("ls-files", "--others", "--exclude-standard").splitlines()
        for path in untracked:
            if path in tracked_paths:
                continue
            groups.append(
                {
                    "id": f"untracked-{len(groups)}",
                    "status": "??",
                    "label": path,
                    "paths": [path],
                }
            )
        return groups

    def _fingerprint(self, groups: list[dict]) -> str:
        digest = hashlib.sha256()
        digest.update(self._git("diff", "--binary", "HEAD", "--").encode("utf-8"))
        for path in sorted({item for group in groups for item in group["paths"]}):
            candidate = self.root / path
            digest.update(path.encode("utf-8"))
            if candidate.is_file():
                digest.update(candidate.read_bytes())
            else:
                digest.update(b"<deleted>")
        return digest.hexdigest()

    def _diff_preview(self, groups: list[dict]) -> str:
        preview = self._git("diff", "--no-ext-diff", "--no-color", "HEAD", "--")
        tracked = {path for group in groups if group["status"] != "??" for path in group["paths"]}
        for group in groups:
            if group["status"] != "??":
                continue
            path = group["paths"][0]
            data = (self.root / path).read_bytes()
            if b"\x00" in data[:8192]:
                preview += f"\nBinary untracked file: {path}\n"
                continue
            text = data.decode("utf-8", errors="replace")
            preview += "".join(
                difflib.unified_diff(
                    [],
                    text.splitlines(keepends=True),
                    fromfile="/dev/null",
                    tofile=f"b/{path}",
                )
            )
        if tracked and not preview:
            preview = "Tracked changes are present (diff preview unavailable)."
        if len(preview) > MAX_DIFF_CHARACTERS:
            preview = preview[:MAX_DIFF_CHARACTERS] + "\n... diff truncated"
        return preview

    def prepare(self, *, branch: str, message: str) -> dict:
        staged = self._git("diff", "--cached", "--name-only").splitlines()
        if staged:
            raise ValueError(
                "Pre-staged changes are not supported. Unstage them so the Developer "
                "Agent can ask approval for the exact staging scope."
            )
        groups = self._change_groups()
        if not groups:
            raise ValueError("There are no local changes to publish")
        base_branch = self.current_branch()
        if branch == base_branch:
            raise ValueError("The GitHub Agent must create a new branch")
        if not message.strip():
            raise ValueError("A commit message is required")
        self._validate_branch_name(branch)
        owner, repo, remote_url = self.github_repository()
        return {
            "owner": owner,
            "repo": repo,
            "repository": f"{owner}/{repo}",
            "remote": "origin",
            "remote_url": remote_url,
            "base_branch": base_branch,
            "branch": branch,
            "message": message.strip(),
            "change_groups": groups,
            "file_paths": sorted({path for group in groups for path in group["paths"]}),
            "diff": self._diff_preview(groups),
            "fingerprint": self._fingerprint(groups),
        }

    def _validate_branch_name(self, branch: str) -> None:
        if not branch or branch.startswith("-"):
            raise ValueError("A valid new branch name is required")
        result = self._run(["check-ref-format", "--branch", branch])
        if result.returncode:
            raise ValueError(f"Invalid branch name: {branch}")

    def validate_approval(self, proposal: dict, selected_group_ids: list[str]) -> list[str]:
        if self.current_branch() != proposal["base_branch"]:
            raise ValueError("The current branch changed after review; request a fresh approval")
        if self._git("diff", "--cached", "--name-only").splitlines():
            raise ValueError("The staging area changed after review; request a fresh approval")
        current_groups = self._change_groups()
        if self._fingerprint(current_groups) != proposal["fingerprint"]:
            raise ValueError("Local changes changed after review; request a fresh approval")
        known = {group["id"]: group for group in proposal["change_groups"]}
        if not selected_group_ids or not set(selected_group_ids).issubset(known):
            raise ValueError("Select at least one reviewed change group")
        self._validate_branch_name(proposal["branch"])
        if proposal["branch"] == proposal["base_branch"]:
            raise ValueError("The GitHub Agent must create a new branch")
        if not proposal["message"]:
            raise ValueError("A commit message is required")
        return sorted(
            {
                path
                for group_id in selected_group_ids
                for path in known[group_id]["paths"]
            }
        )

    def create_branch(self, proposal: dict) -> None:
        branch = proposal["branch"]
        self._validate_branch_name(branch)
        local = self._run(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
        if local.returncode == 0:
            raise ValueError(f"Local branch already exists: {branch}")
        remote = self._git(
            "ls-remote",
            "--heads",
            proposal["remote"],
            f"refs/heads/{branch}",
            timeout=60,
        )
        if remote.strip():
            raise ValueError(f"Remote branch already exists: {branch}")
        self._emit(f"Creating local branch {branch}")
        self._git("switch", "-c", branch)

    def stage(self, proposal: dict, selected_paths: list[str]) -> None:
        self._emit("Staging the explicitly approved files")
        self._git("add", "--", *selected_paths)
        staged = self._git("diff", "--cached", "--name-only").splitlines()
        unexpected = set(staged).difference(selected_paths)
        if unexpected:
            raise RuntimeError(
                "Refusing to commit unapproved staged files: " + ", ".join(sorted(unexpected))
            )
        if not staged:
            raise ValueError("The approved files produced no staged changes")

    def commit(self, proposal: dict) -> str:
        self._emit(f"Creating local commit: {proposal['message']}")
        self._git("commit", "-m", proposal["message"], timeout=120)
        sha = self._git("rev-parse", "HEAD").strip()
        committed = self._git(
            "diff-tree", "--no-commit-id", "--name-only", "-r", sha
        ).splitlines()
        unexpected = set(committed).difference(proposal["selected_paths"])
        if unexpected:
            raise RuntimeError(
                "Commit hooks added unapproved files; refusing to push: "
                + ", ".join(sorted(unexpected))
            )
        return sha

    def push(self, proposal: dict) -> None:
        self._emit(f"Pushing {proposal['branch']} to {proposal['remote']}")
        self._git(
            "push",
            "--set-upstream",
            proposal["remote"],
            proposal["branch"],
            timeout=120,
        )


class PullRequestDraftGenerator:
    """Use the selected LLM only to draft PR text from the local diff."""

    def __init__(self, chat_model):
        self.chat_model = chat_model

    @staticmethod
    def _content(response) -> str:
        content = getattr(response, "content", response)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("text")
            )
        return str(content or "")

    def generate(self, request: str, proposal: dict) -> dict:
        prompt = f"""Draft a GitHub pull request for the change below.
Return only JSON with two string fields: title and body.
The title must be concise. The Markdown body must contain Summary and Testing sections.
Do not invent tests or results that are not visible in the request or diff.

User request:
{request}

Commit message:
{proposal['message']}

Diff:
{proposal['diff'][:MAX_PR_DIFF_CHARACTERS]}
"""
        response = self.chat_model.invoke(
            [{"role": "system", "content": "You write accurate pull request descriptions."},
             {"role": "user", "content": prompt}]
        )
        text = self._content(response).strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        try:
            draft = json.loads(match.group(0) if match else text)
        except (json.JSONDecodeError, AttributeError):
            draft = {}
        title = str(draft.get("title") or proposal["message"]).strip()[:120]
        body = str(draft.get("body") or f"## Summary\n\n{request}\n\n## Testing\n\nNot specified.").strip()
        return {"pr_title": title, "pr_body": body}


class GitHubPublishWorkflow:
    """LangGraph approval boundary for Git writes and one MCP draft PR."""

    def __init__(
        self,
        *,
        publisher: GitPublisher,
        draft_generator: PullRequestDraftGenerator,
        github_mcp: GitHubMCPClient,
        branch: str,
        message: str,
        on_event: Callable[[str], None] | None = None,
    ):
        self.publisher = publisher
        self.draft_generator = draft_generator
        self.github_mcp = github_mcp
        self.branch = branch
        self.message = message
        self.on_event = on_event
        self.graph = self._build_graph()

    def _emit(self, message: str) -> None:
        if self.on_event:
            self.on_event(message)

    def _prepare(self, _state: GitPublishState) -> dict:
        return {"proposal": self.publisher.prepare(branch=self.branch, message=self.message)}

    def _draft(self, state: GitPublishState) -> dict:
        self._emit("Drafting the pull request title and body")
        draft = self.draft_generator.generate(state["request"], state["proposal"])
        return {"proposal": {**state["proposal"], **draft}}

    @staticmethod
    def _approve(state: GitPublishState) -> dict:
        proposal = state["proposal"]
        decision = interrupt(
            {
                "kind": "github_publish_approval",
                "repository": proposal["repository"],
                "remote_url": proposal["remote_url"],
                "base_branch": proposal["base_branch"],
                "branch": proposal["branch"],
                "commit_message": proposal["message"],
                "change_groups": proposal["change_groups"],
                "diff": proposal["diff"],
                "pr_title": proposal["pr_title"],
                "pr_body": proposal["pr_body"],
                "draft": True,
                "required_confirmations": [
                    "approve_branch",
                    "approve_stage",
                    "approve_commit",
                    "approve_push",
                    "approve_pr",
                ],
            }
        )
        if not isinstance(decision, dict):
            decision = {}
        required = (
            "approve_branch",
            "approve_stage",
            "approve_commit",
            "approve_push",
            "approve_pr",
        )
        approved = all(decision.get(name) is True for name in required)
        proposal = {
            **proposal,
            "branch": str(decision.get("branch") or proposal["branch"]).strip(),
            "message": str(decision.get("commit_message") or proposal["message"]).strip(),
            "pr_title": str(decision.get("pr_title") or proposal["pr_title"]).strip(),
            "pr_body": str(decision.get("pr_body") or proposal["pr_body"]).strip(),
            "selected_group_ids": list(decision.get("selected_group_ids") or []),
        }
        return {"approval": {**decision, "approved": approved}, "proposal": proposal}

    @staticmethod
    def _route(state: GitPublishState) -> str:
        return "branch" if state["approval"].get("approved") else "cancel"

    def _branch(self, state: GitPublishState) -> dict:
        proposal = state["proposal"]
        if not proposal["pr_title"] or not proposal["pr_body"]:
            raise ValueError("PR title and PR body are required")
        selected = self.publisher.validate_approval(
            proposal, proposal["selected_group_ids"]
        )
        proposal = {**proposal, "selected_paths": selected}
        self.publisher.create_branch(proposal)
        return {"proposal": proposal}

    def _stage(self, state: GitPublishState) -> dict:
        self.publisher.stage(state["proposal"], state["proposal"]["selected_paths"])
        return {}

    def _commit(self, state: GitPublishState) -> dict:
        sha = self.publisher.commit(state["proposal"])
        return {"proposal": {**state["proposal"], "commit_sha": sha}}

    def _push(self, state: GitPublishState) -> dict:
        self.publisher.push(state["proposal"])
        return {}

    def _create_pr(self, state: GitPublishState) -> dict:
        self._emit("Creating the approved draft pull request through GitHub MCP")
        result = asyncio.run(self.github_mcp.create_pull_request(state["proposal"]))
        return {
            "result": (
                f"Created branch {state['proposal']['branch']}, committed "
                f"{state['proposal']['commit_sha'][:12]}, pushed it, and created a "
                f"draft pull request.\n{result}"
            )
        }

    @staticmethod
    def _cancel(_state: GitPublishState) -> dict:
        return {"result": "Publishing cancelled; no Git operation was performed."}

    def _build_graph(self):
        builder = StateGraph(GitPublishState)
        builder.add_node("prepare", self._prepare)
        builder.add_node("draft", self._draft)
        builder.add_node("approve", self._approve)
        builder.add_node("branch", self._branch)
        builder.add_node("stage", self._stage)
        builder.add_node("commit", self._commit)
        builder.add_node("push", self._push)
        builder.add_node("create_pr", self._create_pr)
        builder.add_node("cancel", self._cancel)
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", "draft")
        builder.add_edge("draft", "approve")
        builder.add_conditional_edges(
            "approve", self._route, {"branch": "branch", "cancel": "cancel"}
        )
        builder.add_edge("branch", "stage")
        builder.add_edge("stage", "commit")
        builder.add_edge("commit", "push")
        builder.add_edge("push", "create_pr")
        builder.add_edge("create_pr", END)
        builder.add_edge("cancel", END)
        return builder.compile(checkpointer=InMemorySaver())


def default_branch_name() -> str:
    return f"agent/{time.strftime('%Y%m%d-%H%M%S')}"


def default_commit_message(request: str) -> str:
    summary = " ".join(request.strip().split())[:65]
    return f"Agent: {summary or 'publish approved changes'}"


def format_approval(approval: dict) -> str:
    files = "\n".join(
        f"  - [{group['status']}] {group['label']}" for group in approval["change_groups"]
    )
    return (
        f"\nDeveloper Agent publish approval\n"
        f"Repository: {approval['repository']}\n"
        f"Branch: {approval['base_branch']} -> {approval['branch']}\n"
        f"Commit: {approval['commit_message']}\n"
        f"Draft PR: {approval['pr_title']}\n"
        f"Files:\n{files}\n\n"
        f"PR body:\n{approval['pr_body']}\n\n"
        f"Diff:\n{approval['diff']}"
    )
