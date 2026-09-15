import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from receipts import config, spliit
from receipts.ingest import compute_participant_cents
from receipts.models import ExtractedItem


def _item(total, assignees):
    return SimpleNamespace(total_price=total, assignees=assignees)


class ApportionTests(unittest.TestCase):
    def test_single_assignee_gets_full_amount(self) -> None:
        c = compute_participant_cents([_item(1.00, ["a"])], ["a", "b"])
        self.assertEqual({"a": 100, "b": 0}, c)

    def test_split_evenly_between_two(self) -> None:
        c = compute_participant_cents([_item(1.00, ["a", "b"])], ["a", "b"])
        self.assertEqual({"a": 50, "b": 50}, c)

    def test_odd_cent_remainder_is_distributed(self) -> None:
        c = compute_participant_cents([_item(1.01, ["a", "b"])], ["a", "b"])
        self.assertEqual(101, c["a"] + c["b"])
        self.assertEqual({51, 50}, {c["a"], c["b"]})

    def test_unassigned_item_splits_among_all(self) -> None:
        c = compute_participant_cents([_item(1.00, [])], ["a", "b"])
        self.assertEqual({"a": 50, "b": 50}, c)

    def test_unknown_assignee_ignored(self) -> None:
        c = compute_participant_cents([_item(1.00, ["a", "ghost"])], ["a", "b"])
        self.assertEqual({"a": 100, "b": 0}, c)

    def test_totals_sum_exactly(self) -> None:
        items = [_item(0.78, ["a"]), _item(3.39, ["a", "b"]), _item(3.41, ["b"])]
        c = compute_participant_cents(items, ["a", "b"])
        self.assertEqual(78 + 339 + 341, c["a"] + c["b"])


class ByAmountPayloadTests(unittest.TestCase):
    def test_shape_and_amount(self) -> None:
        p = spliit.build_amount_payload(
            group_id="g", title="T", payer_id="a",
            participant_cents={"a": 150, "b": 50},
        )
        fv = p["expenseFormValues"]
        self.assertEqual("BY_AMOUNT", fv["splitMode"])
        self.assertEqual(200, fv["amount"])
        shares = {pf["participant"]: pf["shares"] for pf in fv["paidFor"]}
        self.assertEqual({"a": 150, "b": 50}, shares)

    def test_zero_shares_dropped(self) -> None:
        p = spliit.build_amount_payload(
            group_id="g", title="T", payer_id="a",
            participant_cents={"a": 100, "b": 0},
        )
        fv = p["expenseFormValues"]
        self.assertEqual(100, fv["amount"])
        self.assertEqual(["a"], [pf["participant"] for pf in fv["paidFor"]])

    def test_negative_share_rejected(self) -> None:
        with self.assertRaises(spliit.SpliitError):
            spliit.build_amount_payload(
                group_id="g", title="T", payer_id="a",
                participant_cents={"a": 100, "b": -20},
            )

    def test_all_zero_rejected(self) -> None:
        with self.assertRaises(spliit.SpliitError):
            spliit.build_amount_payload(
                group_id="g", title="T", payer_id="a", participant_cents={"a": 0},
            )


class SettleAdvancedTests(unittest.TestCase):
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

    def _receipt_with_assignments(self):
        rid = self.db.create_receipt(
            source="lidl", external_id="lidl:t1", store="Lidl", total_amount=2.00,
            items=[ExtractedItem(name="A", total_price=1.00),
                   ExtractedItem(name="B", total_price=1.00)],
        )
        # A -> alice only; B -> split
        self.db.replace_items(rid, [
            {"name": "A", "quantity": 1, "unit_price": 1.0, "total_price": 1.0,
             "included": True, "assignees": ["p_alice"]},
            {"name": "B", "quantity": 1, "unit_price": 1.0, "total_price": 1.0,
             "included": True, "assignees": ["p_alice", "p_bob"]},
        ])
        return rid

    def test_advanced_settlement_builds_per_person_amounts(self) -> None:
        rid = self._receipt_with_assignments()
        parts = [spliit.Participant("p_alice", "Alice"), spliit.Participant("p_bob", "Bob")]
        with mock.patch.object(self.ingest.spliit, "get_participants", return_value=parts), \
             mock.patch.object(self.ingest.spliit, "resolve_payer", return_value=parts[0]), \
             mock.patch.object(self.ingest.spliit, "create_expense_by_amounts",
                               return_value="exp_1") as ce:
            ok, msg = self.ingest.settle_receipt_advanced(rid)

        self.assertTrue(ok)
        # A(100)->alice, B(100) split -> alice 50 / bob 50  => alice 150, bob 50
        self.assertEqual({"p_alice": 150, "p_bob": 50},
                         ce.call_args.kwargs["participant_cents"])
        self.assertEqual("p_alice", ce.call_args.kwargs["payer_id"])
        self.assertIn("Alice €1.50", msg)
        self.assertIn("Bob €0.50", msg)
        self.assertEqual("settled", self.db.get_receipt(rid).status)

    def test_assignees_persist_through_reload(self) -> None:
        rid = self._receipt_with_assignments()
        items = self.db.get_receipt(rid).items
        self.assertEqual(["p_alice"], items[0].assignees)
        self.assertEqual(["p_alice", "p_bob"], items[1].assignees)

    def test_advanced_idempotent(self) -> None:
        rid = self._receipt_with_assignments()
        parts = [spliit.Participant("p_alice", "Alice"), spliit.Participant("p_bob", "Bob")]
        with mock.patch.object(self.ingest.spliit, "get_participants", return_value=parts), \
             mock.patch.object(self.ingest.spliit, "resolve_payer", return_value=parts[0]), \
             mock.patch.object(self.ingest.spliit, "create_expense_by_amounts",
                               return_value="exp_1") as ce:
            self.ingest.settle_receipt_advanced(rid)
            ok, msg = self.ingest.settle_receipt_advanced(rid)
        self.assertFalse(ok)
        self.assertIn("Already settled", msg)
        ce.assert_called_once()


if __name__ == "__main__":
    unittest.main()
