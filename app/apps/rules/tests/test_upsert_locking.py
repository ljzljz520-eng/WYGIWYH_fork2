import threading
import time
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TransactionTestCase

from apps.accounts.models import Account
from apps.common.middleware.thread_local import (
    delete_current_user,
    write_current_user,
)
from apps.currencies.models import Currency
from apps.rules.models import (
    RuleExecution,
    TransactionRule,
    UpdateOrCreateTransactionRuleAction,
)
from apps.rules.services.exceptions import RetryableEventError
from apps.rules.services.executor import execute_plan
from apps.rules.services.planner import build_plan
from apps.transactions.models import Transaction


class UpsertLockingTestBase(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="locking@example.com", password="testpass123"
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

    def make_rule_with_hit_action(self, *, hit_description="rent",
                                   set_amount="decimal('5.00')"):
        rule = TransactionRule.objects.create(
            name="hit", trigger="is_expense", owner=self.user
        )
        UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description=f"'{hit_description}'",
            set_account="account_id",
            set_amount=set_amount,
            set_date="date",
            set_reference_date="reference_date",
            set_description=f"'{hit_description}-updated'",
        )
        return rule

    def attempt_execution(self, rule, source_tx):
        """Full claim+plan+execute attempt; raise RetryableEventError to caller."""
        with transaction.atomic():
            execution = RuleExecution.objects.create(
                transaction=source_tx,
                transaction_ref=source_tx.id,
                rule=rule,
                rule_ref=rule.id,
                event=RuleExecution.Event.CREATED,
                transaction_version=source_tx.version,
                rule_version=rule.version,
                status=RuleExecution.Status.PENDING,
                created_by=self.user,
            )
            plan = build_plan(
                rule=rule,
                transaction=source_tx,
                event=RuleExecution.Event.CREATED,
            )
            execute_plan(
                execution=execution,
                plan=plan,
                rule=rule,
                trigger=source_tx,
            )


class NarrowUpsertLockTests(UpsertLockingTestBase):
    def test_nowait_conflict_is_retryable_then_serial_success(self):
        target = self.make_tx(description="rent")
        source = self.make_tx(description="source")
        rule = self.make_rule_with_hit_action()

        # Holder thread takes a plain row lock on the target
        ready = threading.Event()
        proceed = threading.Event()
        holder_errors = []

        def holder():
            write_current_user(self.user)
            try:
                with transaction.atomic():
                    list(
                        Transaction.objects.select_for_update().filter(
                            id=target.id
                        )
                    )
                    ready.set()
                    proceed.wait(timeout=10)
            except Exception as exc:  # noqa: BLE001
                holder_errors.append(exc)
            finally:
                delete_current_user()
                connection.close()

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(ready.wait(timeout=5))

        # NOWAIT attempt must raise RetryableEventError and leave no rows
        try:
            self.attempt_execution(rule, source)
            conflict = None
        except RetryableEventError as exc:
            conflict = exc

        self.assertIsNotNone(conflict)
        self.assertEqual(
            RuleExecution.objects.filter(rule_ref=rule.id).count(), 0
        )
        target.refresh_from_db()
        self.assertEqual(target.amount, Decimal("10.00"))
        self.assertEqual(target.description, "rent")

        # Release → serial attempt succeeds on the single target row
        proceed.set()
        thread.join(timeout=10)

        self.attempt_execution(rule, source)

        self.assertEqual(Transaction.objects.count(), 2)
        target.refresh_from_db()
        self.assertEqual(target.description, "rent-updated")
        self.assertEqual(target.amount, Decimal("5.00"))
        self.assertEqual(holder_errors, [])

    def test_create_race_internal_id_conflict_is_retryable(self):
        source = self.make_tx(description="source")

        rules = []
        for name in ("race-a", "race-b"):
            rule = TransactionRule.objects.create(
                name=name, trigger="is_expense", owner=self.user
            )
            UpdateOrCreateTransactionRuleAction.objects.create(
                rule=rule,
                search_description="'no-such-description-xyz'",
                set_account="account_id",
                set_amount="decimal('5.00')",
                set_date="date",
                set_reference_date="reference_date",
                set_internal_id="'shared-internal-id'",
            )
            rules.append(rule)

        start = threading.Barrier(2, timeout=10)
        results = []

        def racer(rule):
            write_current_user(self.user)
            try:
                start.wait()
                self.attempt_execution(rule, source)
                results.append("ok")
            except RetryableEventError:
                results.append("retry")
            except Exception as exc:  # noqa: BLE001
                results.append(f"other:{type(exc).__name__}:{exc}")
            finally:
                delete_current_user()
                connection.close()

        threads = [
            threading.Thread(target=racer, args=(rule,)) for rule in rules
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        # Exactly one derived transaction with the shared internal id
        shared = Transaction.objects.filter(internal_id="shared-internal-id")
        self.assertEqual(shared.count(), 1)

        # One attempt succeeded; the other was reported retryable
        # (unique conflict is deterministic even without perfect overlap).
        self.assertIn("ok", results)
        self.assertIn("retry", results)
        self.assertTrue(all(not r.startswith("other:") for r in results))
