from __future__ import annotations

import argparse
import os
from pathlib import Path

from code_agent.provider_factory import build_providers
from code_agent.repo_tools import RepositoryTools
from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_FILE)

def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Let an AI agent inspect and edit a local code repository."
    )
    parser.add_argument("--repo", help="Path to the local repository")
    parser.add_argument("--query", help="One change request; exits when complete")
    parser.add_argument("--model", help="Gemini model name (or set GEMINI_MODEL)")
    parser.add_argument(
        "--local-model", help="Local Ollama model name (or set OLLAMA_MODEL)"
    )
    parser.add_argument("--groq-model", help="Groq model name (or set GROQ_MODEL)")
    parser.add_argument(
        "--provider",
        choices=("auto", "gemini", "groq", "ollama"),
        default=os.environ.get("CODE_AGENT_PROVIDER", "auto"),
        help="LLM provider; auto tries Gemini, Groq, then local Ollama",
    )
    parser.add_argument(
        "--hide-thinking",
        action="store_true",
        help="Hide model reasoning when the provider returns it",
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    print("\nSimple Code Agent\n")

    repository = args.repo or input("Local repository path: ").strip()
    if not repository:
        print("A repository path is required.")
        return

    try:
        tools = RepositoryTools(Path(repository))
        repository_context = tools.initial_context()
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}")
        return

    print(f"Loaded repository: {tools.root}")
    print("Repository context is ready.")

    try:
        providers = build_providers(
            provider=args.provider,
            gemini_model=args.model,
            groq_model=args.groq_model,
            local_model=args.local_model,
            show_thinking=not args.hide_thinking,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}")
        return

    one_shot_query = args.query

    while True:
        request = one_shot_query or input(
            "\nWhat should I change? (type 'exit' to quit)\n> "
        ).strip()
        if request.casefold() in {"exit", "quit"}:
            break
        if not request:
            continue

        for index, provider in enumerate(providers):
            if index > 0:
                # A failed provider may already have edited files. Refresh before fallback.
                repository_context = tools.initial_context()
            print(f"\nWorking with {provider.model}...\n")
            try:
                result = provider.solve(
                    request=request,
                    repository_context=repository_context,
                    tools=tools.functions(),
                )
                print(result)
                break
            except Exception as exc:
                has_fallback = index < len(providers) - 1
                if has_fallback:
                    next_model = providers[index + 1].model
                    print(f"{provider.model} failed: {exc}\nTrying {next_model}...")
                else:
                    print(f"Agent error: {exc}")

        if one_shot_query:
            break
        repository_context = tools.initial_context()


if __name__ == "__main__":
    main()
