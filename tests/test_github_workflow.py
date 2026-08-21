import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from langchain_core.messages import AIMessage
from langgraph.types import Command

from code_agent.github_workflow import (
    GitHubPublishWorkflow,
    GitPublisher,
    PullRequestDraftGenerator,
)


def run_git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


class FakePublisher:
    def __init__(self):
        self.calls = []

    def prepare(self, *, branch, message):
        self.calls.append("prepare")
        return {
            "owner": "octo",
            "repo": "demo",
            "repository": "octo/demo",
            "remote_url": "https://github.com/octo/demo.git",
            "remote": "origin",
            "base_branch": "main",
            "branch": branch,
            "message": message,
            "change_groups": [
                {"id": "change-1", "status": "M", "label": "app.py", "paths": ["app.py"]}
            ],
            "diff": "-old\n+new",
            "fingerprint": "one",
        }

    def validate_approval(self, _proposal, selected):
        self.calls.append("validate")
        return ["app.py"] if selected == ["change-1"] else []

    def create_branch(self, _proposal):
        self.calls.append("branch")

    def stage(self, _proposal, _paths):
        self.calls.append("stage")

    def commit(self, _proposal):
        self.calls.append("commit")
        return "abcdef1234567890"

    def push(self, _proposal):
        self.calls.append("push")


class FakeMCP:
    def __init__(self):
        self.proposals = []

    async def create_pull_request(self, proposal):
        self.proposals.append(proposal)
        return "https://github.com/octo/demo/pull/1"


class GitPublisherTests(unittest.TestCase):
    def make_repository(self, directory: str) -> Path:
        root = Path(directory)
        run_git(root, "init")
        run_git(root, "config", "user.email", "agent@example.com")
        run_git(root, "config", "user.name", "Agent Test")
        Path(root, "app.py").write_bytes(b"old\n")
        run_git(root, "add", "--", "app.py")
        run_git(root, "commit", "-m", "initial")
        run_git(root, "remote", "add", "origin", "https://github.com/octo/demo.git")
        return root

    def test_prepares_exact_groups_and_can_stage_and_commit_selected_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repository(directory)
            Path(root, "app.py").write_bytes(b"new\n")
            Path(root, "extra.txt").write_bytes(b"extra\n")
            publisher = GitPublisher(root)
            proposal = publisher.prepare(branch="agent/change", message="Apply change")

            labels = [group["label"] for group in proposal["change_groups"]]
            self.assertEqual(labels, ["app.py", "extra.txt"])
            selected_id = next(
                group["id"] for group in proposal["change_groups"] if group["label"] == "app.py"
            )
            selected = publisher.validate_approval(proposal, [selected_id])
            run_git(root, "switch", "-c", "agent/change")
            proposal["selected_paths"] = selected
            publisher.stage(proposal, selected)
            sha = publisher.commit(proposal)

            committed = run_git(root, "show", "--pretty=", "--name-only", sha).splitlines()
            self.assertEqual(committed, ["app.py"])
            self.assertTrue(Path(root, "extra.txt").is_file())

    def test_refuses_a_pre_staged_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repository(directory)
            Path(root, "app.py").write_bytes(b"new\n")
            run_git(root, "add", "--", "app.py")
            with self.assertRaisesRegex(ValueError, "Pre-staged"):
                GitPublisher(root).prepare(branch="agent/change", message="Apply change")


class PullRequestDraftTests(unittest.TestCase):
    def test_llm_receives_diff_and_returns_pr_text(self):
        model = Mock()
        model.invoke.return_value = AIMessage(
            content='{"title":"Fix validation","body":"## Summary\\n\\nFixed.\\n\\n## Testing\\n\\nUnit tests."}'
        )
        proposal = {"message": "Fix", "diff": "+fixed"}

        draft = PullRequestDraftGenerator(model).generate("Fix validation", proposal)

        self.assertEqual(draft["pr_title"], "Fix validation")
        self.assertIn("## Testing", draft["pr_body"])
        self.assertIn("+fixed", model.invoke.call_args.args[0][1]["content"])


class GitHubPublishGraphTests(unittest.TestCase):
    def make_workflow(self):
        publisher = FakePublisher()
        draft = Mock()
        draft.generate.return_value = {"pr_title": "Fix it", "pr_body": "## Summary\n\nDone"}
        mcp = FakeMCP()
        workflow = GitHubPublishWorkflow(
            publisher=publisher,
            draft_generator=draft,
            github_mcp=mcp,
            branch="agent/change",
            message="Apply change",
        )
        return workflow, publisher, mcp

    def test_no_write_occurs_before_approval_and_rejection_is_clean(self):
        workflow, publisher, mcp = self.make_workflow()
        config = {"configurable": {"thread_id": "reject"}}
        paused = workflow.graph.invoke({"request": "Fix it"}, config=config)

        self.assertEqual(paused["__interrupt__"][0].value["kind"], "github_publish_approval")
        self.assertEqual(publisher.calls, ["prepare"])
        rejected = workflow.graph.invoke(
            Command(
                resume={
                    "approve_branch": False,
                    "approve_stage": False,
                    "approve_commit": False,
                    "approve_push": False,
                    "approve_pr": False,
                }
            ),
            config=config,
        )
        self.assertIn("cancelled", rejected["result"])
        self.assertEqual(publisher.calls, ["prepare"])
        self.assertEqual(mcp.proposals, [])

    def test_all_explicit_approvals_run_only_the_restricted_sequence(self):
        workflow, publisher, mcp = self.make_workflow()
        config = {"configurable": {"thread_id": "approve"}}
        workflow.graph.invoke({"request": "Fix it"}, config=config)
        final = workflow.graph.invoke(
            Command(
                resume={
                    "selected_group_ids": ["change-1"],
                    "approve_branch": True,
                    "approve_stage": True,
                    "approve_commit": True,
                    "approve_push": True,
                    "approve_pr": True,
                }
            ),
            config=config,
        )

        self.assertEqual(
            publisher.calls,
            ["prepare", "validate", "branch", "stage", "commit", "push"],
        )
        self.assertEqual(len(mcp.proposals), 1)
        self.assertIn("draft pull request", final["result"])


if __name__ == "__main__":
    unittest.main()
