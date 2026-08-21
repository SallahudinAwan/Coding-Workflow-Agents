from __future__ import annotations

import re
from enum import Enum


class DeveloperIntent(str, Enum):
    CODE = "code"
    GITHUB = "github"
    CODE_AND_GITHUB = "code_and_github"
    GITHUB_UNSUPPORTED = "github_unsupported"


_GITHUB_OPERATION = re.compile(
    r"(?:\b(?:push|publish|commit)\b.*\b(?:changes?|code|git|github|remote)\b|"
    r"\b(?:create|open|make)\b.*\b(?:pull request|pr|branch)\b|"
    r"\b(?:pull request|github pr)\b)",
    re.IGNORECASE,
)
_CODE_CHANGE = re.compile(
    r"\b(?:add|build|change|create|delete|fix|implement|modify|refactor|remove|"
    r"rename|replace|update|write)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_GITHUB_OPERATION = re.compile(
    r"\b(?:close|comment|configure|delete|edit|fork|label|merge|release|reopen|"
    r"review|tag|trigger)\b.*\b(?:action|issue|pr|pull request|release|repo|"
    r"repository|setting|tag|workflow)\b",
    re.IGNORECASE,
)


def classify_developer_request(request: str) -> DeveloperIntent:
    """Route an implementation request without giving either agent extra powers."""
    if _UNSUPPORTED_GITHUB_OPERATION.search(request):
        return DeveloperIntent.GITHUB_UNSUPPORTED
    github_operation = bool(_GITHUB_OPERATION.search(request))
    code_only_text = _GITHUB_OPERATION.sub("", request)
    code_change = bool(_CODE_CHANGE.search(code_only_text))
    if github_operation and code_change:
        return DeveloperIntent.CODE_AND_GITHUB
    if github_operation:
        return DeveloperIntent.GITHUB
    return DeveloperIntent.CODE
