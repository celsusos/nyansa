import unittest

from context.manager import MessageItem


class MessageSerializationTests(unittest.TestCase):
    """Every serialized message must carry a `content` field.

    Strict OpenAI-compatible providers (e.g. Poolside via OpenRouter) reject a
    request where a message omits `content`, even though OpenAI treats it as
    optional. This is the regression guard for that 400.
    """

    def test_assistant_tool_call_message_includes_null_content(self):
        item = MessageItem(
            role="assistant",
            content="",
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "grep", "arguments": "{}"}}],
        )
        result = item.to_dict()
        self.assertIn("content", result)          # field present, not dropped
        self.assertIsNone(result["content"])       # null is the canonical empty value
        self.assertIn("tool_calls", result)

    def test_assistant_text_message_keeps_its_text(self):
        result = MessageItem(role="assistant", content="hello").to_dict()
        self.assertEqual(result["content"], "hello")

    def test_user_message_includes_content(self):
        result = MessageItem(role="user", content="do the thing").to_dict()
        self.assertEqual(result["content"], "do the thing")

    def test_tool_result_message_includes_content(self):
        result = MessageItem(role="tool", content="matched 3 files", tool_call_id="c1").to_dict()
        self.assertIn("content", result)
        self.assertEqual(result["content"], "matched 3 files")
        self.assertEqual(result["tool_call_id"], "c1")

    def test_every_message_has_content_key(self):
        for item in (
            MessageItem(role="user", content="x"),
            MessageItem(role="assistant", content=""),
            MessageItem(role="assistant", content="", tool_calls=[{"id": "1"}]),
            MessageItem(role="tool", content="r", tool_call_id="1"),
        ):
            self.assertIn("content", item.to_dict())


if __name__ == "__main__":
    unittest.main()
