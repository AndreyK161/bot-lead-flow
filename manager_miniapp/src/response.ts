// Runtime checks at the network boundary. Types alone do not validate JSON.
type ObjectValue = Record<string, unknown>;
const object = (v: unknown): v is ObjectValue => !!v && typeof v === 'object' && !Array.isArray(v);
const number = (v: unknown) => typeof v === 'number' && Number.isFinite(v);
const text = (v: unknown) => typeof v === 'string';
const day = (v: unknown) => text(v) && /^\d{4}-\d{2}-\d{2}$/.test(v as string);
const choice = (v: unknown, values: string[]) => text(v) && values.includes(v as string);
const list = (v: unknown, check: (item: unknown) => boolean) => Array.isArray(v) && v.every(check);
const optional = (v: unknown, check: (item: unknown) => boolean) => v === undefined || check(v);
const nullable = (v: unknown, check: (item: unknown) => boolean) => v === null || check(v);
const flag = (v: unknown) => v === 0 || v === 1 || typeof v === 'boolean';
const url = (v: unknown) => {
  if (!text(v)) return false;
  try { const parsed = new URL(v as string); return ['https:', 'http:'].includes(parsed.protocol) && !!parsed.hostname && !parsed.username && !parsed.password; }
  catch { return false; }
};
const employee = (v: unknown) => object(v) && number(v.id) && text(v.name) && v.name.length > 0 && choice(v.role, ['manager', 'supervisor']) && flag(v.active) && ['telegram_id','bitrix_id'].every(k => optional(v[k], x => nullable(x, number)));
const summary = (v: unknown) => object(v) && ['total', 'done', 'moved', 'planned_minutes', 'actual_seconds'].every(k => number(v[k]));
const source = (v: unknown) => object(v) && text(v.name) && number(v.count) && list(v.dates, day);
const task = (v: unknown) => object(v) && ['id','plan_id','employee_id','planned_minutes','actual_value','actual_seconds','revision','locked'].every(k => number(v[k])) && ['title','description','comment','result'].every(k => text(v[k])) && choice(v.kind,['quantitative','general','client','bitrix']) && choice(v.status,['todo','in_progress','done','moved']) && nullable(v.target, number) && nullable(v.timer_started, number) && optional(v.external_url, x => nullable(x, url)) && optional(v.moved_to, x => nullable(x, number)) && optional(v.elapsed_seconds, number) && optional(v.distribution_target_locked, flag) && optional(v.distribution_source, x => nullable(x, source));
const plan = (v: unknown) => object(v) && number(v.id) && number(v.employee_id) && text(v.employee_name) && day(v.day) && choice(v.status,['draft','approved','closed']) && summary(v.summary);
// Historical snapshots need only the fields actually rendered, not new task fields.
const snapshotTask = (v: unknown) => object(v) && number(v.id) && text(v.title) && number(v.planned_minutes) && optional(v.target, x => nullable(x, number));
const detail = (v: unknown) => plan(v) && object(v) && list(v.tasks,task) && number(v.server_time) && optional(v.initial_snapshot, x => nullable(x, s => object(s) && list(s.tasks,snapshotTask)));
const request = (v: unknown) => object(v) && number(v.id) && number(v.task_id) && ['employee_name','title','reason','decision_comment'].every(k => text(v[k])) && day(v.day) && choice(v.field,['target','planned_minutes']) && choice(v.status,['pending','approved','rejected','cancelled']) && number(v.proposed) && nullable(v.old_value, number) && optional(v.created_at, number) && optional(v.decided_at, x => nullable(x, number));
const audit = (v: unknown) => object(v) && number(v.id) && number(v.created_at) && text(v.actor_name) && text(v.action) && text(v.entity) && number(v.entity_id) && (v.before_json === null || object(v.before_json)) && (v.after_json === null || object(v.after_json));
const notice = (v: unknown) => object(v) && text(v.id) && text(v.employee_name) && text(v.reason) && choice(v.status,['imported','review','error']) && nullable(v.count, number) && list(v.dates,day) && optional(v.day, day) && optional(v.employee_id, x => nullable(x, number));
const time = (v: unknown) => text(v) && /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(v as string);
const workday = (v: unknown) => object(v) && time(v.start) && time(v.end) && text(v.timezone) && typeof v.reminders_enabled === 'boolean';
const calls = (v: unknown) => object(v) && typeof v.configured === 'boolean' && ['total','outbound','answered','long_calls','duration_seconds'].every(k => number(v[k]));
const leads = (v: unknown) => object(v) && number(v.processed) && object(v.stages) && Object.values(v.stages).every(number);
const pipeline = (v: unknown) => object(v) && ['category_id','total','amount'].every(k => number(v[k])) && object(v.stages) && Object.values(v.stages).every(number);
const crmSummary = (v: unknown) => object(v) && typeof v.available === 'boolean' && day(v.day) && optional(v.message,text) && optional(v.calls,calls) && optional(v.leads,leads) && optional(v.pipelines,x => list(x,pipeline));
const crmPerformance = (v: unknown) => object(v) && typeof v.available === 'boolean' && object(v.period) && day(v.period.start) && day(v.period.end) && optional(v.message,text) && optional(v.calls,calls) && optional(v.leads,leads) && optional(v.employee, x => object(x) && number(x.id) && text(x.name));
const dialerStage = (v: unknown) => object(v) && text(v.id) && text(v.name);
const dialerFilters = (v: unknown) => object(v) && list(v.lead,dialerStage) && object(v.deal) && list(v.deal['0'],dialerStage) && list(v.deal['2'],dialerStage);
const dialerItem = (v: unknown) => object(v) && ['id','position','attempts_count'].every(k => number(v[k])) && ['session_id','entity_type','entity_id','client_name','phone','bitrix_url','status'].every(k => text(v[k])) && optional(v.stage_id,x => nullable(x,text)) && optional(v.stage_name,x => nullable(x,text)) && optional(v.source_name,x => nullable(x,text)) && optional(v.last_result,x => nullable(x,text)) && optional(v.last_call_id,x => nullable(x,text));
const dialerSession = (v: unknown) => object(v) && ['id','bitrix_user_id','manager_name','entity_type','date_from','date_to','status'].every(k => text(v[k])) && number(v.telegram_id) && flag(v.auto_advance);
const dialerSummary = (v: unknown) => object(v) && ['total','completed','answered','failed','remaining'].every(k => number(v[k]));
const dialerPayload = (v: unknown) => object(v) && dialerSession(v.session) && dialerSummary(v.summary) && nullable(v.current,dialerItem) && list(v.items,dialerItem);

export function validateResponse(path: string, method: string, value: unknown): void {
  const route = path.split('?')[0];
  let valid = true;
  if (method === 'GET') {
    if (route === '/me') valid = object(value) && employee(value.user) && day(value.today) && text(value.timezone) && optional(value.workday, x => nullable(x, workday)) && object(value.crm) && text(value.crm.message) && optional(value.crm.connected, x => typeof x === 'boolean');
    else if (route === '/employees') valid = list(value,employee);
    else if (route === '/plans') valid = list(value,plan);
    else if (route === '/requests') valid = list(value,request);
    else if (route === '/distributions') valid = list(value,notice);
    else if (route === '/crm/summary') valid = crmSummary(value);
    else if (route === '/crm/performance') valid = crmPerformance(value);
    else if (route === '/dialer/filters') valid = dialerFilters(value);
    else if (route === '/dialer/session') valid = value === null || dialerPayload(value);
    else if (/^\/plans\/\d+\/history$/.test(route)) valid = list(value,audit);
    else if (/^\/plans\/\d+(\/report)?$/.test(route)) valid = detail(value);
  } else if (/^\/plans(\/\d+\/(approve|confirm|close))?$/.test(route)) valid = detail(value);
  else if (/^\/dialer\/sessions(\/[0-9a-f-]+\/(start|result|cancel))?$/.test(route)) valid = dialerPayload(value);
  else if (/^\/employees(\/\d+)?$/.test(route)) valid = employee(value);
  else if (/^\/tasks\/\d+\/requests$/.test(route) || /^\/requests\/\d+\/decision$/.test(route)) {
    valid = object(value) && number(value.id) && choice(value.status,['pending','approved','rejected','cancelled']);
  } else if (/^\/tasks\/\d+(\/(timer|move))?$/.test(route) || /^\/plans\/\d+\/tasks$/.test(route)) valid = task(value);
  if (!valid) throw new Error('Сервер вернул данные в неожиданном формате. Повторите загрузку.');
}
