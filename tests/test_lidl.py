import unittest

from receipts.lidl import parse_amount, parse_lidl_html, parse_lidl_ticket, reconciles


# A realistic German v3 htmlPrintedReceipt block: each visual line is a group of
# spans sharing a purchase_list_line id; article lines carry data-art-*, and the
# discount lines that follow hold a negative amount to subtract.
_BLOCK = """
<span id="purchase_list_line_2" class="article css_bold" data-art-id="0080000"
      data-art-quantity="0,638" data-unit-price="1,29" data-tax-type="A"
      data-art-description="Banane lose">Banane lose</span><span
      id="purchase_list_line_2" class="currency css_bold">0,82</span>
<span id="purchase_list_line_3" class="article css_bold" data-art-id="0080000"
      data-art-quantity="0,638" data-unit-price="1,29" data-tax-type="A"
      data-art-description="Banane lose">0,638 kg x 1,29 EUR/kg</span>
<span id="purchase_list_line_4" class="discount css_bold"
      data-promotion-id="PROMO-1">Lidl Plus Rabatt</span><span
      id="purchase_list_line_4" class="discount css_bold">-0,04</span>
<span id="purchase_list_line_5" class="article css_bold" data-art-id="0090000"
      data-art-quantity="1" data-unit-price="3,69" data-tax-type="A"
      data-art-description="Orangen">Orangen</span>
<span id="purchase_list_line_6">Preisvorteil</span><span
      id="purchase_list_line_6">-0,10</span>
<span id="purchase_list_line_7" class="discount css_bold"
      data-promotion-id="PROMO-1">Lidl Plus Rabatt</span><span
      id="purchase_list_line_7" class="discount css_bold">-0,20</span>
"""

# The real HTML repeats the block; the parser must use only the first copy.
SAMPLE_HTML = f'<div class="purchase_list">{_BLOCK}{_BLOCK}</div>'

HTML_TICKET = {
    "id": "23001771842026091273976",
    "date": "2026-09-12T15:33:28+00:00",
    # Banane 0.82-0.04=0.78 ; Orangen 3.69-0.10-0.20=3.39 ; sum 4.17
    "totalAmount": 4.17,
    "store": {"name": "Hamburg", "locality": "Hamburg"},
    "htmlPrintedReceipt": SAMPLE_HTML,
}


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


class HtmlTicketTests(unittest.TestCase):
    def test_deduplicates_and_uses_first_block_only(self) -> None:
        # The block is repeated; only two distinct items must come out.
        items = parse_lidl_html(SAMPLE_HTML)
        self.assertEqual(["Banane lose", "Orangen"], [i.name for i in items])

    def test_weight_line_net_after_discount(self) -> None:
        banane = parse_lidl_html(SAMPLE_HTML)[0]
        self.assertEqual(0.638, banane.quantity)
        self.assertEqual(1.29, banane.unit_price)  # gross unit (per kg) retained
        self.assertEqual(0.78, banane.total_price)  # 0.82 gross - 0.04 discount

    def test_multiple_discounts_on_one_item(self) -> None:
        orangen = parse_lidl_html(SAMPLE_HTML)[1]
        # 3.69 gross - 0.10 Preisvorteil - 0.20 Lidl Plus = 3.39
        self.assertEqual(3.39, orangen.total_price)

    def test_ticket_reconciles_without_residual_line(self) -> None:
        r = parse_lidl_ticket(HTML_TICKET)
        self.assertEqual("lidl:23001771842026091273976", r.external_id)
        self.assertEqual(4.17, r.total_amount)
        self.assertEqual(["Banane lose", "Orangen"], [i.name for i in r.items])
        self.assertTrue(reconciles(r))
        self.assertEqual("Lidl Hamburg", r.store)

    def test_residual_line_added_only_if_unreconciled(self) -> None:
        # Pretend the paid total is lower than the itemized net (missed discount).
        ticket = dict(HTML_TICKET, totalAmount=4.00)
        r = parse_lidl_ticket(ticket)
        self.assertEqual("Weitere Rabatte", r.items[-1].name)
        self.assertEqual(-0.17, r.items[-1].total_price)  # 4.17 - 4.00
        self.assertTrue(reconciles(r))


if __name__ == "__main__":
    unittest.main()
