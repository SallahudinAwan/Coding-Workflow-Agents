from __future__ import annotations

from collections.abc import Callable

import os

from code_agent.langchain_provider import LangChainProvider


def build_providers(
    provider: str = "auto",
    gemini_model: str | None = None,
    groq_model: str | None = None,
    local_model: str | None = None,
    show_thinking: bool = True,
    on_event: Callable[[dict], None] | None = None,
) -> list:
    """Create the selected provider or the configured automatic fallback chain."""
    if provider == "ollama":
        return [
            LangChainProvider(
                "ollama",
                local_model or os.environ.get("OLLAMA_MODEL", "qwen3:1.7b"),
                show_thinking,
                on_event,
            )
        ]
    if provider == "groq":
        return [
            LangChainProvider(
                "groq",
                groq_model
                or os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
                show_thinking,
                on_event,
            )
        ]
    if provider == "gemini":
        return [
            LangChainProvider(
                "gemini",
                gemini_model
                or os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
                show_thinking,
                on_event,
            )
        ]
    if provider != "auto":
        raise ValueError(f"Unknown provider: {provider}")

    providers = []
    try:
        providers.append(
            LangChainProvider(
                "gemini",
                gemini_model
                or os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
                show_thinking,
                on_event,
            )
        )
    except (ValueError, RuntimeError):
        pass
    try:
        providers.append(
            LangChainProvider(
                "groq",
                groq_model
                or os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
                show_thinking,
                on_event,
            )
        )
    except (ValueError, RuntimeError):
        pass
    providers.append(
        LangChainProvider(
            "ollama",
            local_model or os.environ.get("OLLAMA_MODEL", "qwen3:1.7b"),
            show_thinking,
            on_event,
        )
    )
    return providers
