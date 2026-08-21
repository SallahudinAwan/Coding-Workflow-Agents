from __future__ import annotations

import json
import os


DEFAULT_GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"


class GitHubMCPClient:
    """GitHub MCP adapter restricted to creating an approved draft PR."""

    def __init__(self, token: str | None = None, url: str | None = None):
        self.token = token or os.environ.get(
            "GITHUB_PERSONAL_ACCESS_TOKEN"
        ) or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            raise ValueError(
                "GITHUB_PERSONAL_ACCESS_TOKEN (or GITHUB_TOKEN) is not set"
            )
        self.url = url or os.environ.get("GITHUB_MCP_URL", DEFAULT_GITHUB_MCP_URL)
    def _connection(self) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
        headers["X-MCP-Toolsets"] = "context"
        headers["X-MCP-Tools"] = "list_pull_requests,create_pull_request"
        return {
            "github": {
                "transport": "streamable_http",
                "url": self.url,
                "headers": headers,
            }
        }

    @staticmethod
    def _client(connections: dict):
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError as exc:
            raise RuntimeError("Install langchain-mcp-adapters") from exc
        return MultiServerMCPClient(connections)

    async def create_pull_request(self, proposal: dict) -> str:
        """Create one draft PR from the already pushed, explicitly approved branch."""
        client = self._client(self._connection())
        tools = await client.get_tools()
        by_name = {tool.name: tool for tool in tools}
        missing = {"list_pull_requests", "create_pull_request"}.difference(by_name)
        if missing:
            raise RuntimeError(
                "GitHub MCP did not expose required PR tools: "
                + ", ".join(sorted(missing))
            )
        existing = await by_name["list_pull_requests"].ainvoke(
            {
                "owner": proposal["owner"],
                "repo": proposal["repo"],
                "head": f"{proposal['owner']}:{proposal['branch']}",
                "base": proposal["base_branch"],
                "state": "open",
                "perPage": 1,
            }
        )
        if self._contains_pull_request(existing):
            return f"Reused existing open pull request: {existing}"
        result = await by_name["create_pull_request"].ainvoke(
            {
                "owner": proposal["owner"],
                "repo": proposal["repo"],
                "title": proposal["pr_title"],
                "body": proposal["pr_body"],
                "head": proposal["branch"],
                "base": proposal["base_branch"],
                "draft": True,
            }
        )
        return str(result)

    @classmethod
    def _contains_pull_request(cls, result) -> bool:
        if result is None:
            return False
        if isinstance(result, list):
            return any(cls._contains_pull_request(item) for item in result)
        if isinstance(result, dict):
            if result.get("type") == "text" and "text" in result:
                return cls._contains_pull_request(result["text"])
            if isinstance(result.get("total_count"), int):
                return result["total_count"] > 0
            if result.get("number") and (result.get("html_url") or result.get("url")):
                return True
            return any(
                cls._contains_pull_request(result.get(key))
                for key in ("items", "pull_requests", "data")
                if key in result
            )
        if isinstance(result, str):
            text = result.strip()
            if not text or text in {"[]", "{}"}:
                return False
            try:
                return cls._contains_pull_request(json.loads(text))
            except json.JSONDecodeError:
                lowered = text.casefold()
                if "no pull request" in lowered or "no results" in lowered:
                    return False
                return "html_url" in lowered and "number" in lowered
        return False
