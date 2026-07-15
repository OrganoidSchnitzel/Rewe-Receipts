import unittest

from app import ReceiptItem, calculate_total, parse_receipt_text


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

    def test_calculate_total_rounds_to_two_decimals(self) -> None:
        items = [ReceiptItem(name="A", price=1.115), ReceiptItem(name="B", price=2.225)]

        self.assertEqual(3.34, calculate_total(items))


if __name__ == "__main__":
    unittest.main()
