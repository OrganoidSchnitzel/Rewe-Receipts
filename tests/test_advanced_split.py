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

    # The REWE receipt from 06.10.2026: 21 items, 17 with an odd-cent price.
    REWE_0610 = [2.89, 1.65, 2.29, 2.29, 2.49, 1.49, 1.41, 1.99, 1.60, 1.99, 6.58,
                 1.35, 0.99, 2.58, 0.89, 1.58, 3.49, 1.49, 0.55, 1.39, 4.99]

    def test_even_split_does_not_drift_on_many_odd_cent_items(self) -> None:
        # Regression: per-item rounding gave every odd cent to the first person
        # (Tobi €23.07 / Chiara €22.90 instead of €22.99 / €22.98).
        items = [_item(p, ["a", "b"]) for p in self.REWE_0610]
        c = compute_participant_cents(items, ["a", "b"])
        self.assertEqual(4597, c["a"] + c["b"])
        self.assertEqual({2299, 2298}, {c["a"], c["b"]})

    def test_unassigned_items_split_evenly_too(self) -> None:
        items = [_item(p, []) for p in self.REWE_0610]
        c = compute_participant_cents(items, ["a", "b"])
        self.assertEqual({2299, 2298}, {c["a"], c["b"]})

    def test_three_way_split_is_within_a_cent(self) -> None:
        items = [_item(p, []) for p in self.REWE_0610]
        c = compute_participant_cents(items, ["a", "b", "c"])
        self.assertEqual(4597, sum(c.values()))
        self.assertLessEqual(max(c.values()) - min(c.values()), 1)

    def test_mixed_two_and_three_way_items(self) -> None:
        items = [_item(1.00, ["a", "b", "c"]), _item(0.01, ["a", "b"]), _item(2.00, ["c"])]
        c = compute_participant_cents(items, ["a", "b", "c"])
        self.assertEqual(301, sum(c.values()))
        # exact: a = b = 33.83…, c = 233.33… -> c keeps its 2.00 + a third
        self.assertEqual(233, c["c"])
        self.assertEqual({34, 34}, {c["a"], c["b"]})

    def test_negative_line_still_sums_exactly(self) -> None:
        # e.g. a Pfand return or discount line
        items = [_item(3.01, ["a", "b"]), _item(-0.25, ["a", "b"]), _item(1.99, ["a"])]
        c = compute_participant_cents(items, ["a", "b"])
        self.assertEqual(301 - 25 + 199, c["a"] + c["b"])
        self.assertEqual(138 + 199, c["a"])  # (301 - 25) / 2 = 138 each, + 1.99

    def test_duplicate_assignee_counted_once(self) -> None:
        c = compute_participant_cents([_item(1.00, ["a", "a", "b"])], ["a", "b"])
        self.assertEqual({"a": 50, "b": 50}, c)


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
            ok, msg = self.ingest.settle_receipt(rid)

        self.assertTrue(ok)
        # A(100)->alice, B(100) split -> alice 50 / bob 50  => alice 150, bob 50
        self.assertEqual({"p_alice": 150, "p_bob": 50},
                         ce.call_args.kwargs["participant_cents"])
        self.assertEqual("p_alice", ce.call_args.kwargs["payer_id"])
        self.assertIn("Alice €1.50", msg)
        self.assertIn("Bob €0.50", msg)
        self.assertEqual("settled", self.db.get_receipt(rid).status)

    def test_saved_assignments_drive_the_one_settle(self) -> None:
        # The single "Create Spliit expense" button (and Telegram's Approve)
        # must use the saved per-item assignments, not a 50/50 split.
        rid = self._receipt_with_assignments()
        parts = [spliit.Participant("p_alice", "Alice"), spliit.Participant("p_bob", "Bob")]
        with mock.patch.object(self.ingest.spliit, "get_participants", return_value=parts), \
             mock.patch.object(self.ingest.spliit, "resolve_payer", return_value=parts[0]), \
             mock.patch.object(self.ingest.spliit, "create_expense") as even, \
             mock.patch.object(self.ingest.spliit, "create_expense_by_amounts",
                               return_value="exp_1") as by_amount:
            ok, _ = self.ingest.settle_receipt(rid)
        self.assertTrue(ok)
        even.assert_not_called()
        by_amount.assert_called_once()

    def test_no_assignments_is_an_even_split(self) -> None:
        rid = self.db.create_receipt(
            source="lidl", external_id="lidl:t2", store="Lidl", total_amount=2.00,
            items=[ExtractedItem(name="A", total_price=1.00),
                   ExtractedItem(name="B", total_price=1.00)],
        )
        with mock.patch.object(self.ingest.spliit, "get_participants") as gp, \
             mock.patch.object(self.ingest.spliit, "create_expense", return_value="e") as even, \
             mock.patch.object(self.ingest.spliit, "create_expense_by_amounts") as by_amount:
            ok, _ = self.ingest.settle_receipt(rid)
        self.assertTrue(ok)
        even.assert_called_once()
        by_amount.assert_not_called()
        gp.assert_not_called()  # no need to ask Spliit who's in the group

    def test_everyone_on_every_item_is_an_even_split(self) -> None:
        # Opening the advanced view ticks everyone on every item; saving that
        # unchanged must still give Spliit's plain "Evenly" expense.
        rid = self._receipt_with_assignments()
        self.db.replace_items(rid, [
            {"name": n, "total_price": 1.0, "included": True,
             "assignees": ["p_alice", "p_bob"]} for n in ("A", "B")
        ])
        parts = [spliit.Participant("p_alice", "Alice"), spliit.Participant("p_bob", "Bob")]
        with mock.patch.object(self.ingest.spliit, "get_participants", return_value=parts), \
             mock.patch.object(self.ingest.spliit, "create_expense", return_value="e") as even, \
             mock.patch.object(self.ingest.spliit, "create_expense_by_amounts") as by_amount:
            ok, _ = self.ingest.settle_receipt(rid)
        self.assertTrue(ok)
        even.assert_called_once()
        by_amount.assert_not_called()

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
            self.ingest.settle_receipt(rid)
            ok, msg = self.ingest.settle_receipt(rid)
        self.assertFalse(ok)
        self.assertIn("already settled", msg)
        ce.assert_called_once()


if __name__ == "__main__":
    unittest.main()
