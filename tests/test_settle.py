import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from receipts import config
from receipts.models import ExtractedItem


class SettleReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        config.RECEIPT_FILES_DIR = Path(self._tmp.name) / "files"
        from receipts import db as db_module
        importlib.reload(db_module)
        self.db = db_module
        self.db.init_db()
        from receipts import ingest as ingest_module
        importlib.reload(ingest_module)
        self.ingest = ingest_module

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _receipt(self):
        return self.db.create_receipt(
            source="lidl", external_id="lidl:t1", store="Lidl Hamm",
            purchase_date="2026-09-12", total_amount=4.17,
            items=[ExtractedItem(name="Banane", total_price=0.78),
                   ExtractedItem(name="Orangen", total_price=3.39)],
        )

    def test_settle_creates_expense_and_marks_settled(self) -> None:
        rid = self._receipt()
        with mock.patch.object(self.ingest.spliit, "create_expense",
                               return_value="exp_123") as ce:
            ok, msg = self.ingest.settle_receipt(rid)
        self.assertTrue(ok)
        self.assertIn("4.17", msg)
        # amount_eur passed is the included-items sum
        self.assertAlmostEqual(4.17, ce.call_args.kwargs["amount_eur"], places=2)
        r = self.db.get_receipt(rid)
        self.assertEqual("settled", r.status)
        self.assertEqual("exp_123", r.spliit_expense_id)

    def test_settle_is_idempotent(self) -> None:
        rid = self._receipt()
        with mock.patch.object(self.ingest.spliit, "create_expense",
                               return_value="exp_123") as ce:
            self.ingest.settle_receipt(rid)
            ok, msg = self.ingest.settle_receipt(rid)  # second press
        self.assertFalse(ok)
        self.assertIn("already settled", msg)
        ce.assert_called_once()  # no duplicate Spliit expense

    def test_settle_requires_selected_items(self) -> None:
        rid = self._receipt()
        # Deselect everything.
        self.db.replace_items(rid, [
            {"name": "Banane", "quantity": 1, "unit_price": 0.78,
             "total_price": 0.78, "included": False},
        ])
        with mock.patch.object(self.ingest.spliit, "create_expense") as ce:
            ok, msg = self.ingest.settle_receipt(rid)
        self.assertFalse(ok)
        self.assertIn("No items", msg)
        ce.assert_not_called()

    def test_spliit_error_leaves_receipt_pending(self) -> None:
        rid = self._receipt()
        with mock.patch.object(self.ingest.spliit, "create_expense",
                               side_effect=RuntimeError("boom")):
            ok, msg = self.ingest.settle_receipt(rid)
        self.assertFalse(ok)
        self.assertIn("Spliit error", msg)
        self.assertEqual("pending", self.db.get_receipt(rid).status)


class DismissTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        config.RECEIPT_FILES_DIR = Path(self._tmp.name) / "files"
        from receipts import db as db_module
        importlib.reload(db_module)
        self.db = db_module
        self.db.init_db()
        from receipts import ingest as ingest_module
        importlib.reload(ingest_module)
        self.ingest = ingest_module

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _receipt(self):
        return self.db.create_receipt(
            source="rewe", external_id="rewe:1", store="REWE", total_amount=5.0,
            items=[ExtractedItem(name="A", total_price=5.0)],
        )

    def test_dismiss_sets_status_no_expense(self) -> None:
        rid = self._receipt()
        with mock.patch.object(self.ingest.spliit, "create_expense") as ce:
            ok, msg = self.ingest.dismiss_receipt(rid)
        self.assertTrue(ok)
        ce.assert_not_called()
        r = self.db.get_receipt(rid)
        self.assertEqual("dismissed", r.status)
        self.assertIsNone(r.spliit_expense_id)

    def test_dismiss_is_idempotent(self) -> None:
        rid = self._receipt()
        self.ingest.dismiss_receipt(rid)
        ok, msg = self.ingest.dismiss_receipt(rid)
        self.assertFalse(ok)
        self.assertIn("Already dismissed", msg)

    def test_settle_refuses_dismissed(self) -> None:
        rid = self._receipt()
        self.ingest.dismiss_receipt(rid)
        with mock.patch.object(self.ingest.spliit, "create_expense") as ce:
            ok, msg = self.ingest.settle_receipt(rid)
        self.assertFalse(ok)
        self.assertIn("dismissed", msg)
        ce.assert_not_called()

    def test_reopen_returns_to_pending(self) -> None:
        rid = self._receipt()
        self.ingest.dismiss_receipt(rid)
        ok, msg = self.ingest.reopen_receipt(rid)
        self.assertTrue(ok)
        self.assertEqual("pending", self.db.get_receipt(rid).status)

    def test_cannot_reopen_pending(self) -> None:
        rid = self._receipt()
        ok, msg = self.ingest.reopen_receipt(rid)
        self.assertFalse(ok)

    def test_dismissed_receipt_is_not_reextracted(self) -> None:
        rid = self._receipt()
        self.ingest.dismiss_receipt(rid)
        with mock.patch.object(self.ingest.paperless, "get_document_text") as fetch:
            self.assertFalse(self.ingest.reextract_receipt(rid))
        fetch.assert_not_called()


class TelegramSyncTests(unittest.TestCase):
    """Every state change — web UI or chat — re-renders the receipt's message."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        config.RECEIPT_FILES_DIR = Path(self._tmp.name) / "files"
        from receipts import db as db_module
        importlib.reload(db_module)
        self.db = db_module
        self.db.init_db()
        from receipts import ingest as ingest_module
        importlib.reload(ingest_module)
        self.ingest = ingest_module
        self.rid = self.db.create_receipt(
            source="rewe", external_id="rewe:1", store="REWE",
            items=[ExtractedItem(name="A", total_price=5.0)],
        )
        self.db.set_telegram_message(self.rid, 42)
        patcher = mock.patch.object(self.ingest.notifier, "update_receipt_message")
        self.update = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _synced_status(self):
        receipt = self.update.call_args[0][0]
        return receipt.status, receipt.telegram_message_id

    def test_settle_updates_message(self) -> None:
        with mock.patch.object(self.ingest.spliit, "create_expense", return_value="e"):
            self.ingest.settle_receipt(self.rid)
        self.assertEqual(("settled", 42), self._synced_status())
        self.assertIn("5.00", self.update.call_args[0][1])  # outcome line

    def test_dismiss_then_reopen_updates_message(self) -> None:
        self.ingest.dismiss_receipt(self.rid)
        self.assertEqual(("dismissed", 42), self._synced_status())
        self.ingest.reopen_receipt(self.rid)
        self.assertEqual(("pending", 42), self._synced_status())

    def test_delete_marks_message_deleted(self) -> None:
        self.assertEqual("rewe:1", self.ingest.delete_receipt(self.rid))
        self.assertTrue(self.update.call_args.kwargs["deleted"])
        self.assertIsNone(self.db.get_receipt(self.rid))

    def test_failed_settle_leaves_message_alone(self) -> None:
        with mock.patch.object(self.ingest.spliit, "create_expense",
                               side_effect=RuntimeError("boom")):
            self.ingest.settle_receipt(self.rid)
        self.update.assert_not_called()


class ConcurrentSettleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        config.RECEIPT_FILES_DIR = Path(self._tmp.name) / "files"
        from receipts import db as db_module
        importlib.reload(db_module)
        self.db = db_module
        self.db.init_db()
        from receipts import ingest as ingest_module
        importlib.reload(ingest_module)
        self.ingest = ingest_module

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_double_click_creates_one_expense(self) -> None:
        # Two near-simultaneous settles (double-click, or Approve tapped while
        # the web UI is settling) must create exactly one Spliit expense.
        import threading
        import time

        rid = self.db.create_receipt(
            source="rewe", external_id="rewe:1", store="REWE",
            items=[ExtractedItem(name="A", total_price=5.0)],
        )

        def slow_create(**kwargs):
            time.sleep(0.2)  # the Spliit round-trip, where the race lived
            return "exp_1"

        results = []
        with mock.patch.object(self.ingest.spliit, "create_expense",
                               side_effect=slow_create) as ce:
            threads = [
                threading.Thread(target=lambda: results.append(self.ingest.settle_receipt(rid)))
                for _ in range(2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        ce.assert_called_once()
        self.assertEqual([False, True], sorted(ok for ok, _ in results))


if __name__ == "__main__":
    unittest.main()
