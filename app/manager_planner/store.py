"""Transactional planner storage; never migrates the existing bot database."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
SCHEMA_VERSION = 8
EMPLOYEE_BINDINGS = (
    # Identities are copied into an isolated database. Actual login permission
    # is controlled separately by Lead Flow's planner_access registry.
    (1297686797, None, "Тестовый менеджер", "manager"),
    (8306778724, 460, "Вячеслав Шандра", "manager"),
    (7939114214, 110, "Яна", "manager"),
    (7874883320, 50, "Евгений", "manager"),
    (1073597301, None, "Руководитель", "supervisor"),
)
EMPLOYEE_ALIASES = ((399005637, 460),)


def today() -> str:
    return datetime.now(MOSCOW).date().isoformat()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class DomainError(Exception):
    def __init__(self, message: str, status: int = 409):
        self.message, self.status = message, status


SCHEMA = [
    """CREATE TABLE employees (
        id INTEGER PRIMARY KEY, telegram_id INTEGER UNIQUE CHECK(telegram_id > 0),
        bitrix_id INTEGER UNIQUE CHECK(bitrix_id > 0), name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('manager','supervisor')), active INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE plans (
        id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id), day TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','approved','closed')),
        initial_snapshot TEXT, created_at INTEGER NOT NULL, approved_at INTEGER, approved_by INTEGER REFERENCES employees(id),
        closed_at INTEGER, UNIQUE(employee_id, day), UNIQUE(id, employee_id))""",
    """CREATE TABLE tasks (
        id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL, employee_id INTEGER NOT NULL,
        title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL CHECK(kind IN ('quantitative','general','client','bitrix')),
        planned_minutes INTEGER NOT NULL DEFAULT 0 CHECK(planned_minutes >= 0), target REAL,
        actual_value REAL NOT NULL DEFAULT 0, actual_seconds INTEGER NOT NULL DEFAULT 0 CHECK(actual_seconds >= 0),
        timer_started INTEGER, status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo','in_progress','done','moved')),
        result TEXT NOT NULL DEFAULT '', comment TEXT NOT NULL DEFAULT '',
        created_by INTEGER NOT NULL REFERENCES employees(id), locked INTEGER NOT NULL DEFAULT 0,
        revision INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL,
        moved_to INTEGER REFERENCES tasks(id), source_task_id INTEGER REFERENCES tasks(id),
        external_id TEXT, external_url TEXT, external_status TEXT, work_done_today INTEGER NOT NULL DEFAULT 0,
        deadline_outcome TEXT CHECK(deadline_outcome IN ('work_done_and_moved','deadline_only')),
        FOREIGN KEY(plan_id, employee_id) REFERENCES plans(id, employee_id))""",
    "CREATE UNIQUE INDEX one_timer_per_employee ON tasks(employee_id) WHERE timer_started IS NOT NULL",
    "CREATE INDEX tasks_plan ON tasks(plan_id)",
    """CREATE TABLE goal_requests (
        id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
        employee_id INTEGER NOT NULL REFERENCES employees(id), field TEXT NOT NULL CHECK(field IN ('target','planned_minutes')),
        old_value REAL, proposed REAL NOT NULL, reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','cancelled')),
        created_at INTEGER NOT NULL, decided_at INTEGER, decided_by INTEGER REFERENCES employees(id), decision_comment TEXT NOT NULL DEFAULT '')""",
    "CREATE UNIQUE INDEX one_pending_goal ON goal_requests(task_id,field) WHERE status='pending'",
    """CREATE TABLE audit (
        id INTEGER PRIMARY KEY, plan_id INTEGER REFERENCES plans(id), entity TEXT NOT NULL, entity_id INTEGER NOT NULL,
        actor_id INTEGER NOT NULL REFERENCES employees(id), action TEXT NOT NULL,
        before_json TEXT, after_json TEXT, created_at INTEGER NOT NULL)""",
    "CREATE INDEX audit_plan ON audit(plan_id,id)",
    """CREATE TABLE reports (
        plan_id INTEGER PRIMARY KEY REFERENCES plans(id), snapshot TEXT NOT NULL, closed_by INTEGER NOT NULL REFERENCES employees(id),
        created_at INTEGER NOT NULL)""",
    "CREATE TRIGGER immutable_report_update BEFORE UPDATE ON reports BEGIN SELECT RAISE(ABORT,'Immutable report'); END",
    "CREATE TRIGGER immutable_report_delete BEFORE DELETE ON reports BEGIN SELECT RAISE(ABORT,'Immutable report'); END",
    "CREATE TRIGGER immutable_audit_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'Immutable audit'); END",
    "CREATE TRIGGER immutable_audit_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'Immutable audit'); END",
]


class Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self, write=False):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        # Versioned migration, exclusive transaction, rollback on any failure.
        # Refuse unknown databases; no destructive SQL against existing files.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        with self.connection(True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("База планировщика создана более новой версией.")
            if version == 0:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise RuntimeError("Для планировщика нужна отдельная пустая база.")
                for statement in SCHEMA:
                    db.execute(statement)
                db.executemany("INSERT INTO employees(telegram_id,bitrix_id,name,role) VALUES(?,?,?,?)", EMPLOYEE_BINDINGS)
                db.execute("PRAGMA user_version=1")
                version = 1
            if version == 1:
                # Fill only the missing binding from stage 1. Never replace a
                # nonempty identity, rename an employee, or create duplicate users.
                yana = db.execute("SELECT * FROM employees WHERE bitrix_id=110 AND role='manager'").fetchone()
                change = {"yana_binding": "preserved" if yana else "not_found"}
                if yana and yana["telegram_id"] is None:
                    if db.execute("SELECT 1 FROM employees WHERE telegram_id=7939114214").fetchone():
                        raise RuntimeError("Telegram ID Яны занят другой записью. Миграция отменена; разрешите конфликт сотрудников.")
                    db.execute("UPDATE employees SET telegram_id=? WHERE id=? AND telegram_id IS NULL", (7939114214, yana["id"]))
                    change = {"yana_binding": "filled", "employee_id": yana["id"], "old_telegram_id": None, "new_telegram_id": 7939114214}
                db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at INTEGER NOT NULL,details TEXT NOT NULL)")
                db.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (2, int(time.time()), encode(change)))
                db.execute("PRAGMA user_version=2")
                version = 2
            if version == 2:
                from .distribution_import import MIGRATION
                for statement in MIGRATION:
                    db.execute(statement)
                db.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (3, int(time.time()), encode({"distribution_inbox": "created"})))
                db.execute("PRAGMA user_version=3")
                version = 3
            if version == 3:
                db.execute("CREATE TABLE import_attempts(id TEXT PRIMARY KEY,status TEXT NOT NULL CHECK(status IN ('imported','review','error')),attempts INTEGER NOT NULL,last_attempt INTEGER NOT NULL,error_code TEXT NOT NULL)")
                db.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (4, int(time.time()), encode({"import_attempts": "created"})))
                db.execute("PRAGMA user_version=4")
                version = 4
            if version == 4:
                db.execute("CREATE INDEX IF NOT EXISTS external_tasks_id ON tasks(external_id) WHERE external_id IS NOT NULL")
                db.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (5, int(time.time()), encode({"external_tasks_index": "created"})))
                db.execute("PRAGMA user_version=5")
                version = 5
            if version == 5:
                db.execute(
                    "CREATE TABLE legacy_imports("
                    "source_sha256 TEXT PRIMARY KEY,imported_at INTEGER NOT NULL,manifest_json TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT INTO schema_migrations VALUES(?,?,?)",
                    (6, int(time.time()), encode({"legacy_planner_import_log": "created"})),
                )
                db.execute("PRAGMA user_version=6")
                version = 6
            if version == 6:
                db.execute(
                    "CREATE TABLE employee_aliases("
                    "telegram_id INTEGER PRIMARY KEY CHECK(telegram_id > 0),"
                    "employee_id INTEGER NOT NULL REFERENCES employees(id))"
                )
                for telegram_id, bitrix_id in EMPLOYEE_ALIASES:
                    employee = db.execute(
                        "SELECT id FROM employees WHERE bitrix_id=? AND role='manager'",
                        (bitrix_id,),
                    ).fetchone()
                    if employee is None:
                        raise RuntimeError(f"Не найден менеджер Bitrix {bitrix_id} для Telegram-псевдонима.")
                    db.execute(
                        "INSERT INTO employee_aliases(telegram_id,employee_id) VALUES(?,?)",
                        (telegram_id, employee["id"]),
                    )
                db.execute(
                    "INSERT INTO schema_migrations VALUES(?,?,?)",
                    (7, int(time.time()), encode({"employee_aliases": len(EMPLOYEE_ALIASES)})),
                )
                db.execute("PRAGMA user_version=7")
                version = 7
            if version == 7:
                employee = db.execute(
                    "SELECT id,name FROM employees WHERE bitrix_id=460 AND role='manager'"
                ).fetchone()
                renamed = False
                if employee is not None and employee["name"] == "Вячеслав":
                    db.execute(
                        "UPDATE employees SET name='Вячеслав Шандра' WHERE id=?",
                        (employee["id"],),
                    )
                    renamed = True
                db.execute(
                    "INSERT INTO schema_migrations VALUES(?,?,?)",
                    (8, int(time.time()), encode({"vyacheslav_full_name": renamed})),
                )
                db.execute("PRAGMA user_version=8")
            # Lead Flow's isolated test database may already exist from the
            # first migration stage. Add only missing, conflict-free bindings;
            # never rewrite legacy employees or identities.
            for telegram_id, bitrix_id, name, role in EMPLOYEE_BINDINGS:
                existing = db.execute(
                    "SELECT * FROM employees WHERE telegram_id=? OR (? IS NOT NULL AND bitrix_id=?)",
                    (telegram_id, bitrix_id, bitrix_id),
                ).fetchall()
                if not existing:
                    db.execute(
                        "INSERT INTO employees(telegram_id,bitrix_id,name,role) VALUES(?,?,?,?)",
                        (telegram_id, bitrix_id, name, role),
                    )
                elif len(existing) != 1 or existing[0]["telegram_id"] != telegram_id or existing[0]["bitrix_id"] != bitrix_id:
                    raise RuntimeError(f"Конфликт привязки сотрудника {name}; автоматическое изменение отменено.")

    @staticmethod
    def row(db, sql, args=()):
        result = db.execute(sql, args).fetchone()
        if result is None:
            raise DomainError("Запись не найдена.", 404)
        return dict(result)

    def actor(self, telegram_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT e.* FROM employee_aliases a JOIN employees e ON e.id=a.employee_id "
                "WHERE a.telegram_id=? AND e.active=1",
                (telegram_id,),
            ).fetchone()
            if row is None:
                row = db.execute(
                    "SELECT * FROM employees WHERE telegram_id=? AND active=1",
                    (telegram_id,),
                ).fetchone()
            if row is None:
                raise DomainError("Доступ к планировщику не настроен. Обратитесь к руководителю.", 403)
            return dict(row)

    def employee(self, actor, employee_id=None):
        selected_id = employee_id or actor["id"]
        self.owner(actor, selected_id)
        with self.connection() as db:
            return self.row(db, "SELECT * FROM employees WHERE id=? AND active=1", (selected_id,))

    def sync_external_tasks(self, plan_id, external_tasks):
        """Mirror CRM tasks locally; no Planner action writes back to Bitrix."""
        with self.connection(True) as db:
            plan = self.row(db, "SELECT * FROM plans WHERE id=?", (plan_id,))
            if plan["status"] == "closed":
                return 0
            changed = 0
            for item in external_tasks:
                existing = db.execute(
                    "SELECT t.*,p.status AS plan_status FROM tasks t "
                    "JOIN plans p ON p.id=t.plan_id WHERE t.external_id=? ORDER BY t.id LIMIT 1",
                    (item.external_id,),
                ).fetchone()
                external_status = str(item.external_status or "")
                task_status = "done" if external_status in {"5", "7"} else "todo"
                description = f"Срок в Bitrix24: {item.deadline.isoformat()}" if item.deadline else "Без срока в Bitrix24"
                outcome = item.deadline_outcome.value if item.deadline_outcome else None
                if existing is None:
                    db.execute(
                        """INSERT INTO tasks(
                            plan_id,employee_id,title,description,kind,planned_minutes,target,
                            actual_value,actual_seconds,status,result,comment,created_by,locked,
                            revision,created_at,external_id,external_url,external_status,
                            work_done_today,deadline_outcome)
                           VALUES(?,?,?,?,'bitrix',0,NULL,0,0,?,'','',?,1,1,?,?,?,?,?,?)""",
                        (
                            plan["id"], plan["employee_id"], item.title, description, task_status,
                            plan["employee_id"], int(time.time()), item.external_id, item.url,
                            external_status, int(item.work_done_today), outcome,
                        ),
                    )
                    changed += 1
                elif existing["plan_id"] == plan["id"] and existing["plan_status"] != "closed":
                    before = (
                        existing["title"], existing["description"], existing["status"],
                        existing["external_url"], existing["external_status"],
                        existing["work_done_today"], existing["deadline_outcome"],
                    )
                    after = (
                        item.title, description, task_status, item.url, external_status,
                        int(item.work_done_today), outcome,
                    )
                    if before != after:
                        db.execute(
                            """UPDATE tasks SET title=?,description=?,status=?,external_url=?,
                               external_status=?,work_done_today=?,deadline_outcome=?,revision=revision+1
                               WHERE id=?""",
                            (*after, existing["id"]),
                        )
                        changed += 1
            return changed

    def mirror_distribution_for_tester(self, telegram_id, distribution_id, total, dates):
        """Expose an isolated imported distribution to the owner's manager UI."""
        actor = self.actor(telegram_id)
        external_id = f"migration-test:{distribution_id}"
        with self.connection(True) as db:
            existing = db.execute("SELECT * FROM tasks WHERE external_id=?", (external_id,)).fetchone()
            if existing:
                return dict(existing)
            workday = today()
            plan = db.execute(
                "SELECT * FROM plans WHERE employee_id=? AND day=?", (actor["id"], workday),
            ).fetchone()
            if plan is None:
                plan_id = db.execute(
                    "INSERT INTO plans(employee_id,day,created_at) VALUES(?,?,?)",
                    (actor["id"], workday, int(time.time())),
                ).lastrowid
            elif plan["status"] != "draft":
                raise DomainError("Тестовый план на сегодня уже подтверждён или закрыт.")
            else:
                plan_id = plan["id"]
            task_id = db.execute(
                "INSERT INTO tasks(plan_id,employee_id,title,description,kind,target,created_by,locked,created_at,creation_actor,external_id) "
                "VALUES(?,?,'Тест распределения недозвонов',?,'quantitative',?,?,0,?,'system',?)",
                (plan_id, actor["id"], "Назначенные даты: " + ", ".join(sorted(dates)) + f". Лидов: {total}.",
                 total, actor["id"], int(time.time()), external_id),
            ).lastrowid
            metadata = {"distribution_id": distribution_id, "migration_test": True, "target": total}
            db.execute(
                "INSERT INTO system_audit(plan_id,entity,entity_id,action,after_json,created_at) "
                "VALUES(?,'task',?,'distribution_imported',?,?)",
                (plan_id, task_id, encode(metadata), int(time.time())),
            )
            return dict(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    @staticmethod
    def supervisor(actor):
        if actor["role"] != "supervisor":
            raise DomainError("Действие доступно руководителю.", 403)

    @staticmethod
    def owner(actor, employee_id):
        if actor["role"] != "supervisor" and actor["id"] != employee_id:
            raise DomainError("Нет доступа к этому плану.", 403)

    def plan(self, db, actor, plan_id, writable=False):
        plan = self.row(db, "SELECT * FROM plans WHERE id=?", (plan_id,))
        self.owner(actor, plan["employee_id"])
        if writable and plan["status"] == "closed":
            raise DomainError("День закрыт. Сохранённый отчёт нельзя изменять.")
        return plan

    def task(self, db, actor, task_id, writable=False):
        task = self.row(db, "SELECT * FROM tasks WHERE id=?", (task_id,))
        plan = self.plan(db, actor, task["plan_id"], writable)
        if writable and task["kind"] == "bitrix":
            raise DomainError("Задачи Bitrix24 доступны только для чтения.", 403)
        if writable and task["status"] == "moved":
            raise DomainError("Задача уже перенесена. Откройте её на новой дате.")
        return task, plan

    def audit(self, db, actor, entity, entity_id, action, before, after, plan_id=None):
        db.execute("INSERT INTO audit(plan_id,entity,entity_id,actor_id,action,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (plan_id, entity, entity_id, actor["id"], action,
                    encode(before) if before is not None else None, encode(after) if after is not None else None, int(time.time())))

    def employees(self, actor):
        self.supervisor(actor)
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM employees WHERE role='manager' ORDER BY name")]

    def save_employee(self, actor, values, employee_id=None):
        self.supervisor(actor)
        with self.connection(True) as db:
            before = None
            if employee_id is not None:
                before = self.row(db, "SELECT * FROM employees WHERE id=? AND role='manager'", (employee_id,))
                db.execute("UPDATE employees SET name=?,telegram_id=?,bitrix_id=?,active=? WHERE id=?",
                           (values["name"], values["telegram_id"], values["bitrix_id"], values["active"], employee_id))
                if not values["active"]:
                    for timer in db.execute("SELECT * FROM tasks WHERE employee_id=? AND timer_started IS NOT NULL", (employee_id,)).fetchall():
                        old = dict(timer)
                        self.stop_timer(db, old)
                        new = self.row(db, "SELECT * FROM tasks WHERE id=?", (old["id"],))
                        self.audit(db, actor, "task", old["id"], "timer_stop", old, new, old["plan_id"])
            else:
                employee_id = db.execute("INSERT INTO employees(name,telegram_id,bitrix_id,role) VALUES(?,?,?,'manager')",
                                         (values["name"], values["telegram_id"], values["bitrix_id"])).lastrowid
            after = self.row(db, "SELECT * FROM employees WHERE id=?", (employee_id,))
            self.audit(db, actor, "employee", employee_id, "employee_saved", before, after)
            return after

    def new_plan(self, db, actor, employee_id, day):
        employee = self.row(db, "SELECT * FROM employees WHERE id=? AND role='manager' AND active=1", (employee_id,))
        self.owner(actor, employee["id"])
        plan_id = db.execute("INSERT INTO plans(employee_id,day,created_at) VALUES(?,?,?)", (employee_id, day, int(time.time()))).lastrowid
        plan = self.plan(db, actor, plan_id)
        self.audit(db, actor, "plan", plan_id, "plan_created", None, plan, plan_id)
        return plan

    def create_plan(self, actor, day, employee_id=None, from_previous=False):
        employee_id = employee_id or actor["id"]
        self.owner(actor, employee_id)
        with self.connection(True) as db:
            if db.execute("SELECT 1 FROM plans WHERE employee_id=? AND day=?", (employee_id, day)).fetchone():
                raise DomainError("План на эту дату уже существует.")
            plan = self.new_plan(db, actor, employee_id, day)
            if from_previous:
                previous = db.execute("SELECT id FROM plans WHERE employee_id=? AND day<? AND strftime('%w',day) NOT IN ('0','6') ORDER BY day DESC LIMIT 1", (employee_id, day)).fetchone()
                if previous is None:
                    raise DomainError("Нет плана предыдущего рабочего дня для копирования.")
                for row in db.execute("SELECT * FROM tasks WHERE plan_id=? AND kind!='bitrix' AND status!='moved' ORDER BY id", (previous[0],)).fetchall():
                    old = dict(row)
                    if self.imported_task(db, old["id"]):
                        continue
                    self.insert_task(db, actor, plan, {k: old[k] for k in ("title", "description", "kind", "planned_minutes", "target")},
                                     locked=old["locked"], source=old["id"])
            return self.detail(db, actor, plan["id"])

    def list_plans(self, actor, employee_id=None, day=None):
        if actor["role"] == "manager":
            self.owner(actor, employee_id or actor["id"])
            employee_id = actor["id"]
        clauses, args = [], []
        if employee_id is not None:
            clauses.append("p.employee_id=?")
            args.append(employee_id)
        if day:
            clauses.append("p.day=?")
            args.append(day)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as db:
            plans = [dict(r) for r in db.execute("SELECT p.*,e.name AS employee_name FROM plans p JOIN employees e ON e.id=p.employee_id" + where + " ORDER BY p.day DESC,p.id DESC LIMIT 366", args)]
            for plan in plans:
                tasks = [dict(t) for t in db.execute("SELECT * FROM tasks WHERE plan_id=?", (plan["id"],))]
                plan["summary"] = self.summary(tasks)
                plan.pop("initial_snapshot", None)
            return plans

    @staticmethod
    def effective_seconds(task):
        return task["actual_seconds"] + (max(0, int(time.time()) - task["timer_started"]) if task["timer_started"] is not None else 0)

    def summary(self, tasks):
        return {"total": len(tasks), "done": sum(t["status"] == "done" for t in tasks),
                "moved": sum(t["status"] == "moved" for t in tasks),
                "planned_minutes": sum(t["planned_minutes"] for t in tasks),
                "actual_seconds": sum(self.effective_seconds(t) for t in tasks)}

    def detail(self, db, actor, plan_id):
        plan = self.plan(db, actor, plan_id)
        plan["employee_name"] = self.row(db, "SELECT name FROM employees WHERE id=?", (plan["employee_id"],))["name"]
        plan["tasks"] = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE plan_id=? ORDER BY id", (plan_id,))]
        for task in plan["tasks"]:
            task["distribution_target_locked"] = self.imported_task(db, task["id"])
            if task["distribution_target_locked"]:
                link = db.execute("SELECT d.payload FROM distribution_tasks x JOIN distributions d ON d.id=x.distribution_id WHERE x.task_id=?", (task["id"],)).fetchone()
                employee = db.execute("SELECT telegram_id,bitrix_id FROM employees WHERE id=?", (task["employee_id"],)).fetchone()
                allocation = next((a for a in json.loads(link[0])["allocations"] if a["telegram_id"] == employee[0] and a["bitrix_id"] == employee[1]), None)
                task["distribution_source"] = {"name": "Утреннее распределение", "dates": allocation["dates"] if allocation else [], "count": task["target"]}
            task["elapsed_seconds"] = self.effective_seconds(task)
        plan["summary"] = self.summary(plan["tasks"])
        plan["server_time"] = int(time.time())
        plan["initial_snapshot"] = json.loads(plan["initial_snapshot"]) if plan["initial_snapshot"] else None
        return plan

    def get_plan(self, actor, plan_id):
        with self.connection() as db:
            return self.detail(db, actor, plan_id)

    def insert_task(self, db, actor, plan, values, locked=None, source=None):
        count = db.execute("SELECT count(*) FROM tasks WHERE plan_id=?", (plan["id"],)).fetchone()[0]
        if count >= 100:
            raise DomainError("В плане может быть не более 100 задач.")
        task_id = db.execute("""INSERT INTO tasks(plan_id,employee_id,title,description,kind,planned_minutes,target,created_by,locked,created_at,source_task_id)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (plan["id"], plan["employee_id"], values["title"], values["description"], values["kind"],
                             values["planned_minutes"], values["target"], actor["id"],
                             int(actor["role"] == "supervisor") if locked is None else locked, int(time.time()), source)).lastrowid
        task = self.row(db, "SELECT * FROM tasks WHERE id=?", (task_id,))
        self.audit(db, actor, "task", task_id, "task_created", None, task, plan["id"])
        return task

    def add_task(self, actor, plan_id, values):
        with self.connection(True) as db:
            plan = self.plan(db, actor, plan_id, True)
            return self.insert_task(db, actor, plan, values)

    @staticmethod
    def stop_timer(db, task):
        if task["timer_started"] is not None:
            seconds = task["actual_seconds"] + max(0, int(time.time()) - task["timer_started"])
            db.execute("UPDATE tasks SET actual_seconds=?,timer_started=NULL,revision=revision+1 WHERE id=?", (seconds, task["id"]))

    def patch_task(self, actor, task_id, values):
        with self.connection(True) as db:
            task, plan = self.task(db, actor, task_id, True)
            if "target" in values and self.imported_task(db, task_id):
                raise DomainError("Количество недозвонов задано подтверждённым распределением и не изменяется.", 403)
            if values.pop("revision") != task["revision"]:
                raise DomainError("Задача уже изменена. Обновите план и повторите действие.")
            planning = {"title", "description", "planned_minutes", "target"} & values.keys()
            if actor["role"] != "supervisor":
                if planning and task["locked"]:
                    raise DomainError("Плановые условия задал руководитель. Отправьте запрос на изменение.", 403)
                if {"target", "planned_minutes"} & planning and plan["status"] == "approved":
                    raise DomainError("План утверждён. Отправьте запрос на изменение цели или времени.", 403)
            if task["kind"] != "quantitative" and {"target", "actual_value"} & values.keys():
                raise DomainError("Количественные показатели доступны только для количественной задачи.", 422)
            if "actual_minutes" in values:
                if task["timer_started"] is not None:
                    raise DomainError("Сначала остановите таймер, затем укажите время вручную.")
                values["actual_seconds"] = values.pop("actual_minutes") * 60
            if values.get("status") == "done":
                self.stop_timer(db, task)
            if actor["role"] == "supervisor" and planning:
                values["locked"] = 1
            if values:
                # Keys come only from validated Pydantic fields, never from raw input.
                db.execute("UPDATE tasks SET " + ",".join(f"{k}=?" for k in values) + ",revision=revision+1 WHERE id=?", (*values.values(), task_id))
            after = self.row(db, "SELECT * FROM tasks WHERE id=?", (task_id,))
            self.audit(db, actor, "task", task_id, "task_updated", task, after, plan["id"])
            return after

    def timer(self, actor, task_id, action):
        with self.connection(True) as db:
            task, plan = self.task(db, actor, task_id, True)
            if task["employee_id"] != actor["id"]:
                raise DomainError("Таймер запускает сам исполнитель.", 403)
            if action == "start":
                if task["status"] == "done" or plan["day"] != today():
                    raise DomainError("Таймер доступен для незавершённых задач сегодняшнего дня.")
                running = db.execute("SELECT id FROM tasks WHERE employee_id=? AND timer_started IS NOT NULL", (actor["id"],)).fetchone()
                if running and running[0] != task_id:
                    raise DomainError("Уже работает таймер другой задачи. Сначала остановите его.")
                if not running:
                    db.execute("UPDATE tasks SET timer_started=?,status='in_progress',revision=revision+1 WHERE id=?", (int(time.time()), task_id))
            else:
                self.stop_timer(db, task)
            after = self.row(db, "SELECT * FROM tasks WHERE id=?", (task_id,))
            self.audit(db, actor, "task", task_id, "timer_" + action, task, after, plan["id"])
            return after

    def move(self, actor, task_id, day, reason, revision):
        with self.connection(True) as db:
            task, plan = self.task(db, actor, task_id, True)
            if self.imported_task(db, task_id):
                raise DomainError("Задачу подтверждённого распределения нельзя переносить.", 403)
            if revision != task["revision"]:
                raise DomainError("Задача уже изменена. Обновите план.")
            if task["status"] == "done" or day <= plan["day"] or day < today():
                raise DomainError("Переносите незавершённую задачу на более позднюю дату, не раньше сегодня.")
            target = db.execute("SELECT * FROM plans WHERE employee_id=? AND day=?", (plan["employee_id"], day)).fetchone()
            target = self.plan(db, actor, target["id"], True) if target else self.new_plan(db, actor, plan["employee_id"], day)
            new = self.insert_task(db, actor, target, {k: task[k] for k in ("title", "description", "kind", "planned_minutes", "target")},
                                   locked=task["locked"] or int(plan["status"] == "approved"), source=task_id)
            self.stop_timer(db, task)
            db.execute("UPDATE tasks SET status='moved',moved_to=?,revision=revision+1 WHERE id=?", (new["id"], task_id))
            after = self.row(db, "SELECT * FROM tasks WHERE id=?", (task_id,))
            self.cancel_requests(db, actor, task_id, "Задача перенесена; создайте запрос на новой дате.")
            self.audit(db, actor, "task", task_id, "task_moved", task, {**after, "move_reason": reason, "destination_day": day}, plan["id"])
            return new

    @staticmethod
    def imported_task(db, task_id):
        if db.execute("PRAGMA user_version").fetchone()[0] < 3:
            return False
        return db.execute("SELECT 1 FROM distribution_tasks WHERE task_id=?", (task_id,)).fetchone() is not None

    def confirm_plan(self, actor, plan_id):
        with self.connection(True) as db:
            current = self.row(db, "SELECT * FROM employees WHERE id=? AND active=1", (actor["id"],))
            if current["role"] != "manager":
                raise DomainError("Свой план подтверждает менеджер.", 403)
            before = self.plan(db, current, plan_id, True)
            if before["status"] == "approved":
                return self.detail(db, current, plan_id)
            snapshot = self.detail(db, current, plan_id)
            stamp = int(time.time())
            db.execute("UPDATE plans SET status='approved',initial_snapshot=?,approved_at=?,approved_by=? WHERE id=?",
                       (encode(snapshot), stamp, current["id"], plan_id))
            after = {**self.plan(db, current, plan_id), "confirmed_by_telegram_id": current["telegram_id"]}
            self.audit(db, current, "plan", plan_id, "plan_confirmed", before, after, plan_id)
            return self.detail(db, current, plan_id)

    def approve_plan(self, actor, plan_id):
        self.supervisor(actor)
        with self.connection(True) as db:
            before = self.plan(db, actor, plan_id, True)
            if before["status"] != "draft":
                raise DomainError("План уже утверждён.")
            snapshot = self.detail(db, actor, plan_id)
            db.execute("UPDATE plans SET status='approved',initial_snapshot=?,approved_at=?,approved_by=? WHERE id=?",
                       (encode(snapshot), int(time.time()), actor["id"], plan_id))
            after = self.plan(db, actor, plan_id)
            self.audit(db, actor, "plan", plan_id, "plan_approved", before, after, plan_id)
            return self.detail(db, actor, plan_id)

    def request_goal(self, actor, task_id, values):
        if actor["role"] != "manager":
            raise DomainError("Запрос создаёт менеджер.", 403)
        with self.connection(True) as db:
            task, plan = self.task(db, actor, task_id, True)
            field = values["field"]
            if field == "target" and self.imported_task(db, task_id):
                raise DomainError("Количество недозвонов нельзя менять через согласование цели.", 403)
            if field == "target" and task["kind"] != "quantitative":
                raise DomainError("У этой задачи нет количественной цели.", 422)
            if task[field] == values["proposed"]:
                raise DomainError("Предложенное значение совпадает с действующим.", 422)
            request_id = db.execute("INSERT INTO goal_requests(task_id,employee_id,field,old_value,proposed,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                                    (task_id, actor["id"], field, task[field], values["proposed"], values["reason"], int(time.time()))).lastrowid
            after = self.row(db, "SELECT * FROM goal_requests WHERE id=?", (request_id,))
            self.audit(db, actor, "request", request_id, "goal_requested", None, after, plan["id"])
            return after

    def requests(self, actor):
        with self.connection() as db:
            sql = "SELECT r.*,t.title,p.day,e.name AS employee_name FROM goal_requests r JOIN tasks t ON t.id=r.task_id JOIN plans p ON p.id=t.plan_id JOIN employees e ON e.id=r.employee_id"
            args = ()
            if actor["role"] != "supervisor":
                sql += " WHERE r.employee_id=?"
                args = (actor["id"],)
            return [dict(r) for r in db.execute(sql + " ORDER BY (r.status='pending') DESC,r.id DESC LIMIT 500", args)]

    def decide(self, actor, request_id, decision, comment):
        self.supervisor(actor)
        with self.connection(True) as db:
            request = self.row(db, "SELECT * FROM goal_requests WHERE id=?", (request_id,))
            task, plan = self.task(db, actor, request["task_id"], True)
            if request["status"] != "pending":
                raise DomainError("Запрос уже рассмотрен.")
            if decision == "approved":
                field = request["field"]
                if field == "target" and self.imported_task(db, task["id"]):
                    raise DomainError("Количество недозвонов нельзя менять через согласование цели.", 403)
                if task[field] != request["old_value"]:
                    raise DomainError("Условие задачи изменилось после запроса. Отклоните его и попросите новый.")
                db.execute(f"UPDATE tasks SET {field}=?,locked=1,revision=revision+1 WHERE id=?", (request["proposed"], task["id"]))
                after = self.row(db, "SELECT * FROM tasks WHERE id=?", (task["id"],))
                self.audit(db, actor, "task", task["id"], "goal_changed", task, after, plan["id"])
            db.execute("UPDATE goal_requests SET status=?,decided_at=?,decided_by=?,decision_comment=? WHERE id=?",
                       (decision, int(time.time()), actor["id"], comment, request_id))
            after = self.row(db, "SELECT * FROM goal_requests WHERE id=?", (request_id,))
            self.audit(db, actor, "request", request_id, "goal_" + decision, request, after, plan["id"])
            return after

    def cancel_requests(self, db, actor, task_id, reason):
        for row in db.execute("SELECT * FROM goal_requests WHERE task_id=? AND status='pending'", (task_id,)).fetchall():
            before = dict(row)
            db.execute("UPDATE goal_requests SET status='cancelled',decided_at=?,decided_by=?,decision_comment=? WHERE id=?",
                       (int(time.time()), actor["id"], reason, before["id"]))
            after = self.row(db, "SELECT * FROM goal_requests WHERE id=?", (before["id"],))
            plan_id = self.row(db, "SELECT plan_id FROM tasks WHERE id=?", (task_id,))["plan_id"]
            self.audit(db, actor, "request", before["id"], "goal_cancelled", before, after, plan_id)

    def close_plan(self, actor, plan_id):
        with self.connection(True) as db:
            plan = self.plan(db, actor, plan_id)
            if plan["status"] == "closed":
                return json.loads(self.row(db, "SELECT snapshot FROM reports WHERE plan_id=?", (plan_id,))["snapshot"])
            if plan["day"] > today():
                raise DomainError("Нельзя завершить будущий рабочий день.")
            for row in db.execute("SELECT * FROM tasks WHERE plan_id=?", (plan_id,)).fetchall():
                task = dict(row)
                if task["timer_started"] is not None:
                    self.stop_timer(db, task)
                    after = self.row(db, "SELECT * FROM tasks WHERE id=?", (task["id"],))
                    self.audit(db, actor, "task", task["id"], "timer_stop", task, after, plan_id)
                self.cancel_requests(db, actor, task["id"], "День закрыт.")
            db.execute("UPDATE plans SET status='closed',closed_at=? WHERE id=?", (int(time.time()), plan_id))
            snapshot = self.detail(db, actor, plan_id)
            snapshot["report_version"] = 1
            db.execute("INSERT INTO reports(plan_id,snapshot,closed_by,created_at) VALUES(?,?,?,?)",
                       (plan_id, encode(snapshot), actor["id"], int(time.time())))
            self.audit(db, actor, "plan", plan_id, "day_closed", plan, self.plan(db, actor, plan_id), plan_id)
            return snapshot

    def report(self, actor, plan_id):
        with self.connection() as db:
            self.plan(db, actor, plan_id)
            return json.loads(self.row(db, "SELECT snapshot FROM reports WHERE plan_id=?", (plan_id,))["snapshot"])

    def history(self, actor, plan_id):
        with self.connection() as db:
            self.plan(db, actor, plan_id)
            rows = [dict(r) for r in db.execute("SELECT a.*,e.name AS actor_name FROM audit a JOIN employees e ON e.id=a.actor_id WHERE a.plan_id=? ORDER BY a.id", (plan_id,))]
            for row in db.execute("SELECT * FROM system_audit WHERE plan_id=?", (plan_id,)):
                rows.append({**dict(row), "id": -row["id"], "actor_id": None, "actor_name": "Система", "before_json": None})
            rows.sort(key=lambda row: (row["created_at"], abs(row["id"])))
            for row in rows:
                for key in ("before_json", "after_json"):
                    row[key] = json.loads(row[key]) if row[key] else None
            return rows
