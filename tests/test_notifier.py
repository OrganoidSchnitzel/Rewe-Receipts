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
            config.TELEGRAM_TWO_WAY,
        )

    def tearDown(self) -> None:
        (
            config.TELEGRAM_ENABLED,
            config.TELEGRAM_BOT_TOKEN,
            config.TELEGRAM_CHAT_ID,
            config.APP_PUBLIC_URL,
            config.TELEGRAM_TWO_WAY,
        ) = self._saved

    def _configure(self, two_way: bool = False) -> None:
        config.TELEGRAM_ENABLED = True
        config.TELEGRAM_BOT_TOKEN = "TOKEN"
        config.TELEGRAM_CHAT_ID = "999"
        config.APP_PUBLIC_URL = "http://host:8881"
        config.TELEGRAM_TWO_WAY = two_way

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

    def test_notification_only_puts_link_in_text(self) -> None:
        self._configure(two_way=False)
        captured = {}

        class Resp:
            def raise_for_status(self):
                pass

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return Resp()

        with mock.patch.object(notifier.requests, "post", fake_post):
            notifier.notify_new_receipt(
                "abc123", "REWE receipt", 7, 16.31,
                top_items=["Milch", "Brot & Co", "Käse", "Eier", "Apfel", "Banane"],
            )

        text = captured["json"]["text"]
        self.assertIn("sendMessage", captured["url"])
        self.assertEqual("999", captured["json"]["chat_id"])
        self.assertEqual("HTML", captured["json"]["parse_mode"])
        self.assertIn("7 items · €16.31", text)
        self.assertIn("http://host:8881/receipts/abc123", text)
        # Top items are shown (max 5 + a "+N more"), with HTML-escaped names.
        self.assertIn("Brot &amp; Co", text)
        self.assertIn("+1 more", text)
        self.assertNotIn("reply_markup", captured["json"])

    def test_two_way_adds_approve_and_review_buttons(self) -> None:
        self._configure(two_way=True)
        captured = {}

        class Resp:
            def raise_for_status(self):
                pass

        def fake_post(url, json=None, timeout=None):
            captured["json"] = json
            return Resp()

        with mock.patch.object(notifier.requests, "post", fake_post):
            notifier.notify_new_receipt("abc123", "Lidl Hamm", 11, 37.89)

        row = captured["json"]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual("approve:abc123", row[0]["callback_data"])
        self.assertEqual("http://host:8881/receipts/abc123", row[1]["url"])
        # With two-way on, the link lives on the button, not repeated in text.
        self.assertNotIn("Open receipt", captured["json"]["text"])

    def test_singular_item_wording(self) -> None:
        self._configure()

        class Resp:
            def raise_for_status(self):
                pass

        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["json"] = json
            return Resp()

        with mock.patch.object(notifier.requests, "post", fake_post):
            notifier.notify_new_receipt("x", "Lidl", 1, 2.50)
        self.assertIn("1 item · €2.50", captured["json"]["text"])

    def test_failure_is_swallowed(self) -> None:
        self._configure()

        def boom(*a, **k):
            raise RuntimeError("network down")

        with mock.patch.object(notifier.requests, "post", boom):
            # Must not raise — ingestion should never break on a Telegram error.
            self.assertFalse(notifier.send_message("hi"))


if __name__ == "__main__":
    unittest.main()
