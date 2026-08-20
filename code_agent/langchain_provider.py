from __future__ import annotations

import os
import uuid
from collections.abc import Callable


SYSTEM_PROMPT = """You are a careful coding agent working in one local repository.

Your job is to implement the user's request, not just describe a solution.
- Begin by understanding the repository context and reading the relevant files.
- Respect any AGENTS.md or repository instructions included in the context.
- Search for existing patterns and make the smallest coherent change.
- Existing files must never be rewritten in full. Preserve all unrelated code.
- Use write_file only to create new files; it refuses to overwrite existing files.
- Read the relevant lines, then use one or more small replace_in_file calls for edits.
- Request independent read-only tools together when possible to reduce round trips.
- Never invent file contents you have not inspected.
- Run the most relevant tests or checks after editing when possible.
- Inspect git diff after editing when the repository supports it.
- If a tool returns an error, correct the call or choose another safe approach.
- Finish with a concise summary of files changed and checks run.
"""


class LangChainProvider:
    """One LangChain agent wrapper for Gemini, Groq, and Ollama models."""

    def __init__(
        self,
        provider: str,
        model: str,
        show_thinking: bool = True,
        on_event: Callable[[dict], None] | None = None,
        chat_model=None,
    ):
        self.provider = provider
        self.model = model
        self.show_thinking = show_thinking
        self.on_event = on_event
        self.chat_model = chat_model or self._build_chat_model()
        self._agent = None
        self._thread_id = uuid.uuid4().hex
        self._has_context = False

    def _build_chat_model(self):
        if self.provider == "gemini":
            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if not api_key:
                raise ValueError("GEMINI_API_KEY is not set")
            try:
                from langchain_google_genai import ChatGoogleGenerativeAI
            except ImportError as exc:
                raise RuntimeError("Install langchain-google-genai") from exc
            return ChatGoogleGenerativeAI(
                model=self.model,
                api_key=api_key,
                include_thoughts=self.show_thinking,
                max_retries=3,
            )

        if self.provider == "groq":
            api_key = os.environ.get("GROQ_API_KEY")
            if not api_key:
                raise ValueError("GROQ_API_KEY is not set")
            try:
                from langchain_groq import ChatGroq
            except ImportError as exc:
                raise RuntimeError("Install langchain-groq") from exc
            options = {
                "model": self.model,
                "api_key": api_key,
                "temperature": 0.1,
                "max_retries": 3,
            }
            if self.show_thinking and any(
                family in self.model.casefold()
                for family in ("qwen", "deepseek", "gpt-oss")
            ):
                options["reasoning_format"] = "parsed"
            return ChatGroq(**options)

        if self.provider == "ollama":
            try:
                from langchain_ollama import ChatOllama
            except ImportError as exc:
                raise RuntimeError("Install langchain-ollama") from exc
            return ChatOllama(
                model=self.model,
                base_url=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                reasoning=self.show_thinking,
                temperature=0,
                num_ctx=32_768,
            )

        raise ValueError(f"Unknown LangChain provider: {self.provider}")

    def _get_agent(self, tools: list):
        if self._agent is None:
            from langchain.agents import create_agent
            from langgraph.checkpoint.memory import InMemorySaver

            self._agent = create_agent(
                model=self.chat_model,
                tools=tools,
                system_prompt=SYSTEM_PROMPT,
                checkpointer=InMemorySaver(),
                name="repository_code_agent",
            )
        return self._agent

    @staticmethod
    def _message_text(message) -> str:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
        return str(content or "")

    @staticmethod
    def _message_thinking(message) -> str:
        thoughts = []
        additional = getattr(message, "additional_kwargs", {}) or {}
        for key in ("reasoning_content", "reasoning", "thinking"):
            if additional.get(key):
                thoughts.append(str(additional[key]))
        content = getattr(message, "content", "")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") not in {
                    "thinking",
                    "reasoning",
                }:
                    continue
                thought = block.get("thinking") or block.get("text")
                if thought:
                    thoughts.append(str(thought))
        return "\n\n".join(dict.fromkeys(thoughts)).strip()

    def solve(self, request: str, repository_context: str, tools: list) -> str:
        agent = self._get_agent(tools)
        include_context = not self._has_context
        if not include_context:
            prompt = f"Follow-up request:\n{request}"
        else:
            prompt = (
                f"Repository context:\n{repository_context}\n\n"
                f"User request:\n{request}"
            )

        final_text = ""
        config = {
            "configurable": {"thread_id": self._thread_id},
            "recursion_limit": 60,
        }
        for update in agent.stream(
            {"messages": [{"role": "user", "content": prompt}]},
            config=config,
            stream_mode="updates",
        ):
            for data in update.values():
                if not isinstance(data, dict):
                    continue
                for message in data.get("messages", []):
                    if getattr(message, "type", None) != "ai":
                        continue
                    thinking = self._message_thinking(message)
                    if self.show_thinking and thinking:
                        if self.on_event:
                            self.on_event({"type": "thinking", "text": thinking})
                        else:
                            print(f"Thinking:\n{thinking}\n")
                    tool_calls = getattr(message, "tool_calls", None) or []
                    text = self._message_text(message)
                    if text and not tool_calls:
                        final_text = text

        if include_context:
            self._has_context = True
        return final_text or "The model finished without a text response."
