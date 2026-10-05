from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from apps.accounts.models import Account
from apps.common.middleware.thread_local import (
    delete_current_user,
    write_current_user,
)
from apps.currencies.models import Currency
from apps.rules.jobs import process_transaction_event
from apps.rules.models import (
    RuleExecution,
    TransactionRule,
    UpdateOrCreateTransactionRuleAction,
)
from apps.transactions.models import Transaction


def run_worker(**kwargs):
    func = process_transaction_event.func
    func = getattr(func, "__wrapped__", func)
    return func(**kwargs)


class WorkerTestBase(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="worker@example.com", password="testpass123"
        )
        write_current_user(self.user)
        self.currency = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2
        )
        self.account = Account.objects.create(
            name="Main", currency=self.currency, owner=self.user
        )

    def tearDown(self):
        delete_current_user()

    def make_tx(self, amount="10.00", description="", **kwargs):
        defaults = {
            "account": self.account,
            "type": Transaction.Type.EXPENSE,
            "amount": Decimal(amount),
            "date": date(2026, 5, 4),
            "reference_date": date(2026, 5, 1),
            "description": description,
            "owner": self.user,
        }
        defaults.update(kwargs)
        return Transaction.objects.create(**defaults)

    def make_rule(self, order=0, trigger="is_expense", **kwargs):
        defaults = {
            "name": f"R{order}",
            "trigger": trigger,
            "owner": self.user,
            "order": order,
        }
        defaults.update(kwargs)
        return TransactionRule.objects.create(**defaults)

    def add_create_action(self, rule, description, amount="5.00"):
        return UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'no-such-description-xyz'",
            set_type="'EX'",
            set_date="date",
            set_reference_date="reference_date",
            set_account="account_id",
            set_amount=f"decimal('{amount}')",
            set_description=repr(description),
        )


class WorkerIdempotencyTests(WorkerTestBase):
    def test_replay_of_legacy_event_hits_existing_execution(self):
        tx = self.make_tx(description="source")
        rule = self.make_rule()
        self.add_create_action(rule, "already-derived")

        # Simulate state migrated with a pre-existing completed execution
        # and the derived transaction it had already produced
        derived = self.make_tx(description="already-derived")
        RuleExecution.objects.create(
            transaction_ref=tx.id,
            rule_ref=rule.id,
            transaction=tx,
            rule=rule,
            event=RuleExecution.Event.CREATED,
            transaction_version=tx.version,
            rule_version=rule.version,
            status=RuleExecution.Status.COMPLETED,
            created_by=self.user,
        )
        before = Transaction.objects.filter(
            description="already-derived"
        ).count()
        self.assertEqual(before, 1)

        results = run_worker(
            event=RuleExecution.Event.CREATED,
            transaction_ref=tx.id,
            transaction_version=tx.version,
            user_id=self.user.id,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0]["status"], RuleExecution.Status.COMPLETED
        )
        self.assertEqual(
            Transaction.objects.filter(
                description="already-derived"
            ).count(),
            1,
        )

    def test_duplicate_event_returns_same_completed_execution(self):
        tx = self.make_tx(description="source")
        rule = self.make_rule()
        self.add_create_action(rule, "derived-one")

        payload = {
            "event": RuleExecution.Event.CREATED,
            "transaction_ref": tx.id,
            "transaction_version": tx.version,
            "user_id": self.user.id,
        }

        first = run_worker(**payload)
        second = run_worker(**payload)

        self.assertEqual(RuleExecution.objects.count(), 1)
        derived = Transaction.objects.filter(description="derived-one")
        self.assertEqual(derived.count(), 1)

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["id"], first[0]["id"])
        self.assertEqual(second[0]["status"], RuleExecution.Status.COMPLETED)


class WorkerStalenessTests(WorkerTestBase):
    def stale_payload(self, tx, **kwargs):
        params = {
            "event": RuleExecution.Event.CREATED,
            "transaction_ref": tx.id,
            "transaction_version": tx.version,
            "user_id": self.user.id,
        }
        params.update(kwargs)
        return params

    def assert_single_stale(self, results, reason):
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], RuleExecution.Status.STALE)
        self.assertEqual(results[0]["detail"]["reason"], reason)
        self.assertEqual(RuleExecution.objects.count(), 1)

    def test_row_missing_is_stale(self):
        tx = self.make_tx()
        rule = self.make_rule()
        self.add_create_action(rule, "should-not-exist")
        tx_ref = tx.id
        tx.hard_delete()

        results = run_worker(
            event=RuleExecution.Event.CREATED,
            transaction_ref=tx_ref,
            transaction_version=1,
            user_id=self.user.id,
        )

        self.assert_single_stale(results, "row_missing")
        self.assertFalse(
            Transaction.objects.filter(description="should-not-exist").exists()
        )

    def test_soft_deleted_is_stale(self):
        tx = self.make_tx()
        rule = self.make_rule()
        self.add_create_action(rule, "should-not-exist")

        tx.deleted = True
        tx.save()

        results = run_worker(**self.stale_payload(tx))

        self.assert_single_stale(results, "soft_deleted")
        self.assertFalse(
            Transaction.objects.filter(description="should-not-exist").exists()
        )

    def test_newer_version_is_stale(self):
        tx = self.make_tx()
        rule = self.make_rule()
        self.add_create_action(rule, "should-not-exist")

        # Event captured at version 1, but the row has since moved to version 2
        tx.description = "edited again"
        tx.save()

        results = run_worker(**self.stale_payload(tx, transaction_version=1))

        self.assert_single_stale(results, "newer_version")
        self.assertFalse(
            Transaction.objects.filter(description="should-not-exist").exists()
        )

    def test_out_of_order_is_stale(self):
        tx = self.make_tx()
        rule = self.make_rule()
        self.add_create_action(rule, "from-newer-event")

        # A newer version of the same event was already processed
        RuleExecution.objects.create(
            transaction_ref=tx.id,
            rule_ref=rule.id,
            rule=rule,
            transaction=tx,
            event=RuleExecution.Event.CREATED,
            transaction_version=tx.version + 1,
            rule_version=rule.version,
            status=RuleExecution.Status.COMPLETED,
        )

        results = run_worker(**self.stale_payload(tx))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], RuleExecution.Status.STALE)
        self.assertEqual(results[0]["detail"]["reason"], "out_of_order")
        self.assertFalse(
            Transaction.objects.filter(description="from-newer-event").exists()
        )

    def test_hard_deleted_delete_event_executes_normally(self):
        tx = self.make_tx(description="gone")
        rule = self.make_rule(on_create=False, on_delete=True)
        self.add_create_action(rule, "cleanup-transaction")

        from apps.rules.utils.transactions import serialize_transaction

        snapshot = serialize_transaction(tx, deleted=True)
        tx.hard_delete()

        results = run_worker(
            event=RuleExecution.Event.DELETED,
            transaction_ref=snapshot["id"],
            transaction_version=1,
            user_id=self.user.id,
            transaction_data=snapshot,
            is_hard_deleted=True,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0]["status"], RuleExecution.Status.COMPLETED
        )
        self.assertTrue(
            Transaction.objects.filter(
                description="cleanup-transaction"
            ).exists()
        )


class WorkerRuleOrderingTests(WorkerTestBase):
    def test_rules_process_in_order_failures_and_skips_dont_block(self):
        tx = self.make_tx(description="source")

        rule_a = self.make_rule(order=1, name="A")
        self.add_create_action(rule_a, "derived-a")

        rule_b = self.make_rule(order=2, name="B")
        UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule_b,
            search_description="'no-such-description-xyz'",
            set_amount="1/0",
        )

        rule_c = self.make_rule(order=3, name="C", trigger="False")
        self.add_create_action(rule_c, "derived-c")

        rule_d = self.make_rule(order=4, name="D")
        self.add_create_action(rule_d, "derived-d")

        results = run_worker(
            event=RuleExecution.Event.CREATED,
            transaction_ref=tx.id,
            transaction_version=tx.version,
            user_id=self.user.id,
        )

        self.assertEqual(
            [item["rule_ref"] for item in results],
            [rule_a.id, rule_b.id, rule_c.id, rule_d.id],
        )
        self.assertEqual(
            [item["status"] for item in results],
            [
                RuleExecution.Status.COMPLETED,
                RuleExecution.Status.FAILED,
                RuleExecution.Status.SKIPPED,
                RuleExecution.Status.COMPLETED,
            ],
        )

        for description in ("derived-a", "derived-d"):
            self.assertTrue(
                Transaction.objects.filter(description=description).exists()
            )
        self.assertFalse(
            Transaction.objects.filter(description="derived-c").exists()
        )
