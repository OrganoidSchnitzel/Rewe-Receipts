import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from receipts import config, notifier
from receipts.models import ExtractedItem


class FakeResponse:
    def __init__(self, body=None):
        self._body = body or {"ok": True, "result": {"message_id": 77}}

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class TelegramTestCase(unittest.TestCase):
    """Telegram configured against a temp DB; captures every API call."""

    def setUp(self) -> None:
        self._saved = (
            config.TELEGRAM_ENABLED, config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID,
            config.APP_PUBLIC_URL, config.TELEGRAM_TWO_WAY,
        )
        config.TELEGRAM_ENABLED = True
        config.TELEGRAM_BOT_TOKEN = "TOKEN"
        config.TELEGRAM_CHAT_ID = "999"
        config.APP_PUBLIC_URL = "http://host:8881"
        config.TELEGRAM_TWO_WAY = True

        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        config.RECEIPT_FILES_DIR = Path(self._tmp.name) / "files"
        from receipts import db as db_module
        importlib.reload(db_module)
        self.db = db_module
        self.db.init_db()

        self.calls = []

        def fake_post(url, json=None, timeout=None):
            self.calls.append((url.rsplit("/", 1)[-1], json))
            return FakeResponse()

        patcher = mock.patch.object(notifier.requests, "post", fake_post)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        (
            config.TELEGRAM_ENABLED, config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID,
            config.APP_PUBLIC_URL, config.TELEGRAM_TWO_WAY,
        ) = self._saved
        self._tmp.cleanup()

    def make_receipt(self, names=("Milch", "Brot"), total=None, **kw):
        rid = self.db.create_receipt(
            source=kw.pop("source", "lidl"), external_id=kw.pop("external_id", "lidl:1"),
            store=kw.pop("store", "Lidl Hamm"), purchase_date="2026-09-12",
            items=[ExtractedItem(name=n, total_price=1.0) for n in names],
            total_amount=total,
        )
        return self.db.get_receipt(rid)

    def last(self, method):
        return [payload for name, payload in self.calls if name == method][-1]


class NotifyNewReceiptTests(TelegramTestCase):
    def test_noop_when_unconfigured(self) -> None:
        config.TELEGRAM_ENABLED = False
        self.assertFalse(notifier.send_message("hi"))
        self.assertEqual([], self.calls)

    def test_missing_token_is_not_configured(self) -> None:
        config.TELEGRAM_BOT_TOKEN = ""
        self.assertFalse(notifier.is_configured())

    def test_notification_only_puts_link_in_text(self) -> None:
        config.TELEGRAM_TWO_WAY = False
        receipt = self.make_receipt(
            names=["Milch", "Brot & Co", "Käse", "Eier", "Apfel", "Banane", "Kiwi"],
            total=16.31, source="rewe", external_id="rewe:1", store="REWE",
        )
        notifier.notify_new_receipt(receipt)

        payload = self.last("sendMessage")
        text = payload["text"]
        self.assertEqual("999", payload["chat_id"])
        self.assertEqual("HTML", payload["parse_mode"])
        self.assertIn("New REWE", text)
        self.assertIn("2026-09-12 · 7 items · €16.31", text)
        self.assertIn(f"http://host:8881/receipts/{receipt.id}", text)
        # Top items are shown (max 5 + a "+N more"), with HTML-escaped names.
        self.assertIn("Brot &amp; Co", text)
        self.assertIn("+2 more", text)
        self.assertNotIn("reply_markup", payload)

    def test_two_way_adds_approve_review_dismiss_buttons(self) -> None:
        receipt = self.make_receipt()
        notifier.notify_new_receipt(receipt)

        payload = self.last("sendMessage")
        rows = payload["reply_markup"]["inline_keyboard"]
        self.assertEqual(f"approve:{receipt.id}", rows[0][0]["callback_data"])
        self.assertEqual(f"http://host:8881/receipts/{receipt.id}", rows[0][1]["url"])
        self.assertEqual(f"dismiss:{receipt.id}", rows[1][0]["callback_data"])
        # With two-way on, the link lives on the button, not repeated in text.
        self.assertNotIn("Open receipt", payload["text"])

    def test_message_id_is_stored_for_later_updates(self) -> None:
        receipt = self.make_receipt()
        notifier.notify_new_receipt(receipt)
        self.assertEqual(77, self.db.get_receipt(receipt.id).telegram_message_id)

    def test_singular_item_wording(self) -> None:
        notifier.notify_new_receipt(self.make_receipt(names=["Milch"], total=2.50))
        self.assertIn("1 item · €2.50", self.last("sendMessage")["text"])

    def test_failure_is_swallowed(self) -> None:
        def boom(*a, **k):
            raise RuntimeError("network down")

        with mock.patch.object(notifier.requests, "post", boom):
            # Must not raise — ingestion should never break on a Telegram error.
            self.assertFalse(notifier.send_message("hi"))


class UpdateReceiptMessageTests(TelegramTestCase):
    def _notified(self):
        receipt = self.make_receipt()
        notifier.notify_new_receipt(receipt)
        return self.db.get_receipt(receipt.id)

    def test_settled_message_loses_its_buttons(self) -> None:
        receipt = self._notified()
        self.db.mark_settled(receipt.id, "exp_1")
        notifier.update_receipt_message(
            self.db.get_receipt(receipt.id), "Created Spliit expense for €2.00."
        )
        payload = self.last("editMessageText")
        self.assertEqual(77, payload["message_id"])
        self.assertIn("shared to Spliit", payload["text"])
        self.assertIn("Created Spliit expense for €2.00.", payload["text"])
        self.assertNotIn("reply_markup", payload)  # omitted => buttons removed

    def test_dismissed_and_deleted_states(self) -> None:
        receipt = self._notified()
        self.db.set_status(receipt.id, "dismissed")
        notifier.update_receipt_message(self.db.get_receipt(receipt.id))
        self.assertIn("dismissed, nothing shared", self.last("editMessageText")["text"])

        notifier.update_receipt_message(receipt, deleted=True)
        text = self.last("editMessageText")["text"]
        self.assertIn("deleted", text)
        self.assertNotIn("/receipts/", text)  # no link to a receipt that's gone

    def test_pending_again_restores_buttons(self) -> None:
        receipt = self._notified()
        notifier.update_receipt_message(receipt)  # e.g. after a reopen
        rows = self.last("editMessageText")["reply_markup"]["inline_keyboard"]
        self.assertEqual(f"approve:{receipt.id}", rows[0][0]["callback_data"])

    def test_noop_without_a_stored_message(self) -> None:
        notifier.update_receipt_message(self.make_receipt())
        self.assertEqual([], [c for c in self.calls if c[0] == "editMessageText"])


if __name__ == "__main__":
    unittest.main()
