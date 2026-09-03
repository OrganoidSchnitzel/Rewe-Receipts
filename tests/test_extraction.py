import unittest

from receipts.extraction import (
    extract_rewe_items,
    normalize_line,
    parse_lidl_receipt,
    parse_ollama_response,
    parse_rewe_line,
)


class ReweRegexTests(unittest.TestCase):
    def test_extracts_priced_items(self) -> None:
        receipt = """
BANANEN 1,99 A
BROT 2,49 A
SUMME 4,48
"""
        items = extract_rewe_items(receipt)
        self.assertEqual(2, len(items))
        self.assertEqual("BANANEN", items[0].name)
        self.assertEqual(1.99, items[0].total_price)
        self.assertEqual("BROT", items[1].name)
        self.assertEqual(2.49, items[1].total_price)

    def test_ignores_payment_and_bonus_lines(self) -> None:
        receipt = """
RFW SALAMI FENCH 3,59 A
KERRYGOLD CHEDD. 1,59 A
Geg. VISA EUR 13,27
TSE-Start: 2026-07-17T19:13: 43,00
Mit diesem Einkauf hast du 0,50
Eingesetztes Bonus-Guthaben: 3,04
SUMME 5,18
"""
        items = extract_rewe_items(receipt)
        names = [i.name for i in items]
        self.assertEqual(["RFW SALAMI FENCH", "KERRYGOLD CHEDD."], names)

    def test_quantity_prefix_sets_quantity_and_unit_price(self) -> None:
        item = parse_rewe_line("2 x APFEL 3,00 A")
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual("APFEL", item.name)
        self.assertEqual(2.0, item.quantity)
        self.assertEqual(3.00, item.total_price)
        self.assertEqual(1.50, item.unit_price)

    def test_non_priced_line_returns_none(self) -> None:
        self.assertIsNone(parse_rewe_line("VIELEN DANK FUER IHREN EINKAUF"))

    def test_stops_at_summe_and_ignores_cashback_below(self) -> None:
        receipt = """
SKYR NATUR              3,87 B
JODSALZ                 0,79 B
SUMME              EUR   16,31
Geg. VISA            EUR  13,27
10% auf REWE Beste Wahl      0,30
0,10EUR auf REWE Beste       0,10
"""
        items = extract_rewe_items(receipt)
        self.assertEqual(["SKYR NATUR", "JODSALZ"], [i.name for i in items])

    def test_quantity_breakdown_merges_into_previous_item(self) -> None:
        # "3 Stk x 1,29" is a sub-line of SKYR NATUR, not a separate article.
        receipt = """
SKYR NATUR              3,87 B
        3 Stk x    1,29
PIZZA CL.TEX-MEX        2,19 B
"""
        items = extract_rewe_items(receipt)
        self.assertEqual(["SKYR NATUR", "PIZZA CL.TEX-MEX"], [i.name for i in items])
        skyr = items[0]
        self.assertEqual(3.0, skyr.quantity)
        self.assertEqual(1.29, skyr.unit_price)
        self.assertEqual(3.87, skyr.total_price)  # own total preserved

    def test_weight_breakdown_is_not_an_item(self) -> None:
        self.assertIsNone(parse_rewe_line("0,234 kg x 5,99 EUR/kg"))
        self.assertIsNone(parse_rewe_line("3 Stk x 1,29"))


class KnownItemsTests(unittest.TestCase):
    def test_known_item_overrides_name_deterministically(self) -> None:
        known = {normalize_line("RFW SALAMI FENCH 3,59 A"): {
            "canonical_name": "Rügenwalder Salami", "price_rule": None}}
        items = extract_rewe_items("RFW SALAMI FENCH 3,59 A", known_items=known)
        self.assertEqual(1, len(items))
        self.assertEqual("Rügenwalder Salami", items[0].name)
        self.assertEqual("known", items[0].source_method)
        self.assertEqual(3.59, items[0].total_price)

    def test_normalize_line_strips_price_and_uppercases(self) -> None:
        self.assertEqual("RFW SALAMI FENCH", normalize_line("rfw salami fench 3,59 A"))
        self.assertEqual(
            normalize_line("BROT   2,49 A"), normalize_line("brot 2,49 B")
        )


class LidlTests(unittest.TestCase):
    def test_parse_lidl_cents(self) -> None:
        data = {"lineItems": [
            {"name": "Bananas", "totalPrice": {"value": 199}},
            {"name": "Bread", "totalPrice": {"value": 249}, "quantity": 1},
        ]}
        items = parse_lidl_receipt(data)
        self.assertEqual(2, len(items))
        self.assertEqual(1.99, items[0].total_price)
        self.assertEqual("lidl", items[0].source_method)

    def test_parse_lidl_skips_zero_and_unnamed(self) -> None:
        data = {"lineItems": [
            {"name": "", "totalPrice": {"value": 100}},
            {"name": "Free", "totalPrice": {"value": 0}},
        ]}
        self.assertEqual([], parse_lidl_receipt(data))


class OllamaParsingTests(unittest.TestCase):
    def test_parses_plain_json_array(self) -> None:
        raw = '[{"name": "Milk", "quantity": 1, "unit_price": 1.29, "total_price": 1.29}]'
        items = parse_ollama_response(raw)
        self.assertEqual(1, len(items))
        self.assertEqual("Milk", items[0].name)
        self.assertEqual(1.29, items[0].total_price)
        self.assertEqual("ollama", items[0].source_method)

    def test_parses_object_wrapped_array(self) -> None:
        raw = '{"items": [{"name": "Eggs", "total_price": 2.49}]}'
        items = parse_ollama_response(raw)
        self.assertEqual(1, len(items))
        self.assertEqual("Eggs", items[0].name)

    def test_handles_garbage(self) -> None:
        self.assertEqual([], parse_ollama_response("not json at all"))
        self.assertEqual([], parse_ollama_response(""))


if __name__ == "__main__":
    unittest.main()
