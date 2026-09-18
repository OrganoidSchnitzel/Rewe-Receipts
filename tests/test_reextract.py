import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from receipts import config
from receipts.models import ExtractedItem


class ReextractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        config.RECEIPT_FILES_DIR = Path(self._tmp.name) / "files"
        from receipts import db as db_module
        importlib.reload(db_module)
        self.db = db_module
        self.db.init_db()
        # ingest imports db at module load; reload so it uses the test db.
        from receipts import ingest as ingest_module
        importlib.reload(ingest_module)
        self.ingest = ingest_module

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_receipt(self, source, external_id, names):
        return self.db.create_receipt(
            source=source,
            external_id=external_id,
            items=[ExtractedItem(name=n, total_price=1.0) for n in names],
            store=source.upper(),
        )

    def test_reextract_rewe_replaces_items(self) -> None:
        rid = self._make_receipt("rewe", "rewe:5", ["OLD", "OLD", "OLD"])
        with mock.patch.object(
            self.ingest.paperless, "get_document_text",
            return_value="MILCH 1,29 A\nBROT 2,49 A\nSUMME 3,78",
        ):
            self.assertTrue(self.ingest.reextract_receipt(rid))
        items = self.db.get_receipt(rid).items
        self.assertEqual(["MILCH", "BROT"], [i.name for i in items])

    def test_reextract_lidl_uses_passed_client(self) -> None:
        rid = self._make_receipt("lidl", "lidl:t1", ["Dup"] * 5)
        parsed = self.ingest.lidl.LidlReceipt(
            external_id="lidl:t1", purchase_date=None, total_amount=4.17,
            store="Lidl", items=[
                ExtractedItem(name="Banane", total_price=0.78),
                ExtractedItem(name="Orangen", total_price=3.39),
            ],
        )
        with mock.patch.object(self.ingest.lidl, "is_configured", return_value=True), \
             mock.patch.object(self.ingest.lidl, "fetch_ticket", return_value=parsed) as ft:
            self.assertTrue(self.ingest.reextract_receipt(rid, lidl_client=object()))
        ft.assert_called_once()
        items = self.db.get_receipt(rid).items
        self.assertEqual(["Banane", "Orangen"], [i.name for i in items])
        self.assertAlmostEqual(4.17, self.db.get_receipt(rid).total_amount, places=2)

    def test_settled_receipt_is_not_reextracted(self) -> None:
        rid = self._make_receipt("rewe", "rewe:9", ["KEEP"])
        self.db.mark_settled(rid, "exp_1")
        self.assertFalse(self.ingest.reextract_receipt(rid))
        self.assertEqual(["KEEP"], [i.name for i in self.db.get_receipt(rid).items])

    def test_manual_receipt_has_no_source(self) -> None:
        rid = self._make_receipt("rewe", "manual:rewe:abc", ["X"])
        # external_id splits, but source rewe with non-int ref -> handled as rewe;
        # a truly manual source ('manual') returns False.
        rid2 = self.db.create_receipt(
            source="manual", external_id="manual:xyz",
            items=[ExtractedItem(name="Y", total_price=1.0)], store="MANUAL",
        )
        self.assertFalse(self.ingest.reextract_receipt(rid2))


if __name__ == "__main__":
    unittest.main()
