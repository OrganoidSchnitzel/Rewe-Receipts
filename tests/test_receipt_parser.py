import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import (
    ReceiptItem,
    app,
    calculate_total,
    get_processed_receipt,
    parse_receipt_text,
    upsert_processed_receipt,
)


class ReceiptParserTests(unittest.TestCase):
    def test_parse_receipt_text_extracts_items(self) -> None:
        receipt = """
BANANEN 1,99 A
BROT 2,49 A
SUMME 4,48
        """

        items = parse_receipt_text(receipt)

        self.assertEqual(2, len(items))
        self.assertEqual("BANANEN", items[0].name)
        self.assertEqual(1.99, items[0].price)
        self.assertEqual("BROT", items[1].name)
        self.assertEqual(2.49, items[1].price)

    def test_parse_receipt_text_ignores_payment_and_bonus_lines(self) -> None:
        receipt = """
RFW SALAMI FENCH 3,59 A
KERRYGOLD CHEDD. 1,59 A
Geg. VISA EUR 13,27
TSE-Start: 2026-07-17T19:13: 43,00
Mit diesem Einkauf hast du 0,50
Eingesetztes Bonus-Guthaben: 3,04
SUMME 5,18
        """

        items = parse_receipt_text(receipt)

        self.assertEqual(2, len(items))
        self.assertEqual("RFW SALAMI FENCH", items[0].name)
        self.assertEqual(3.59, items[0].price)
        self.assertEqual("KERRYGOLD CHEDD.", items[1].name)
        self.assertEqual(1.59, items[1].price)

    def test_calculate_total_rounds_to_two_decimals(self) -> None:
        items = [ReceiptItem(name="A", price=1.115), ReceiptItem(name="B", price=2.225)]

        self.assertEqual(3.34, calculate_total(items))


class ProcessedReceiptTests(unittest.TestCase):
    def test_upsert_processed_receipt_stores_and_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "processed_receipts.json"
            with patch.dict("os.environ", {"RECEIPTS_DB_PATH": str(db_path)}, clear=False):
                items = [ReceiptItem(name="MILCH", price=1.29), ReceiptItem(name="BROT", price=2.49)]
                receipt_id = upsert_processed_receipt(all_items=items, selected_indices=[0])

                stored = get_processed_receipt(receipt_id)
                self.assertIsNotNone(stored)
                self.assertEqual([0], stored["selected_indices"])
                self.assertEqual(1.29, stored["selected_total"])

                upsert_processed_receipt(all_items=items, selected_indices=[0, 1], receipt_id=receipt_id)
                updated = get_processed_receipt(receipt_id)

                self.assertEqual([0, 1], updated["selected_indices"])
                self.assertEqual(3.78, updated["selected_total"])

    def test_processed_receipts_can_be_listed_and_edited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "processed_receipts.json"
            with patch.dict("os.environ", {"RECEIPTS_DB_PATH": str(db_path)}, clear=False):
                receipt_id = upsert_processed_receipt(
                    all_items=[ReceiptItem(name="APFEL", price=1.0)],
                    selected_indices=[0],
                )

                client = app.test_client()
                list_response = client.get("/receipts")
                edit_response = client.get(f"/receipts/{receipt_id}/edit")

                self.assertEqual(200, list_response.status_code)
                self.assertIn(b"Processed receipts", list_response.data)
                self.assertIn(receipt_id.encode(), list_response.data)

                self.assertEqual(200, edit_response.status_code)
                self.assertIn(b"APFEL", edit_response.data)
                self.assertIn(b"checked", edit_response.data)


if __name__ == "__main__":
    unittest.main()
