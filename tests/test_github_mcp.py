import asyncio
import unittest
from unittest.mock import AsyncMock

from code_agent.github_mcp import GitHubMCPClient


class FakeMCPTool:
    def __init__(self, name, result):
        self.name = name
        self.ainvoke = AsyncMock(return_value=result)


class FakeMCPAdapter:
    def __init__(self, tools):
        self.tools = tools

    async def get_tools(self):
        return self.tools


class GitHubMCPClientTests(unittest.TestCase):
    def test_connection_requests_only_pr_lookup_and_creation_explicitly(self):
        client = GitHubMCPClient(token="secret")
        connection = client._connection()["github"]

        self.assertEqual(connection["transport"], "streamable_http")
        self.assertEqual(
            connection["headers"]["X-MCP-Tools"],
            "list_pull_requests,create_pull_request",
        )
        self.assertEqual(connection["headers"]["X-MCP-Toolsets"], "context")
        self.assertNotIn("X-MCP-Readonly", connection["headers"])

    def test_creates_one_draft_pr_with_exact_approved_fields(self):
        list_tool = FakeMCPTool("list_pull_requests", [])
        create_tool = FakeMCPTool(
            "create_pull_request", {"url": "https://github.com/octo/demo/pull/1"}
        )
        client = GitHubMCPClient(token="secret")
        client._client = lambda _connections: FakeMCPAdapter([list_tool, create_tool])
        proposal = {
            "owner": "octo",
            "repo": "demo",
            "branch": "agent/change",
            "base_branch": "main",
            "pr_title": "Fix validation",
            "pr_body": "## Summary\n\nFix it.",
        }

        result = asyncio.run(client.create_pull_request(proposal))

        list_tool.ainvoke.assert_awaited_once_with(
            {
                "owner": "octo",
                "repo": "demo",
                "head": "octo:agent/change",
                "base": "main",
                "state": "open",
                "perPage": 1,
            }
        )
        create_tool.ainvoke.assert_awaited_once_with(
            {
                "owner": "octo",
                "repo": "demo",
                "title": "Fix validation",
                "body": "## Summary\n\nFix it.",
                "head": "agent/change",
                "base": "main",
                "draft": True,
            }
        )
        self.assertIn("pull/1", result)

    def test_reuses_existing_open_pr_instead_of_creating_another(self):
        existing = [{"number": 7, "html_url": "https://github.com/octo/demo/pull/7"}]
        list_tool = FakeMCPTool("list_pull_requests", existing)
        create_tool = FakeMCPTool("create_pull_request", {})
        client = GitHubMCPClient(token="secret")
        client._client = lambda _connections: FakeMCPAdapter([list_tool, create_tool])
        proposal = {
            "owner": "octo",
            "repo": "demo",
            "branch": "agent/change",
            "base_branch": "main",
            "pr_title": "Fix validation",
            "pr_body": "Body",
        }

        result = asyncio.run(client.create_pull_request(proposal))

        create_tool.ainvoke.assert_not_awaited()
        self.assertIn("existing", result.casefold())
        self.assertIn("pull/7", result)

    def test_empty_text_content_block_is_not_mistaken_for_an_existing_pr(self):
        self.assertFalse(
            GitHubMCPClient._contains_pull_request(
                [{"type": "text", "text": "[]"}]
            )
        )
        self.assertTrue(
            GitHubMCPClient._contains_pull_request(
                [
                    {
                        "type": "text",
                        "text": '[{"number": 7, "html_url": "https://github.com/o/r/pull/7"}]',
                    }
                ]
            )
        )


if __name__ == "__main__":
    unittest.main()
