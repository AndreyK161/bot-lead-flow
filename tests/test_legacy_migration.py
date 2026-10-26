import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.manager_planner.store import Store
from scripts.import_legacy_planner_data import ImportRejected, run
from scripts.migrate_legacy_planner import DATABASES, snapshot


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _database(self, kind: str) -> Path:
        path = self.root / f'{kind}-source.sqlite3'
        connection = sqlite3.connect(path)
        for table in DATABASES[kind]['tables']:
            connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
            connection.execute(f'INSERT INTO "{table}" VALUES (1)')
        connection.execute('PRAGMA user_version=4')
        connection.commit()
        connection.close()
        return path

    def test_snapshot_is_complete_and_does_not_modify_source(self):
        source = self._database('planner')
        before = source.read_bytes()
        destination = self.root / 'snapshot.sqlite3'
        result = snapshot('planner', source, destination)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(result['schema_version'], 4)
        self.assertEqual(result['counts']['tasks'], 1)
        self.assertEqual(len(result['sha256']), 64)
        self.assertTrue(destination.is_file())

    def test_refuses_database_with_missing_schema(self):
        source = self.root / 'invalid.sqlite3'
        sqlite3.connect(source).close()
        with self.assertRaises(RuntimeError):
            snapshot('access', source, self.root / 'snapshot.sqlite3')

    def _planner_pair(self):
        source = self.root / 'legacy-planner.sqlite3'
        target = self.root / 'lead-flow-planner.sqlite3'
        Store(source).initialize()
        Store(target).initialize()
        with sqlite3.connect(source) as db:
            db.execute("INSERT INTO plans(id,employee_id,day,status,initial_snapshot,created_at,approved_at,approved_by) VALUES(7,2,'2026-10-13','approved',?,1,2,2)",
                       (json.dumps({'id': 7, 'employee_id': 2, 'tasks': []}),))
            db.execute("INSERT INTO tasks(id,plan_id,employee_id,title,description,kind,planned_minutes,target,created_by,created_at) VALUES(9,7,2,'Позвонить','','general',15,NULL,2,1)")
            db.execute("INSERT INTO goal_requests(id,task_id,employee_id,field,old_value,proposed,reason,status,created_at,decided_at,decided_by) VALUES(3,9,2,'planned_minutes',15,20,'Нужно время','approved',1,2,5)")
            db.execute("INSERT INTO audit(id,plan_id,entity,entity_id,actor_id,action,after_json,created_at) VALUES(11,7,'task',9,2,'task_created',?,1)",
                       (json.dumps({'id': 9, 'employee_id': 2, 'created_by': 2}),))
            db.execute("INSERT INTO reports(plan_id,snapshot,closed_by,created_at) VALUES(7,?,5,3)",
                       (json.dumps({'id': 7, 'employee_id': 2, 'tasks': [{'id': 9, 'employee_id': 2, 'created_by': 2}]}),))
            db.execute('PRAGMA user_version=4')
        # Make the destination ids intentionally differ from the legacy ids.
        with sqlite3.connect(target) as db:
            db.execute('UPDATE employee_aliases SET employee_id=20 WHERE employee_id=2')
            db.execute('UPDATE employees SET id=20 WHERE id=2')
            db.execute('UPDATE employees SET id=50 WHERE id=5')
        return source, target

    def test_imports_core_history_and_rewrites_employee_references(self):
        source, target = self._planner_pair()
        preview = run(source, target)
        self.assertEqual(preview['status'], 'ready')
        with sqlite3.connect(target) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM plans').fetchone()[0], 0)

        result = run(source, target, apply=True)
        self.assertEqual(result['status'], 'imported')
        with sqlite3.connect(target) as db:
            db.row_factory = sqlite3.Row
            plan = db.execute('SELECT * FROM plans WHERE id=7').fetchone()
            task = db.execute('SELECT * FROM tasks WHERE id=9').fetchone()
            request = db.execute('SELECT * FROM goal_requests WHERE id=3').fetchone()
            report = json.loads(db.execute('SELECT snapshot FROM reports WHERE plan_id=7').fetchone()[0])
            self.assertEqual(plan['employee_id'], 20)
            self.assertEqual(plan['approved_by'], 20)
            self.assertEqual(task['employee_id'], 20)
            self.assertEqual(task['created_by'], 20)
            self.assertEqual(request['employee_id'], 20)
            self.assertEqual(request['decided_by'], 50)
            self.assertEqual(report['employee_id'], 20)
            self.assertEqual(report['tasks'][0]['created_by'], 20)

        repeated = run(source, target, apply=True)
        self.assertEqual(repeated['status'], 'already_imported')

    def test_refuses_import_into_nonempty_target(self):
        source, target = self._planner_pair()
        with sqlite3.connect(target) as db:
            db.execute("INSERT INTO plans(employee_id,day,created_at) VALUES(1,'2026-10-21',1)")
        with self.assertRaises(ImportRejected):
            run(source, target, apply=True)


if __name__ == '__main__':
    unittest.main()
