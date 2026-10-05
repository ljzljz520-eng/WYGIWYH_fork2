from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TransactionTestCase

from apps.accounts.models import Account
from apps.currencies.models import Currency
from apps.common.middleware.thread_local import (
    delete_current_user,
    write_current_user,
)
from apps.rules.models import (
    RuleActionExecution,
    RuleExecution,
    TransactionRule,
)
from apps.transactions.models import Transaction


class RuleExecutionModelTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="execution-models@example.com", password="testpass123"
        )
        write_current_user(self.user)
        self.currency = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2
        )
        self.account = Account.objects.create(
            name="Main Account", currency=self.currency, owner=self.user
        )
        self.transaction = Transaction.objects.create(
            account=self.account,
            type=Transaction.Type.EXPENSE,
            amount=Decimal("10.00"),
            date=date(2026, 5, 4),
            reference_date=date(2026, 5, 1),
            owner=self.user,
        )
        self.rule = TransactionRule.objects.create(
            name="R", trigger="True", owner=self.user
        )

    def tearDown(self):
        delete_current_user()

    def _create_execution(self, **kwargs):
        defaults = {
            "transaction": self.transaction,
            "transaction_ref": self.transaction.id,
            "rule": self.rule,
            "rule_ref": self.rule.id,
            "event": RuleExecution.Event.UPDATED,
            "transaction_version": self.transaction.version,
            "rule_version": self.rule.version,
            "status": RuleExecution.Status.COMPLETED,
            "created_by": self.user,
        }
        defaults.update(kwargs)
        return RuleExecution.objects.create(**defaults)

    def test_duplicate_key_is_rejected(self):
        self._create_execution()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._create_execution()

    def test_different_versions_coexist(self):
        self._create_execution()
        self._create_execution(
            transaction_version=self.transaction.version + 1,
        )
        self.assertEqual(RuleExecution.objects.count(), 2)

    def test_history_survives_rule_and_transaction_deletion(self):
        execution = self._create_execution()
        rule_id = self.rule.id
        transaction_id = self.transaction.id
        self.rule.delete()
        self.transaction.delete()

        execution.refresh_from_db()
        self.assertIsNone(execution.rule)
        self.assertIsNone(execution.transaction)
        self.assertEqual(execution.rule_ref, rule_id)
        self.assertEqual(execution.transaction_ref, transaction_id)

    def test_action_execution_relation_and_effects(self):
        execution = self._create_execution()
        action = RuleActionExecution.objects.create(
            rule_execution=execution,
            action_type=RuleActionExecution.ActionType.EDIT,
            action_ref=42,
            order=0,
            status=RuleActionExecution.Status.APPLIED,
            effects=[
                {
                    "kind": RuleActionExecution.EffectKind.MODIFY,
                    "target_ref": self.transaction.id,
                    "old": "x",
                    "new": "y",
                }
            ],
        )

        self.assertEqual(execution.action_executions.count(), 1)
        self.assertEqual(
            execution.action_executions.first().action_ref, action.action_ref
        )

    def test_transaction_provenance_column(self):
        execution = self._create_execution()
        action = RuleActionExecution.objects.create(
            rule_execution=execution,
            action_type=RuleActionExecution.ActionType.UPDATE_OR_CREATE,
            action_ref=7,
            order=0,
            status=RuleActionExecution.Status.APPLIED,
        )
        derived = Transaction.objects.create(
            account=self.account,
            type=Transaction.Type.EXPENSE,
            amount=Decimal("5.00"),
            date=date(2026, 5, 5),
            reference_date=date(2026, 5, 1),
            description="derived",
            generated_by_action_execution_id=action.id,
            owner=self.user,
        )

        derived.refresh_from_db()
        self.assertEqual(derived.generated_by_action_execution_id, action.id)
