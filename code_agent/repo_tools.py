from __future__ import annotations

import fnmatch
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path


IGNORED_PARTS = {
    ".git",
    ".idea",
    ".next",
    ".pytest_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "venv",
}

CONTEXT_FILES = {
    "AGENTS.md",
    "CONTRIBUTING.md",
    "Dockerfile",
    "Gemfile",
    "Makefile",
    "README.md",
    "README.rst",
    "composer.json",
    "go.mod",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "requirements.txt",
    "tsconfig.json",
}


class RepositoryTools:
    """Small, repository-scoped tools exposed to the LLM."""

    def __init__(self, root: str | Path, on_event: Callable[[dict], None] | None = None):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"Repository does not exist: {self.root}")
        self.on_event = on_event

    def _emit(self, event: dict) -> None:
        if self.on_event:
            self.on_event(event)

    def _invoke(self, name: str, arguments: dict, function: Callable) -> str:
        call_id = uuid.uuid4().hex[:10]
        started = time.perf_counter()
        displayed_arguments = {
            key: value[:2_000] + "\n… truncated" if isinstance(value, str) and len(value) > 2_000 else value
            for key, value in arguments.items()
        }
        self._emit(
            {
                "type": "tool_call",
                "id": call_id,
                "name": name,
                "arguments": displayed_arguments,
            }
        )
        try:
            result = function(**arguments)
        except Exception as exc:
            self._emit(
                {
                    "type": "tool_result",
                    "id": call_id,
                    "name": name,
                    "result": f"Error: {exc}",
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                }
            )
            raise
        self._emit(
            {
                "type": "tool_result",
                "id": call_id,
                "name": name,
                "result": str(result)[:8_000],
                "duration_ms": round((time.perf_counter() - started) * 1000),
            }
        )
        return result

    def _path(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Path must stay inside the selected repository") from exc
        if ".git" in relative.parts:
            raise ValueError("The repository's .git directory is protected")
        return candidate

    def _files(self):
        for path in self.root.rglob("*"):
            relative = path.relative_to(self.root)
            if (
                path.is_file()
                and not path.is_symlink()
                and not any(part in IGNORED_PARTS for part in relative.parts)
            ):
                yield path

    @staticmethod
    def _text(path: Path) -> str:
        data = path.read_bytes()
        if b"\x00" in data[:4096]:
            raise ValueError("File appears to be binary")
        decoded = data.decode("utf-8", errors="replace")
        return decoded.replace("\r\n", "\n").replace("\r", "\n")

    def list_files(self, pattern: str = "*") -> str:
        """List repository files. Pattern is a glob such as '*.py' or 'src/*'."""
        matches = []
        for path in self._files():
            relative = path.relative_to(self.root).as_posix()
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern):
                matches.append(relative)
            if len(matches) >= 500:
                matches.append("... result limited to 500 files")
                break
        return "\n".join(sorted(matches)) or "No matching files"

    def read_file(self, path: str, start_line: int = 1, end_line: int = 400) -> str:
        """Read a text file with line numbers. Use 1-based start and end lines."""
        file_path = self._path(path)
        if not file_path.is_file():
            return f"Error: file not found: {path}"
        lines = self._text(file_path).splitlines()
        start = max(1, start_line)
        end = min(len(lines), max(start, end_line), start + 499)
        if not lines:
            return "(empty file)"
        return "\n".join(f"{number:4}: {lines[number - 1]}" for number in range(start, end + 1))

    def search_code(self, query: str, path: str = ".") -> str:
        """Search text files for a literal string, optionally below a directory."""
        base = self._path(path)
        files = [base] if base.is_file() else (
            file for file in base.rglob("*")
            if file.is_file()
            and not file.is_symlink()
            and not any(
                part in IGNORED_PARTS
                for part in file.relative_to(self.root).parts
            )
        )
        matches = []
        for file in files:
            try:
                lines = self._text(file).splitlines()
            except (OSError, ValueError):
                continue
            for number, line in enumerate(lines, start=1):
                if query.casefold() in line.casefold():
                    relative = file.relative_to(self.root).as_posix()
                    matches.append(f"{relative}:{number}: {line[:300]}")
                    if len(matches) >= 200:
                        matches.append("... result limited to 200 matches")
                        return "\n".join(matches)
        return "\n".join(matches) or "No matches"

    def write_file(self, path: str, content: str) -> str:
        """Create a new UTF-8 text file. Refuses to overwrite existing files."""
        file_path = self._path(path)
        if file_path.exists():
            return (
                f"Error: refusing to overwrite existing file: {path}. "
                "Use replace_in_file for targeted edits."
            )
        file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with file_path.open("xb") as new_file:
                new_file.write(content.encode("utf-8"))
        except FileExistsError:
            return f"Error: refusing to overwrite existing file: {path}"
        return f"Created {file_path.relative_to(self.root).as_posix()} ({len(content)} characters)"

    def replace_in_file(self, path: str, old_text: str, new_text: str) -> str:
        """Replace one exact, unique block of text in a repository file."""
        file_path = self._path(path)
        if not file_path.is_file():
            return f"Error: file not found: {path}"
        raw_content = file_path.read_bytes().decode("utf-8", errors="replace")
        newline = "\r\n" if "\r\n" in raw_content else "\r" if "\r" in raw_content else "\n"
        content = raw_content.replace("\r\n", "\n").replace("\r", "\n")
        old_text = old_text.replace("\r\n", "\n").replace("\r", "\n")
        new_text = new_text.replace("\r\n", "\n").replace("\r", "\n")
        count = content.count(old_text)
        if count != 1:
            return f"Error: expected one occurrence of old_text, found {count}"
        if old_text == content or (
            len(content) > 1_000 and len(old_text) > len(content) * 0.6
        ):
            return "Error: replacement is too large; make smaller targeted edits"
        updated = content.replace(old_text, new_text, 1)
        if newline != "\n":
            updated = updated.replace("\n", newline)
        file_path.write_bytes(updated.encode("utf-8"))
        return f"Updated {file_path.relative_to(self.root).as_posix()}"

    def run_command(self, command: list[str]) -> str:
        """Run a test, lint, build, or read-only git command in the repository."""
        if not command:
            return "Error: command cannot be empty"
        allowed = {
            "bun", "cargo", "dotnet", "go", "gradle", "gradlew", "mvn",
            "npm", "pnpm", "py", "pytest", "python", "python3", "ruff", "yarn",
        }
        executable = Path(command[0]).name.lower()
        if executable == "git":
            if len(command) < 2 or command[1] not in {"diff", "status"}:
                return "Error: only 'git diff' and 'git status' are allowed"
        elif executable not in allowed:
            return f"Error: command '{command[0]}' is not in the test/build allowlist"
        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=120,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"Error running command: {exc}"
        output = (result.stdout + result.stderr).strip()
        if len(output) > 20_000:
            output = output[-20_000:] + "\n... output truncated"
        return f"Exit code: {result.returncode}\n{output or '(no output)'}"

    def initial_context(self, max_characters: int = 30_000) -> str:
        """Build a small repository map and include important project metadata."""
        relative_files = sorted(path.relative_to(self.root).as_posix() for path in self._files())
        tree = "\n".join(relative_files[:500])
        if len(relative_files) > 500:
            tree += f"\n... and {len(relative_files) - 500} more files"

        sections = [f"Repository root: {self.root}", f"Files:\n{tree or '(empty repository)'}"]
        remaining = max_characters - sum(len(section) for section in sections)
        for relative in relative_files:
            path = Path(relative)
            if path.name not in CONTEXT_FILES:
                continue
            try:
                content = self._text(self.root / path)
            except (OSError, ValueError):
                continue
            excerpt = content[: min(8_000, remaining)]
            if not excerpt:
                break
            sections.append(f"--- {relative} ---\n{excerpt}")
            remaining -= len(excerpt)
            if remaining <= 0:
                break
        return "\n\n".join(sections)

    def functions(self) -> list:
        """Functions handed to an LLM that supports automatic tool calling."""
        def list_files(pattern: str = "*") -> str:
            """List repository files. Pattern is a glob such as '*.py' or 'src/*'."""
            return self._invoke("list_files", {"pattern": pattern}, self.list_files)

        def read_file(path: str, start_line: int = 1, end_line: int = 400) -> str:
            """Read a text file with line numbers. Use 1-based start and end lines."""
            return self._invoke(
                "read_file",
                {"path": path, "start_line": start_line, "end_line": end_line},
                self.read_file,
            )

        def search_code(query: str, path: str = ".") -> str:
            """Search text files for a literal string, optionally below a directory."""
            return self._invoke(
                "search_code", {"query": query, "path": path}, self.search_code
            )

        def write_file(path: str, content: str) -> str:
            """Create a new UTF-8 text file. Refuses to overwrite existing files."""
            return self._invoke(
                "write_file", {"path": path, "content": content}, self.write_file
            )

        def replace_in_file(path: str, old_text: str, new_text: str) -> str:
            """Replace one exact, unique block of text in a repository file."""
            return self._invoke(
                "replace_in_file",
                {"path": path, "old_text": old_text, "new_text": new_text},
                self.replace_in_file,
            )

        def run_command(command: list[str]) -> str:
            """Run a test, lint, build, or read-only git command in the repository."""
            return self._invoke(
                "run_command", {"command": command}, self.run_command
            )

        return [
            list_files,
            read_file,
            search_code,
            write_file,
            replace_in_file,
            run_command,
        ]
