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


if __name__ == "__main__":
    unittest.main()
