from __future__ import annotations

import argparse
import uuid
from pathlib import Path

from langgraph.types import Command

from code_agent.config import load_project_environment
from code_agent.github_mcp import GitHubMCPClient
from code_agent.github_workflow import (
    GitHubPublishWorkflow,
    GitPublisher,
    PullRequestDraftGenerator,
    default_branch_name,
    default_commit_message,
    format_approval,
)
from code_agent.provider_factory import build_providers


load_project_environment()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Approve a local Git branch, commit, push, and draft GitHub PR"
    )
    parser.add_argument("--repo", required=True, help="Local GitHub repository")
    parser.add_argument("--request", required=True, help="Original development request")
    parser.add_argument("--branch", help="New feature branch")
    parser.add_argument("--message", help="Local commit message")
    parser.add_argument(
        "--provider", choices=("gemini", "groq", "ollama"), default="gemini"
    )
    parser.add_argument("--model", help="Gemini model override")
    parser.add_argument("--groq-model", help="Groq model override")
    parser.add_argument("--local-model", help="Ollama model override")
    return parser.parse_args()


def _yes(prompt: str) -> bool:
    return input(f"{prompt} Type 'yes' to approve: ").strip().casefold() == "yes"


def main() -> None:
    args = _arguments()
    publisher = GitPublisher(Path(args.repo))
    provider = build_providers(
        provider=args.provider,
        gemini_model=args.model,
        groq_model=args.groq_model,
        local_model=args.local_model,
        show_thinking=False,
    )[0]
    workflow = GitHubPublishWorkflow(
        publisher=publisher,
        draft_generator=PullRequestDraftGenerator(provider.chat_model),
        github_mcp=GitHubMCPClient(),
        branch=args.branch or default_branch_name(),
        message=args.message or default_commit_message(args.request),
        on_event=print,
    )
    config = {"configurable": {"thread_id": uuid.uuid4().hex}}
    result = workflow.graph.invoke({"request": args.request}, config=config)
    interruption = result.get("__interrupt__", [])[0].value
    print(format_approval(interruption))
    selected = [group["id"] for group in interruption["change_groups"]]
    decision = {
        "selected_group_ids": selected,
        "approve_branch": _yes("Approve creating the new local branch?"),
        "approve_stage": _yes("Approve staging this exact file set?"),
        "approve_commit": _yes("Approve creating the local commit?"),
        "approve_push": _yes("Approve pushing the new branch to origin?"),
        "approve_pr": _yes("Approve creating this draft PR through GitHub MCP?"),
    }
    final = workflow.graph.invoke(Command(resume=decision), config=config)
    print(final["result"])


if __name__ == "__main__":
    main()
