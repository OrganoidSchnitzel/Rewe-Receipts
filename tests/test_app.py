import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from receipts import config
from receipts.models import ExtractedItem


class AppTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        config.RECEIPT_FILES_DIR = Path(self._tmp.name) / "files"
        from receipts import db as db_module
        importlib.reload(db_module)
        self.db = db_module
        self.db.init_db()
        import app as app_module
        self.client = app_module.app.test_client()
        self.rid = self.db.create_receipt(
            source="rewe", external_id="rewe:1", store="REWE",
            items=[ExtractedItem(name="MILCH", total_price=1.29)],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _form(self, name="CHANGED", included=True):
        form = {"item_count": "1", "name_0": name, "quantity_0": "1",
                "total_price_0": "1.29"}
        if included:
            form["included"] = "0"
        return form


class ReadOnlyGuardTests(AppTestCase):
    def test_pending_receipt_can_be_saved(self) -> None:
        self.client.post(f"/receipts/{self.rid}/save", data=self._form())
        self.assertEqual("CHANGED", self.db.get_receipt(self.rid).items[0].name)

    def test_dismissed_receipt_cannot_be_edited_or_settled(self) -> None:
        self.db.set_status(self.rid, "dismissed")
        with mock.patch("receipts.spliit.create_expense") as ce:
            for action in ("save", "spliit", "spliit-advanced", "reextract"):
                response = self.client.post(f"/receipts/{self.rid}/{action}", data=self._form())
                self.assertEqual(302, response.status_code, action)
        ce.assert_not_called()
        receipt = self.db.get_receipt(self.rid)
        self.assertEqual("MILCH", receipt.items[0].name)
        self.assertEqual("dismissed", receipt.status)

    def test_settled_receipt_cannot_be_edited(self) -> None:
        self.db.mark_settled(self.rid, "exp_1")
        self.client.post(f"/receipts/{self.rid}/save", data=self._form())
        self.assertEqual("MILCH", self.db.get_receipt(self.rid).items[0].name)

    def test_delete_goes_through_ingest(self) -> None:
        with mock.patch("receipts.notifier.update_receipt_message") as update:
            response = self.client.post(f"/receipts/{self.rid}/delete")
        self.assertEqual(302, response.status_code)
        self.assertIsNone(self.db.get_receipt(self.rid))
        self.assertTrue(update.call_args.kwargs["deleted"])


class ReweIngestTotalTests(AppTestCase):
    def test_printed_summe_becomes_the_receipt_total(self) -> None:
        from receipts import ingest
        importlib.reload(ingest)
        # The parser misses a line here, so the items (3.78) fall short of the
        # printed total (5.00): the stored total keeps that gap visible.
        doc = {"id": 7, "content": "MILCH 1,29 A\nBROT 2,49 A\n???? 1,22\nSUMME 5,00",
               "created": "2026-09-12"}
        with mock.patch.object(ingest.paperless, "get_document", return_value=doc), \
             mock.patch.object(ingest.paperless, "download_document",
                               side_effect=OSError("no pdf")), \
             mock.patch.object(ingest.paperless, "mark_document_processed"), \
             mock.patch.object(ingest.notifier, "notify_new_receipt"):
            rid = ingest.ingest_rewe_document(7)
        receipt = self.db.get_receipt(rid)
        self.assertAlmostEqual(5.00, receipt.total_amount, places=2)


if __name__ == "__main__":
    unittest.main()
