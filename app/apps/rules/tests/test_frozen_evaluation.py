import time
from datetime import date, datetime, timezone

from django.core import signing
from django.test import SimpleTestCase
from simpleeval import EvalWithCompoundTypes

from apps.rules.services.evaluation import FrozenEvalContext

_FIXED_NOW = datetime(2026, 5, 4, 12, 30, 15, tzinfo=timezone.utc)
_FIXED_SEED = 123456789

_EXPRESSIONS = [
    "datetime.now()",
    "datetime.utcnow()",
    "datetime.today()",
    "date.today()",
    "random()",
    "random()",
    "randint(0, 1000000)",
    "randint(-500, 500)",
]


class FrozenEvalContextTests(SimpleTestCase):
    def _evaluate_sequence(self, context):
        results = []
        for expression in _EXPRESSIONS:
            simple = EvalWithCompoundTypes(functions=context.functions())
            results.append(simple.eval(expression))
        return results

    def test_same_token_reproduces_identical_sequence(self):
        context = FrozenEvalContext(_FIXED_NOW, _FIXED_SEED)
        token = context.issue_token()

        rebuilt_a = FrozenEvalContext.read_token(token)
        rebuilt_b = FrozenEvalContext.read_token(token)

        sequence_a = self._evaluate_sequence(rebuilt_a)
        sequence_b = self._evaluate_sequence(rebuilt_b)

        self.assertEqual(sequence_a, sequence_b)

        # Explicit spot checks on the frozen time values
        self.assertEqual(rebuilt_a.datetime.now(), _FIXED_NOW)
        self.assertEqual(rebuilt_a.date.today(), date(2026, 5, 4))

    def test_different_seeds_diverge(self):
        context_a = FrozenEvalContext(_FIXED_NOW, _FIXED_SEED)
        context_b = FrozenEvalContext(_FIXED_NOW, _FIXED_SEED + 1)

        simple_a = EvalWithCompoundTypes(functions=context_a.functions())
        simple_b = EvalWithCompoundTypes(functions=context_b.functions())
        self.assertNotEqual(
            simple_a.eval("random()"), simple_b.eval("random()")
        )

    def test_tampered_token_is_rejected(self):
        token = FrozenEvalContext(_FIXED_NOW, _FIXED_SEED).issue_token()
        tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")

        with self.assertRaises(signing.BadSignature):
            FrozenEvalContext.read_token(tampered)

    def test_expired_token_is_rejected(self):
        token = FrozenEvalContext(_FIXED_NOW, _FIXED_SEED).issue_token()
        time.sleep(1.1)

        with self.assertRaises(signing.BadSignature):
            FrozenEvalContext.read_token(token, max_age=1)

    def test_existing_functions_still_available(self):
        context = FrozenEvalContext(_FIXED_NOW, _FIXED_SEED)
        simple = EvalWithCompoundTypes(functions=context.functions())

        self.assertEqual(
            simple.eval("datetime(2026, 1, 1) + relativedelta(days=2)"),
            datetime(2026, 1, 3),
        )
        self.assertEqual(
            simple.eval("date(2026, 1, 1) + relativedelta(months=1)"),
            date(2026, 2, 1),
        )
        self.assertEqual(simple.eval("decimal('1.5') + decimal('2.25')"), 3.75)
        self.assertEqual(simple.eval("abs(int(float('-3.5')))"), 3)
