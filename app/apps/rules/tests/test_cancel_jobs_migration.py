import importlib.util
import pathlib

from django.db import connection, migrations as dj_migrations
from django.test import TransactionTestCase

_MIGRATION_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations"
    / "0021_cancel_legacy_rule_jobs.py"
)


def _load_operation():
    spec = importlib.util.spec_from_file_location(
        "cancel_legacy_rule_jobs_migration", _MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Migration.operations[0]


def _insert_job(cursor, job_id, task_name, status):
    cursor.execute(
        """
        INSERT INTO procrastinate_jobs
            (id, queue_name, task_name, args, status, attempts)
        VALUES (%s, 'default', %s, '{}', %s, 0)
        """,
        [job_id, task_name, status],
    )


def _statuses(cursor):
    cursor.execute(
        """
        SELECT id, status FROM procrastinate_jobs ORDER BY id
        """
    )
    return dict(cursor.fetchall())


class CancelLegacyJobsMigrationTests(TransactionTestCase):
    def apply(self, direction):
        operation = _load_operation()
        state = dj_migrations.state.ProjectState()
        with connection.schema_editor() as editor:
            if direction == "forward":
                operation.database_forwards("rules", editor, state, state)
            else:
                operation.database_backwards("rules", editor, state, state)

    def test_todo_jobs_cancelled_doing_kept_other_task_kept(self):
        with connection.cursor() as cursor:
            _insert_job(cursor, 101, "check_for_transaction_rules", "todo")
            _insert_job(cursor, 102, "check_for_transaction_rules", "todo")
            _insert_job(cursor, 103, "check_for_transaction_rules", "doing")
            _insert_job(cursor, 104, "some_other_task", "todo")

        self.apply("forward")

        with connection.cursor() as cursor:
            statuses = _statuses(cursor)
        self.assertEqual(statuses[101], "cancelled")
        self.assertEqual(statuses[102], "cancelled")
        self.assertEqual(statuses[103], "doing")
        self.assertEqual(statuses[104], "todo")

        self.apply("backward")

        with connection.cursor() as cursor:
            statuses = _statuses(cursor)
        self.assertEqual(statuses[101], "todo")
        self.assertEqual(statuses[102], "todo")
        self.assertEqual(statuses[103], "doing")
        self.assertEqual(statuses[104], "todo")
