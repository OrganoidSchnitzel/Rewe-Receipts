import unittest
from unittest import mock

from receipts import config, notifier


class NotifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = (
            config.TELEGRAM_ENABLED,
            config.TELEGRAM_BOT_TOKEN,
            config.TELEGRAM_CHAT_ID,
            config.APP_PUBLIC_URL,
        )

    def tearDown(self) -> None:
        (
            config.TELEGRAM_ENABLED,
            config.TELEGRAM_BOT_TOKEN,
            config.TELEGRAM_CHAT_ID,
            config.APP_PUBLIC_URL,
        ) = self._saved

    def _configure(self) -> None:
        config.TELEGRAM_ENABLED = True
        config.TELEGRAM_BOT_TOKEN = "TOKEN"
        config.TELEGRAM_CHAT_ID = "999"
        config.APP_PUBLIC_URL = "http://host:8881"

    def test_noop_when_unconfigured(self) -> None:
        config.TELEGRAM_ENABLED = False
        with mock.patch.object(notifier.requests, "post") as post:
            self.assertFalse(notifier.send_message("hi"))
            post.assert_not_called()

    def test_missing_token_is_not_configured(self) -> None:
        config.TELEGRAM_ENABLED = True
        config.TELEGRAM_BOT_TOKEN = ""
        config.TELEGRAM_CHAT_ID = "999"
        self.assertFalse(notifier.is_configured())

    def test_new_receipt_message_includes_link(self) -> None:
        self._configure()
        captured = {}

        class Resp:
            def raise_for_status(self):
                pass

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return Resp()

        with mock.patch.object(notifier.requests, "post", fake_post):
            notifier.notify_new_receipt("abc123", "REWE receipt", 7, 16.31)

        self.assertIn("sendMessage", captured["url"])
        self.assertEqual("999", captured["json"]["chat_id"])
        self.assertIn("7 item(s) · €16.31", captured["json"]["text"])
        self.assertIn("http://host:8881/receipts/abc123", captured["json"]["text"])

    def test_failure_is_swallowed(self) -> None:
        self._configure()

        def boom(*a, **k):
            raise RuntimeError("network down")

        with mock.patch.object(notifier.requests, "post", boom):
            # Must not raise — ingestion should never break on a Telegram error.
            self.assertFalse(notifier.send_message("hi"))


if __name__ == "__main__":
    unittest.main()
