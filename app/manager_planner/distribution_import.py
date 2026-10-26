"""Idempotent transactional inbox. Only an explicit local call runs imports."""
import json
import time

from .distribution_queue import canonical, validate

MIGRATION = (
    "ALTER TABLE tasks ADD COLUMN creation_actor TEXT NOT NULL DEFAULT 'employee' CHECK(creation_actor IN ('employee','system'))",
    "CREATE TABLE distributions(id TEXT PRIMARY KEY,workday TEXT NOT NULL UNIQUE,sha256 TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('imported','review')),reason TEXT NOT NULL,imported_at INTEGER NOT NULL)",
    "CREATE TABLE distribution_tasks(distribution_id TEXT NOT NULL REFERENCES distributions(id),employee_id INTEGER NOT NULL REFERENCES employees(id),task_id INTEGER UNIQUE REFERENCES tasks(id),status TEXT NOT NULL CHECK(status IN ('imported','review')),PRIMARY KEY(distribution_id,employee_id))",
    "CREATE TABLE distribution_leads(distribution_id TEXT NOT NULL REFERENCES distributions(id),lead_id INTEGER NOT NULL,employee_id INTEGER NOT NULL REFERENCES employees(id),created_date TEXT NOT NULL,initial_stage TEXT NOT NULL CHECK(initial_stage='IN_PROCESS'),PRIMARY KEY(distribution_id,lead_id))",
    "CREATE TABLE system_audit(id INTEGER PRIMARY KEY,plan_id INTEGER REFERENCES plans(id),entity TEXT NOT NULL,entity_id INTEGER NOT NULL,action TEXT NOT NULL,after_json TEXT NOT NULL,created_at INTEGER NOT NULL)",
    "CREATE TRIGGER immutable_system_update BEFORE UPDATE ON system_audit BEGIN SELECT RAISE(ABORT,'Immutable system audit'); END",
    "CREATE TRIGGER immutable_system_delete BEFORE DELETE ON system_audit BEGIN SELECT RAISE(ABORT,'Immutable system audit'); END",
)


def import_snapshot(store, snapshot, authorized_ids):
    digest = validate(snapshot)
    if snapshot["confirmed_by"] not in authorized_ids:
        raise PermissionError("Инициатор не уполномочен подтверждать распределение.")
    with store.connection(True) as db:
        old = db.execute("SELECT * FROM distributions WHERE id=? OR workday=?", (snapshot["distribution_id"], snapshot["workday"])).fetchone()
        if old:
            if old["id"] != snapshot["distribution_id"] or old["sha256"] != digest:
                raise ValueError("Конфликт подтверждений на один день.")
            return dict(old)
        employees = []
        reasons = []
        initiator = db.execute(
            "SELECT e.id FROM employee_aliases a JOIN employees e ON e.id=a.employee_id "
            "WHERE a.telegram_id=? UNION ALL "
            "SELECT id FROM employees WHERE telegram_id=? LIMIT 1",
            (snapshot["confirmed_by"], snapshot["confirmed_by"]),
        ).fetchone()
        if initiator is None:
            reasons.append("initiator_not_bound")
        for item in snapshot["allocations"]:
            employee = db.execute("SELECT * FROM employees WHERE telegram_id=? AND bitrix_id=? AND role='manager'", (item["telegram_id"], item["bitrix_id"])).fetchone()
            if employee is None or not employee["active"]:
                reasons.append("inactive_or_missing_manager")
            employees.append(employee)
        status = "review" if reasons else "imported"
        # An inactive manager blocks the whole snapshot, without creating any tasks.
        db.execute("INSERT INTO distributions VALUES(?,?,?,?,?,?,?)", (snapshot["distribution_id"], snapshot["workday"], digest, canonical(snapshot), status, ",".join(reasons), int(time.time())))
        for item, employee in zip(snapshot["allocations"], employees, strict=True):
            if employee is None:
                continue
            plan = db.execute("SELECT * FROM plans WHERE employee_id=? AND day=?", (employee["id"], snapshot["workday"])).fetchone()
            task_id = None
            outcome = "review"
            if not reasons and (plan is None or plan["status"] == "draft"):
                if plan is None:
                    plan_id = db.execute("INSERT INTO plans(employee_id,day,created_at) VALUES(?,?,?)", (employee["id"], snapshot["workday"], int(time.time()))).lastrowid
                else:
                    plan_id = plan["id"]
                if db.execute("SELECT count(*) FROM tasks WHERE plan_id=?", (plan_id,)).fetchone()[0] >= 100:
                    raise ValueError("В черновике нет места для импортированной задачи.")
                description = "Назначенные даты: " + ", ".join(item["dates"]) + f". Лидов: {item['count']}."
                # created_by retains confirmation provenance; system_audit explicitly
                # records creation as automatic, never as an employee action.
                task_id = db.execute("INSERT INTO tasks(plan_id,employee_id,title,description,kind,target,created_by,locked,created_at,creation_actor) VALUES(?,?,'Обработка недозвонов',?,'quantitative',?,?,1,?,'system')", (plan_id, employee["id"], description, item["count"], initiator["id"], int(time.time()))).lastrowid
                metadata = {"distribution_id": snapshot["distribution_id"], "confirmed_by_telegram_id": snapshot["confirmed_by"], "confirmed_at": snapshot["confirmed_at"], "creation_actor": "system", "target": item["count"]}
                db.execute("INSERT INTO system_audit(plan_id,entity,entity_id,action,after_json,created_at) VALUES(?,'task',?,'distribution_imported',?,?)", (plan_id, task_id, canonical(metadata), int(time.time())))
                outcome = "imported"
            db.execute("INSERT INTO distribution_tasks VALUES(?,?,?,?)", (snapshot["distribution_id"], employee["id"], task_id, outcome))
            for lead in item["leads"]:
                db.execute("INSERT INTO distribution_leads VALUES(?,?,?,?,?)", (snapshot["distribution_id"], lead["id"], employee["id"], lead["created_date"], lead["stage_id"]))
            if outcome == "review":
                status = "review"
        db.execute("UPDATE distributions SET status=? WHERE id=?", (status, snapshot["distribution_id"]))
        return dict(db.execute("SELECT * FROM distributions WHERE id=?", (snapshot["distribution_id"],)).fetchone())


def import_once(store, queue, authorized_ids):
    results = []
    # Read and close the queue before writing planner.db: no cross-DB locks.
    for snapshot in list(queue.read()):
        try:
            result = import_snapshot(store, snapshot, authorized_ids)
            results.append({"id": result["id"], "status": result["status"]})
        except (ValueError, PermissionError):
            results.append({"id": snapshot["distribution_id"], "status": "error"})
    return results
