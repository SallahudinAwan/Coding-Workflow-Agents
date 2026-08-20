import unittest

from langchain_core.messages import AIMessage

from code_agent.langchain_provider import LangChainProvider


class FakeAgent:
    def __init__(self):
        self.inputs = []

    def stream(self, input_data, **kwargs):
        self.inputs.append(input_data)
        number = len(self.inputs)
        yield {
            "model": {
                "messages": [
                    AIMessage(
                        content=[
                            {"type": "thinking", "thinking": f"Thought {number}"},
                            {"type": "text", "text": f"Answer {number}"},
                        ]
                    )
                ]
            }
        }


class LangChainProviderTests(unittest.TestCase):
    def test_streams_thinking_and_keeps_follow_up_memory(self):
        events = []
        provider = LangChainProvider(
            provider="ollama",
            model="test-model",
            chat_model=object(),
            on_event=events.append,
        )
        provider._agent = FakeAgent()

        first = provider.solve("first request", "repository context", [])
        second = provider.solve("follow up", "new context", [])

        self.assertEqual(first, "Answer 1")
        self.assertEqual(second, "Answer 2")
        self.assertIn("Repository context:\nrepository context", provider._agent.inputs[0]["messages"][0]["content"])
        self.assertEqual(
            provider._agent.inputs[1]["messages"][0]["content"],
            "Follow-up request:\nfollow up",
        )
        self.assertEqual(
            [event["text"] for event in events], ["Thought 1", "Thought 2"]
        )

    def test_reads_groq_reasoning_metadata(self):
        message = AIMessage(
            content="Done",
            additional_kwargs={"reasoning_content": "Groq thought"},
        )
        self.assertEqual(
            LangChainProvider._message_thinking(message), "Groq thought"
        )

