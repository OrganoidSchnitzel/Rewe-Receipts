import unittest
from unittest import mock

from receipts import config, notifier, telegram_bot


class TelegramCallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = (config.TELEGRAM_ENABLED, config.TELEGRAM_BOT_TOKEN,
                       config.TELEGRAM_CHAT_ID, config.TELEGRAM_TWO_WAY)
        config.TELEGRAM_ENABLED = True
        config.TELEGRAM_BOT_TOKEN = "T"
        config.TELEGRAM_CHAT_ID = "999"
        config.TELEGRAM_TWO_WAY = True

    def tearDown(self) -> None:
        (config.TELEGRAM_ENABLED, config.TELEGRAM_BOT_TOKEN,
         config.TELEGRAM_CHAT_ID, config.TELEGRAM_TWO_WAY) = self._saved

    def _update(self, data, chat_id="999"):
        return {
            "update_id": 1,
            "callback_query": {
                "id": "cb1",
                "data": data,
                "message": {"message_id": 55, "chat": {"id": chat_id}},
            },
        }

    def test_approve_settles_and_edits(self) -> None:
        with mock.patch("receipts.ingest.settle_receipt",
                        return_value=(True, "Created Spliit expense for €37.89.")) as settle, \
             mock.patch.object(notifier, "answer_callback") as answer, \
             mock.patch.object(notifier, "edit_message") as edit:
            telegram_bot._handle_update(self._update("approve:abc123"))

        settle.assert_called_once_with("abc123")
        answer.assert_called_once()
        edit.assert_called_once()
        self.assertIn("37.89", edit.call_args[0][2])

    def test_press_from_other_chat_is_rejected(self) -> None:
        with mock.patch("receipts.ingest.settle_receipt") as settle, \
             mock.patch.object(notifier, "answer_callback") as answer:
            telegram_bot._handle_update(self._update("approve:abc123", chat_id="12345"))
        settle.assert_not_called()
        self.assertIn("authorized", answer.call_args[0][1].lower())

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

    def test_already_settled_does_not_edit(self) -> None:
        with mock.patch("receipts.ingest.settle_receipt",
                        return_value=(False, "Already settled.")), \
             mock.patch.object(notifier, "answer_callback") as answer, \
             mock.patch.object(notifier, "edit_message") as edit:
            telegram_bot._handle_update(self._update("approve:abc123"))
        answer.assert_called_once()
        edit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
