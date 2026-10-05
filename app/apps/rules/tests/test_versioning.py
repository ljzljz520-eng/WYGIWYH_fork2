from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from apps.rules.models import TransactionRule


class TransactionRuleVersionTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="rule-versioning@example.com", password="testpass123"
        )

    def test_new_rule_starts_at_version_1(self):
        rule = TransactionRule.objects.create(
            name="R", trigger="True", owner=self.user
        )
        self.assertEqual(rule.version, 1)

    def test_save_bumps_version(self):
        rule = TransactionRule.objects.create(
            name="R", trigger="True", owner=self.user
        )
        rule.name = "R renamed"
        rule.save()

        rule.refresh_from_db()
        self.assertEqual(rule.name, "R renamed")
        self.assertEqual(rule.version, 2)

    def test_update_fields_bumps_version(self):
        rule = TransactionRule.objects.create(
            name="R", trigger="True", owner=self.user
        )
        rule.active = False
        rule.save(update_fields=["active"])

        rule.refresh_from_db()
        self.assertFalse(rule.active)
        self.assertEqual(rule.version, 2)
