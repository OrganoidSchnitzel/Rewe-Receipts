import importlib
import tempfile
import unittest
from pathlib import Path

from receipts import config
from receipts.models import ExtractedItem


class DbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        config.RECEIPT_FILES_DIR = Path(self._tmp.name) / "files"
        # Reimport db fresh so it reads the patched config path.
        from receipts import db as db_module
        importlib.reload(db_module)
        self.db = db_module
        self.db.init_db()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _items(self):
        return [
            ExtractedItem(name="MILCH", total_price=1.29),
            ExtractedItem(name="BROT", total_price=2.49, source_method="ollama",
                          raw_line="BROT"),
        ]


class DuplicateDetectionTests(DbTestCase):
    def test_unique_external_id_blocks_reimport(self) -> None:
        rid = self.db.create_receipt(
            source="rewe", external_id="rewe:42", items=self._items(), store="REWE"
        )
        self.assertIsNotNone(rid)
        self.assertTrue(self.db.receipt_exists("rewe:42"))

        # Second import of the same external_id is skipped (returns None).
        again = self.db.create_receipt(
            source="rewe", external_id="rewe:42", items=self._items(), store="REWE"
        )
        self.assertIsNone(again)

        # Still only one receipt.
        self.assertEqual(1, len(self.db.list_receipts()))

    def test_total_defaults_to_item_sum(self) -> None:
        rid = self.db.create_receipt(
            source="rewe", external_id="rewe:1", items=self._items()
        )
        receipt = self.db.get_receipt(rid)
        self.assertAlmostEqual(3.78, receipt.total_amount, places=2)
        self.assertEqual(2, len(receipt.items))
        # list_receipts populates the lightweight item_count used by the list UI.
        listed = {r.id: r for r in self.db.list_receipts()}
        self.assertEqual(2, listed[rid].item_count)


class EditAndSettleTests(DbTestCase):
    def test_replace_items_updates_total_from_included(self) -> None:
        rid = self.db.create_receipt(
            source="rewe", external_id="rewe:2", items=self._items()
        )
        self.db.replace_items(rid, [
            {"name": "MILCH", "quantity": 1, "unit_price": 1.29,
             "total_price": 1.29, "included": True},
            {"name": "BROT", "quantity": 1, "unit_price": 2.49,
             "total_price": 2.49, "included": False},
        ])
        receipt = self.db.get_receipt(rid)
        self.assertAlmostEqual(1.29, receipt.total_amount, places=2)
        self.assertTrue(receipt.items[0].included)
        self.assertFalse(receipt.items[1].included)

    def test_mark_settled(self) -> None:
        rid = self.db.create_receipt(
            source="rewe", external_id="rewe:3", items=self._items()
        )
        self.db.mark_settled(rid, "exp_abc")
        receipt = self.db.get_receipt(rid)
        self.assertEqual("settled", receipt.status)
        self.assertEqual("exp_abc", receipt.spliit_expense_id)


class KnownItemsTests(DbTestCase):
    def test_upsert_and_read_back(self) -> None:
        self.db.upsert_known_item("RFW SALAMI FENCH", "Rügenwalder Salami")
        known = self.db.get_known_items()
        self.assertIn("RFW SALAMI FENCH", known)
        self.assertEqual("Rügenwalder Salami", known["RFW SALAMI FENCH"]["canonical_name"])

        # Upserting again increments hit_count without duplicating.
        self.db.upsert_known_item("RFW SALAMI FENCH", "Rügenwalder Salami")
        self.assertEqual(1, len(self.db.get_known_items()))


if __name__ == "__main__":
    unittest.main()
