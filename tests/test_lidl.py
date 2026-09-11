import unittest

from receipts.lidl import parse_amount, parse_lidl_ticket, reconciles


# A representative ticket detail, matching the lidl-plus `ticket()` shape:
# comma-decimal string amounts, per-line discounts, a weight line.
SAMPLE_TICKET = {
    "id": "abc123",
    "date": "2026-09-10T17:42:00",
    "totalAmount": "7,17",
    "store": {"name": "Hamburg Zentrum"},
    "itemsLine": [
        {
            "name": "Vegane Frikadellen",
            "currentUnitPrice": "2,19",
            "quantity": "1",
            "isWeight": False,
            "originalAmount": "2,19",
            "discounts": [{"description": "5€ Coupon", "amount": "0,21"}],
            "taxGroupName": "A",
        },
        {
            "name": "Bananen",
            "currentUnitPrice": "1,99",
            "quantity": "0,650",
            "isWeight": True,
            "originalAmount": "1,29",
            "discounts": [],
            "taxGroupName": "A",
        },
        {
            "name": "Milch",
            "currentUnitPrice": "1,95",
            "quantity": "2",
            "isWeight": False,
            "originalAmount": "3,90",
            "discounts": [],
            "taxGroupName": "A",
        },
    ],
}


class ParseAmountTests(unittest.TestCase):
    def test_comma_decimal(self) -> None:
        self.assertEqual(2.19, parse_amount("2,19"))
        self.assertEqual(-1.20, parse_amount("-1,20"))
        self.assertEqual(0.0, parse_amount(None))
        self.assertEqual(0.0, parse_amount("nonsense"))
        self.assertEqual(16.31, parse_amount(16.31))


class ParseTicketTests(unittest.TestCase):
    def test_maps_external_id_date_store(self) -> None:
        r = parse_lidl_ticket(SAMPLE_TICKET)
        self.assertEqual("lidl:abc123", r.external_id)
        self.assertEqual("2026-09-10T17:42:00", r.purchase_date)
        self.assertEqual("Lidl Hamburg Zentrum", r.store)
        self.assertEqual(3, len(r.items))

    def test_discount_reduces_line_total(self) -> None:
        r = parse_lidl_ticket(SAMPLE_TICKET)
        frikadellen = r.items[0]
        # 2,19 gross - 0,21 discount = 1,98 net
        self.assertEqual(1.98, frikadellen.total_price)
        self.assertEqual("lidl", frikadellen.source_method)

    def test_weight_line_uses_line_amount(self) -> None:
        r = parse_lidl_ticket(SAMPLE_TICKET)
        bananen = r.items[1]
        self.assertEqual(0.650, bananen.quantity)
        self.assertEqual(1.29, bananen.total_price)
        self.assertEqual(1.99, bananen.unit_price)  # per kg

    def test_multi_quantity_line(self) -> None:
        r = parse_lidl_ticket(SAMPLE_TICKET)
        milch = r.items[2]
        self.assertEqual(2.0, milch.quantity)
        self.assertEqual(3.90, milch.total_price)

    def test_reconciles_with_ticket_total(self) -> None:
        r = parse_lidl_ticket(SAMPLE_TICKET)
        # 1,98 + 1,29 + 3,90 = 7,17 == totalAmount
        self.assertEqual(7.17, round(sum(i.total_price for i in r.items), 2))
        self.assertEqual(7.17, r.total_amount)
        self.assertTrue(reconciles(r))

    def test_total_falls_back_to_item_sum(self) -> None:
        ticket = {"id": "x", "itemsLine": [
            {"name": "A", "originalAmount": "1,00", "quantity": "1"},
        ]}
        r = parse_lidl_ticket(ticket)
        self.assertEqual(1.00, r.total_amount)

    def test_skips_unnamed_and_zero_lines(self) -> None:
        ticket = {"id": "x", "totalAmount": "1,00", "itemsLine": [
            {"name": "", "originalAmount": "5,00", "quantity": "1"},
            {"name": "Free sample", "originalAmount": "0,00", "quantity": "1"},
            {"name": "Real", "originalAmount": "1,00", "quantity": "1"},
        ]}
        r = parse_lidl_ticket(ticket)
        self.assertEqual(["Real"], [i.name for i in r.items])


if __name__ == "__main__":
    unittest.main()
