import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from receipts import config, notifier, telegram_bot
from receipts.models import ExtractedItem


class TelegramCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = (config.TELEGRAM_ENABLED, config.TELEGRAM_BOT_TOKEN,
                       config.TELEGRAM_CHAT_ID, config.TELEGRAM_TWO_WAY)
        config.TELEGRAM_ENABLED = True
        config.TELEGRAM_BOT_TOKEN = "T"
        config.TELEGRAM_CHAT_ID = "999"
        config.TELEGRAM_TWO_WAY = True

        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        config.RECEIPT_FILES_DIR = Path(self._tmp.name) / "files"
        from receipts import db as db_module
        importlib.reload(db_module)
        self.db = db_module
        self.db.init_db()
        self.rid = self.db.create_receipt(
            source="lidl", external_id="lidl:1", store="Lidl",
            items=[ExtractedItem(name="Banane", total_price=0.78)],
        )

    def tearDown(self) -> None:
        (config.TELEGRAM_ENABLED, config.TELEGRAM_BOT_TOKEN,
         config.TELEGRAM_CHAT_ID, config.TELEGRAM_TWO_WAY) = self._saved
        self._tmp.cleanup()

    def _update(self, data, chat_id="999"):
        return {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "data": data,
                "message": {"message_id": 55, "chat": {"id": chat_id}},
            },
        }

    def test_approve_settles_and_answers(self) -> None:
        with mock.patch("receipts.ingest.settle_receipt",
                        return_value=(True, "Created Spliit expense for €0.78.")) as settle, \
             mock.patch.object(notifier, "answer_callback") as answer, \
             mock.patch.object(notifier, "update_receipt_message") as update:
            telegram_bot._handle_update(self._update(f"approve:{self.rid}"))

        settle.assert_called_once_with(self.rid)
        self.assertIn("0.78", answer.call_args[0][1])
        # On success ingest re-renders the message itself; the bot doesn't.
        update.assert_not_called()

    def test_legacy_message_is_linked_to_its_receipt(self) -> None:
        # Notifications sent before message ids were stored get linked on press.
        with mock.patch("receipts.ingest.settle_receipt", return_value=(True, "ok")), \
             mock.patch.object(notifier, "answer_callback"):
            telegram_bot._handle_update(self._update(f"approve:{self.rid}"))
        self.assertEqual(55, self.db.get_receipt(self.rid).telegram_message_id)

    def test_press_from_other_chat_is_rejected(self) -> None:
        with mock.patch("receipts.ingest.settle_receipt") as settle, \
             mock.patch.object(notifier, "answer_callback") as answer:
            telegram_bot._handle_update(self._update(f"approve:{self.rid}", chat_id="12345"))
        settle.assert_not_called()
        self.assertIn("authorized", answer.call_args[0][1].lower())

    def test_dismiss_calls_dismiss_not_settle(self) -> None:
        with mock.patch("receipts.ingest.dismiss_receipt",
                        return_value=(True, "Dismissed — nothing shared.")) as dismiss, \
             mock.patch("receipts.ingest.settle_receipt") as settle, \
             mock.patch.object(notifier, "answer_callback") as answer:
            telegram_bot._handle_update(self._update(f"dismiss:{self.rid}"))
        dismiss.assert_called_once_with(self.rid)
        settle.assert_not_called()
        self.assertIn("Dismissed", answer.call_args[0][1])

    def test_stale_press_refreshes_message_to_current_state(self) -> None:
        # Settled in the web UI meanwhile; a tap on the old Approve button
        # changes nothing but brings the message up to date.
        self.db.mark_settled(self.rid, "exp_1")
        with mock.patch.object(notifier, "answer_callback") as answer, \
             mock.patch.object(notifier, "update_receipt_message") as update, \
             mock.patch("receipts.spliit.create_expense") as ce:
            telegram_bot._handle_update(self._update(f"approve:{self.rid}"))
        ce.assert_not_called()
        self.assertIn("already settled", answer.call_args[0][1])
        self.assertEqual("settled", update.call_args[0][0].status)

    def test_non_approve_callback_ignored(self) -> None:
        with mock.patch("receipts.ingest.settle_receipt") as settle, \
             mock.patch.object(notifier, "answer_callback") as answer:
            telegram_bot._handle_update(self._update("something:else"))
        settle.assert_not_called()
        answer.assert_called_once()

    def test_non_callback_update_ignored(self) -> None:
        with mock.patch("receipts.ingest.settle_receipt") as settle:
            telegram_bot._handle_update({"update_id": 2, "message": {"text": "hi"}})
        settle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
