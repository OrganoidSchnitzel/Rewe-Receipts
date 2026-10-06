import unittest

from receipts.spliit import (
    Participant,
    SpliitError,
    build_expense_payload,
    resolve_payer,
)


class SpliitPayloadTests(unittest.TestCase):
    """The payload builder is money-critical: a bug here creates wrong real
    expenses. These tests pin the exact tRPC input shape."""

    def _payload(self, **overrides):
        base = dict(
            group_id="grp1",
            title="REWE 2026-09-02",
            amount_cents=448,
            payer_id="p1",
            participant_ids=["p1", "p2", "p3"],
        )
        base.update(overrides)
        return build_expense_payload(**base)

    def test_amount_stays_integer_cents(self) -> None:
        payload = self._payload()
        self.assertEqual(448, payload["expenseFormValues"]["amount"])
        self.assertIsInstance(payload["expenseFormValues"]["amount"], int)

    def test_split_mode_is_evenly_and_covers_all_participants(self) -> None:
        payload = self._payload()
        fv = payload["expenseFormValues"]
        self.assertEqual("EVENLY", fv["splitMode"])
        self.assertEqual(
            ["p1", "p2", "p3"], [pf["participant"] for pf in fv["paidFor"]]
        )
        # EVENLY ignores shares, but the schema requires a positive value.
        for pf in fv["paidFor"]:
            self.assertGreater(pf["shares"], 0)

    def test_payer_added_to_paidfor_when_missing(self) -> None:
        payload = self._payload(payer_id="pX", participant_ids=["p2", "p3"])
        fv = payload["expenseFormValues"]
        self.assertEqual("pX", fv["paidBy"])
        self.assertIn("pX", [pf["participant"] for pf in fv["paidFor"]])

    def test_top_level_shape(self) -> None:
        payload = self._payload()
        self.assertEqual("grp1", payload["groupId"])
        # participantId is optional in Spliit's schema and must be OMITTED
        # rather than sent as null: an optional (non-nullable) Zod field
        # rejects an explicit null, which previously caused a 400.
        self.assertNotIn("participantId", payload)
        self.assertEqual("REWE 2026-09-02", payload["expenseFormValues"]["title"])

    def test_rejects_non_positive_amount(self) -> None:
        with self.assertRaises(SpliitError):
            self._payload(amount_cents=0)

    def test_rejects_empty_participants(self) -> None:
        with self.assertRaises(SpliitError):
            self._payload(participant_ids=[])

    def test_expense_date_defaults_to_today_when_missing(self) -> None:
        import re

        payload = self._payload(expense_date=None)
        date = payload["expenseFormValues"]["expenseDate"]
        self.assertRegex(date, r"^\d{4}-\d{2}-\d{2}$")
        # A provided date is passed through untouched.
        payload = self._payload(expense_date="2026-07-17T00:00:00Z")
        self.assertEqual("2026-07-17T00:00:00Z", payload["expenseFormValues"]["expenseDate"])


class ResolvePayerTests(unittest.TestCase):
    parts = [Participant("p1", "Alice"), Participant("p2", "Bob")]

    def test_defaults_to_first(self) -> None:
        self.assertEqual("p1", resolve_payer(self.parts).id)

    def test_resolves_by_name(self) -> None:
        from receipts import config

        old = config.SPLIIT_PAYER_NAME
        config.SPLIIT_PAYER_NAME = "bob"
        try:
            self.assertEqual("p2", resolve_payer(self.parts).id)
        finally:
            config.SPLIIT_PAYER_NAME = old

    def test_raises_on_empty(self) -> None:
        with self.assertRaises(SpliitError):
            resolve_payer([])


if __name__ == "__main__":
    unittest.main()
