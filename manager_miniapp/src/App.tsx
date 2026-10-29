import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { api, isTelegramSession, webLogout } from './api';
import type { Audit, Client, CrmPerformance, CrmSummary, DialerFilters, DialerPayload, Employee, GoalRequest, Me, Plan, Task } from './types';

const kindName = { quantitative: 'План / факт', general: 'Задача', client: 'По клиенту', bitrix: 'Bitrix24' };
const statusName = { todo: 'К выполнению', in_progress: 'В работе', done: 'Выполнена', moved: 'Перенесена', draft: 'Черновик', approved: 'Подтверждён', closed: 'День закрыт', pending: 'Ожидает решения', rejected: 'Отклонён', cancelled: 'Отменён' };
const fieldName: Record<string, string> = { title: 'Название', description: 'Описание', planned_minutes: 'План, мин', target: 'Цель', actual_value: 'Факт', actual_seconds: 'Время, сек', result: 'Результат', comment: 'Комментарий', status: 'Статус', timer_started: 'Начало таймера', locked: 'Условия руководителя', destination_day: 'Новая дата', move_reason: 'Причина переноса', proposed: 'Предложено', reason: 'Причина', decision_comment: 'Решение', day: 'Дата', moved_to: 'Новая задача' };
const actionName: Record<string, string> = { plan_created: 'Создан план', task_created: 'Добавлена задача', task_updated: 'Изменена задача', timer_start: 'Запущен таймер', timer_pause: 'Пауза таймера', timer_stop: 'Остановлен таймер', task_moved: 'Задача перенесена', plan_approved: 'План утверждён', goal_requested: 'Запрошено изменение', goal_changed: 'Условие изменено', goal_approved: 'Запрос утверждён', goal_rejected: 'Запрос отклонён', goal_cancelled: 'Запрос отменён', day_closed: 'День закрыт' };
const dateLabel = (day: string) => new Date(day + 'T12:00:00+03:00').toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'Europe/Moscow' });
const minutes = (seconds: number) => `${Math.floor(seconds / 3600)} ч ${Math.floor(seconds % 3600 / 60)} мин`;
const clock = (seconds: number) => `${Math.floor(seconds / 3600).toString().padStart(2, '0')}:${Math.floor(seconds % 3600 / 60).toString().padStart(2, '0')}:${Math.floor(seconds % 60).toString().padStart(2, '0')}`;

function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    home: <><path d="M3 11 12 3l9 8"/><path d="M5 10v10h14V10M9 20v-6h6v6"/></>,
    plan: <><rect x="4" y="5" width="16" height="16" rx="4"/><path d="M8 3v4m8-4v4M4 11h16m-12 5h3"/></>,
    tasks: <><path d="m4 6 2 2 4-4m-6 9 2 2 4-4m4-5h6m-6 7h6M4 20h16"/></>,
    results: <><path d="M5 20v-6m7 6V9m7 11V4"/></>,
    history: <><path d="M3 10a9 9 0 1 1 1 8M3 4v6h6m3-4v6l4 2"/></>,
    people: <><circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3m1-17a3 3 0 0 1 0 6m3 11v-3a6 6 0 0 0-2-4"/></>,
    requests: <><path d="M20 11a8 8 0 0 1-8 8H4l-2 3V11a9 9 0 0 1 18 0Z"/><path d="M8 11h8m-8 4h5"/></>,
    calls: <><path d="M7 3h3l2 5-2 2a16 16 0 0 0 4 4l2-2 5 2v3a4 4 0 0 1-4 4C9 20 4 15 3 7a4 4 0 0 1 4-4Z"/><path d="M15 5h4m-2-2v4"/></>,
    plus: <path d="M12 5v14M5 12h14"/>,
    play: <path d="m8 5 11 7-11 7Z"/>, pause: <path d="M8 5v14m8-14v14"/>,
    check: <path d="m5 12 4 4L19 6"/>, close: <path d="m6 6 12 12M6 18 18 6"/>,
    arrow: <path d="M4 12h16m-6-6 6 6-6 6"/>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.plan}</svg>;
}

actionName.plan_confirmed = 'План подтверждён менеджером';
actionName.distribution_imported = 'Недозвоны автоматически добавлены системой';
type Modal = { type: 'task' | 'goal' | 'move'; task?: Task } | { type: 'employee'; employee?: Employee } | { type: 'close' };

export default function App({ client = api, demo = false }: { client?: Client; demo?: boolean }) {
  const [me, setMe] = useState<Me>();
  const [section, setSection] = useState('home');
  const [day, setDay] = useState('');
  const [employeeId, setEmployeeId] = useState<number>();
  const [employees, setEmployees] = useState<Employee[]>([]);
  const [employeesLoaded, setEmployeesLoaded] = useState(false);
  const [loadErrors, setLoadErrors] = useState<Record<string, string>>({});
  const resourceVersions = useRef<Record<string, number>>({});
  const selectedPlanId = useRef<number | undefined>(undefined);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [loadedPlan, setPlan] = useState<Plan>();
  // Do not render the previous plan even for the frame before effect cleanup.
  const plan = loadedPlan?.day === day && loadedPlan?.employee_id === employeeId ? loadedPlan : undefined;
  const [requests, setRequests] = useState<GoalRequest[]>([]);
  const [distributions, setDistributions] = useState<Array<{ id: string; day?: string; employee_id?: number; employee_name: string; status: string; reason: string; count: number | null; dates: string[] }>>([]);
  const [crm, setCrm] = useState<CrmSummary>();
  const [performance, setPerformance] = useState<CrmPerformance>();
  const [profileStart, setProfileStart] = useState('');
  const [profileEnd, setProfileEnd] = useState('');
  const [profileLoading, setProfileLoading] = useState(false);
  const [profileError, setProfileError] = useState('');
  const [dialer, setDialer] = useState<DialerPayload | null>();
  const [dialerFilters, setDialerFilters] = useState<DialerFilters>();
  const [dialerLoading, setDialerLoading] = useState(false);
  const [dialerEntity, setDialerEntity] = useState<'lead' | 'deal'>('lead');
  const [dialerCategory, setDialerCategory] = useState<'0' | '2'>('0');
  const [dialerFrom, setDialerFrom] = useState('');
  const [dialerTo, setDialerTo] = useState('');
  const [dialerStages, setDialerStages] = useState<string[]>([]);
  const [audit, setAudit] = useState<Audit[]>([]);
  const [error, setError] = useState('');
  const [toast, setToast] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [modal, setModal] = useState<Modal>();
  const [now, setNow] = useState(Date.now());
  const [offset, setOffset] = useState(0);
  const [filter, setFilter] = useState('all');
  const [showAudit, setShowAudit] = useState(false);
  const generation = useRef(0);
  // Identity changes even when navigation returns to the same date/employee.
  const view = useMemo(() => ({ day, employeeId }), [day, employeeId]);
  const currentView = useRef(view);
  currentView.current = view;
  const latestRefresh = useRef<(scope?: string) => Promise<void>>(async () => {});
  const supervisor = me?.user.role === 'supervisor';

  useEffect(() => {
    let alive = true;
    client<Me>('/me').then(value => {
      if (!alive) return;
      setMe(value); setDay(value.today); setProfileStart(value.today); setProfileEnd(value.today); setDialerFrom(value.today); setDialerTo(value.today);
      if (value.user.role === 'manager') setEmployeeId(value.user.id);
    }).catch(e => { if (alive) { setError(e.message); setLoading(false); } });
    return () => { alive = false; };
  }, [client]);

  const refresh = useCallback(async (scope = 'all') => {
    if (!me || view !== currentView.current) return;
    const query = employeeId ? `?employee_id=${employeeId}` : '';
    const context = generation.current;
    const load = async <T,>(key: string, path: string, apply: (data: T) => void, historyPlanId?: number) => {
      const version = (resourceVersions.current[key] || 0) + 1;
      resourceVersions.current[key] = version;
      const current = () => view === currentView.current && context === generation.current && version === resourceVersions.current[key] && (historyPlanId === undefined || historyPlanId === selectedPlanId.current);
      setLoadErrors(old => { const next = { ...old }; delete next[key]; return next; });
      try {
        const value = await client<T>(path);
        if (current()) apply(value);
      } catch (e) { if (current()) setLoadErrors(old => ({ ...old, [key]: (e as Error).message })); }
    };
    if ((scope === 'all' || scope === 'employees') && supervisor)
      void load<Employee[]>('employees', '/employees', staff => { setEmployees(staff); setEmployeesLoaded(true); });
    if (scope === 'all' || scope === 'requests') void load<GoalRequest[]>('requests', '/requests', setRequests);
    if (scope === 'all' || scope === 'distributions') void load<typeof distributions>('distributions', `/distributions${query}`, setDistributions);
    if ((scope === 'all' || scope === 'crm') && employeeId)
      void load<CrmSummary>('crm', `/crm/summary?employee_id=${employeeId}&day=${day}`, setCrm);
    else if (!employeeId) setCrm(undefined);
    if (scope === 'history') {
      const id = selectedPlanId.current;
      if (id !== undefined) await load<Audit[]>('history', `/plans/${id}/history`, setAudit, id);
      return;
    }
    if (scope !== 'all' && scope !== 'plans') return;
    const version = (resourceVersions.current.plans || 0) + 1;
    resourceVersions.current.plans = version;
    const current = () => view === currentView.current && context === generation.current && version === resourceVersions.current.plans;
    setLoading(true);
    setLoadErrors(old => { const next = { ...old }; delete next.plans; return next; });
    try {
      const allPlans = await client<Plan[]>(`/plans${query}`);
      if (!current()) return;
      setPlans(allPlans);
      const found = allPlans.find(p => p.day === day && p.employee_id === employeeId);
      const detail = found ? await client<Plan>(`/plans/${found.id}${found.status === 'closed' ? '/report' : ''}`) : undefined;
      if (!current()) return;
      if (detail && (detail.id !== found!.id || detail.day !== day || detail.employee_id !== employeeId))
        throw new Error('Сервер вернул план другой даты или сотрудника. Повторите загрузку.');
      setPlan(detail); setAudit([]);
      resourceVersions.current.history = (resourceVersions.current.history || 0) + 1;
      setLoadErrors(old => { const next = { ...old }; delete next.history; return next; });
      selectedPlanId.current = detail?.id;
      if (detail) {
        setOffset(detail.server_time * 1000 - Date.now());
        void load<Audit[]>('history', `/plans/${detail.id}/history`, setAudit, detail.id);
      }
    } catch (e) { if (current()) setLoadErrors(old => ({ ...old, plans: (e as Error).message })); }
    finally { if (current()) setLoading(false); }
  }, [client, me, employeeId, day, supervisor, view]);
  latestRefresh.current = refresh;

  useEffect(() => {
    if (!me) return;
    setLoading(true); setPlan(undefined); setError('');
    setCrm(undefined);
    selectedPlanId.current = undefined;
    setAudit([]); setShowAudit(false); setModal(undefined);
    resourceVersions.current.history = (resourceVersions.current.history || 0) + 1;
    setLoadErrors(old => { const next = { ...old }; delete next.history; return next; });
    refresh().catch(e => { setError(e.message); setLoading(false); });
    return () => { generation.current++; };
  }, [refresh, me]);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    if (!me || modal || busy || loading) return;
    const update = () => { if (!document.hidden) refresh().catch(e => setError(e.message)); };
    const id = setInterval(update, 30000);
    window.addEventListener('focus', update);
    return () => { clearInterval(id); window.removeEventListener('focus', update); };
  }, [refresh, me, modal, busy, loading]);
  useEffect(() => { if (!toast) return; const id = setTimeout(() => setToast(''), 4000); return () => clearTimeout(id); }, [toast]);

  const loadPerformance = useCallback(async () => {
    if (!me || supervisor || !profileStart || !profileEnd) return;
    if (profileStart > profileEnd) { setProfileError('Дата начала не может быть позже даты окончания.'); return; }
    setProfileLoading(true); setProfileError('');
    try {
      const params = new URLSearchParams({ start: profileStart, end: profileEnd });
      setPerformance(await client<CrmPerformance>(`/crm/performance?${params}`));
    } catch (e) { setProfileError((e as Error).message); }
    finally { setProfileLoading(false); }
  }, [client, me, supervisor, profileStart, profileEnd]);

  useEffect(() => { if (section === 'profile') void loadPerformance(); }, [section, loadPerformance]);

  const loadDialer = useCallback(async (quiet = false) => {
    if (!me) return;
    if (!quiet) setDialerLoading(true);
    try {
      const [current, available] = await Promise.all([
        client<DialerPayload | null>('/dialer/session'),
        dialerFilters ? Promise.resolve(dialerFilters) : client<DialerFilters>('/dialer/filters'),
      ]);
      setDialer(current); setDialerFilters(available);
    } catch (e) { if (!quiet) setError((e as Error).message); }
    finally { if (!quiet) setDialerLoading(false); }
  }, [client, me, dialerFilters]);

  useEffect(() => {
    if (section !== 'calls' || !me) return;
    void loadDialer();
    const timer = setInterval(() => { if (!document.hidden) void loadDialer(true); }, 2500);
    return () => clearInterval(timer);
  }, [section, me, loadDialer]);

  async function act(path: string, method = 'POST', data?: unknown, message = 'Сохранено') {
    if (busy || view !== currentView.current) return;
    const operationView = view;
    const operationPlanId = selectedPlanId.current;
    const samePlan = () => operationView === currentView.current && operationPlanId === selectedPlanId.current;
    setBusy(true); setError('');
    try {
      await client(path, method, data);
      const unchanged = samePlan();
      if (unchanged) setModal(undefined);
      // The mutation belongs to its original server-side plan. Reload only the
      // current view; never run the refresh closure captured before navigation.
      await latestRefresh.current();
      setToast(unchanged ? message : `${message}. Операция относится к ранее выбранному плану.`);
    }
    catch (e) {
      if (samePlan()) setError((e as Error).message);
      else setToast(`Операция в предыдущем плане: ${(e as Error).message}`);
    }
    finally { setBusy(false); }
  }
  async function dialerAct(path: string, data?: unknown, message = 'Готово') {
    if (busy) return;
    setBusy(true); setError('');
    try {
      const value = await client<DialerPayload>(path, 'POST', data);
      setDialer(value); setToast(message);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  function selectPlan(p: Plan, destination = 'plan') { setEmployeeId(p.employee_id); setDay(p.day); setSection(destination); setShowAudit(false); }
  const createPlan = (fromPrevious: boolean) => act('/plans', 'POST', { day, employee_id: employeeId, from_previous: fromPrevious }, 'План создан');
  const tasks = plan?.tasks || [];
  const selectedTasks = tasks.filter(t => filter === 'all' || t.status === filter);
  const elapsed = (t: Task) => t.actual_seconds + (t.timer_started ? Math.max(0, Math.floor((now + offset) / 1000) - t.timer_started) : 0);
  const pending = requests.filter(r => r.status === 'pending').length;
  const tabs = supervisor ? [['home', 'Обзор отдела'], ['people', 'Сотрудники'], ['calls', 'Обзвон'], ['plan', 'Планы'], ['results', 'Прогресс'], ['requests', 'Согласования'], ['history', 'История'], ['reports', 'Отчёты']] : [['home', 'Главная'], ['profile', 'Мой профиль'], ['calls', 'Обзвон'], ['plan', 'Мой план'], ['tasks', 'Задачи'], ['results', 'Мои результаты'], ['history', 'История']];

  if (!me) return <div className="welcome"><div className="brand-mark"><Icon name="plan" size={32}/></div><p className="eyebrow">ОТДЕЛ ПРОДАЖ</p><h1>Хороший день<br/>начинается с плана.</h1><p>{loading ? 'Открываем ваше рабочее пространство…' : error}</p>{!loading && <button onClick={() => window.location.reload()}>Попробовать снова</button>}<span className="muted">Планы, задачи и результаты — в одном месте</span></div>;

  const renderSummary = () => <div className="metrics">
    <div><span>Задачи</span><strong>{plan?.summary.done || 0}<small> / {tasks.length}</small></strong><span>выполнено</span></div>
    <div><span>План на день</span><strong>{plan?.summary.planned_minutes || 0}<small> мин</small></strong><span>запланировано</span></div>
    <div><span>В работе</span><strong>{Math.floor(tasks.reduce((s, t) => s + elapsed(t), 0) / 60)}<small> мин</small></strong><span>фактически</span></div>
  </div>;
  const rubles = (value: number) => new Intl.NumberFormat('ru-RU', { style: 'currency', currency: 'RUB', maximumFractionDigits: 0 }).format(value);
  const renderCrm = () => crm && <section className="card crm-summary"><div className="section-heading"><h3>Обработка лидов и звонки</h3><span className="badge">Только чтение</span></div>{!crm.available ? <p className="notice">{crm.message}</p> : <div className="crm-grid"><div><span>Обработано лидов</span><strong>{crm.leads?.processed || 0}</strong><small>{crm.leads && Object.entries(crm.leads.stages).length ? Object.entries(crm.leads.stages).map(([stage, count]) => `${stage}: ${count}`).join(' · ') : 'Первый выход из стадии «Не обработан»'}</small></div><div><span>Звонков за день</span><strong>{crm.calls?.total || 0}</strong><small>Исходящих: {crm.calls?.outbound || 0} · дозвоны: {crm.calls?.answered || 0} · от 3 минут: {crm.calls?.long_calls || 0}</small>{crm.calls && !crm.calls.configured && <small>Нет привязки номера МегаФона</small>}</div><div><span>Общая длительность</span><strong>{minutes(crm.calls?.duration_seconds || 0)}</strong><small>Сумма состоявшихся исходящих звонков</small></div>{(crm.pipelines || []).map(p => <div key={p.category_id}><span>{p.category_id === 0 ? 'Сделки · Продажи' : `Сделки · Категория ${p.category_id}`}</span><strong>{p.total}</strong><small>Сумма: {rubles(p.amount)}</small></div>)}</div>}</section>;
  const setProfilePreset = (preset: 'today' | 'week' | 'month') => {
    const end = new Date(`${me.today}T12:00:00+03:00`);
    const start = new Date(end);
    if (preset === 'week') start.setDate(start.getDate() - 6);
    if (preset === 'month') start.setDate(1);
    const value = (date: Date) => date.toLocaleDateString('sv-SE', { timeZone: 'Europe/Moscow' });
    setProfileStart(value(start)); setProfileEnd(value(end));
  };
  const renderProfile = () => <>
    <section className="card profile-hero"><div className="row"><div className="avatar profile-avatar">{me.user.name[0]}</div><div><p className="eyebrow">ЛИЧНАЯ СТАТИСТИКА</p><h2>{me.user.name}</h2><p className="muted">Данные по обработке лидов и звонкам</p></div></div></section>
    <section className="card period-card"><div className="section-heading"><h3>Период</h3><span className="badge">Москва</span></div><div className="period-presets"><button className="secondary" onClick={() => setProfilePreset('today')}>Сегодня</button><button className="secondary" onClick={() => setProfilePreset('week')}>7 дней</button><button className="secondary" onClick={() => setProfilePreset('month')}>Этот месяц</button></div><div className="period-fields"><label>С<input type="date" value={profileStart} onChange={e => setProfileStart(e.target.value)}/></label><label>По<input type="date" value={profileEnd} onChange={e => setProfileEnd(e.target.value)}/></label><button onClick={() => void loadPerformance()} disabled={profileLoading}>Показать</button></div></section>
    {profileError && <p className="error" role="alert">{profileError}</p>}
    {profileLoading ? <div className="empty"><div className="spinner"/><p>Загружаем статистику…</p></div> : performance && (!performance.available ? <p className="notice">{performance.message}</p> : <><div className="profile-stats"><article className="card"><span>Обработано лидов</span><strong>{performance.leads?.processed || 0}</strong></article><article className="card"><span>Общая длительность</span><strong>{minutes(performance.calls?.duration_seconds || 0)}</strong></article><article className="card"><span>Всего звонков</span><strong>{performance.calls?.total || 0}</strong></article><article className="card"><span>Исходящих</span><strong>{performance.calls?.outbound || 0}</strong></article><article className="card"><span>Успешных</span><strong>{performance.calls?.answered || 0}</strong></article><article className="card"><span>Дольше 3 минут</span><strong>{performance.calls?.long_calls || 0}</strong></article></div><section className="card stage-summary"><div className="section-heading"><h3>Стадии обработанных лидов</h3><span className="muted">{dateLabel(performance.period.start)} — {dateLabel(performance.period.end)}</span></div><div className="stage-chips">{Object.entries(performance.leads?.stages || {}).map(([stage, count]) => <span key={stage}>{stage} <b>{count}</b></span>)}{!Object.keys(performance.leads?.stages || {}).length && <p className="muted">За выбранный период обработанных лидов нет.</p>}</div></section></>)}
  </>;
  const dialerStatus: Record<string, string> = { pending: 'Ожидает', starting: 'Запускаем', calling: 'Идёт вызов', connected: 'Соединение', awaiting_confirmation: 'Нужен результат', answered: 'Клиент ответил', no_answer: 'Не ответил', busy: 'Занято', unavailable: 'Недоступен', voicemail: 'Автоответчик', skipped: 'Пропущен', postponed: 'Отложен', error: 'Ошибка запуска' };
  const availableStages = dialerFilters ? (dialerEntity === 'lead' ? dialerFilters.lead : dialerFilters.deal[dialerCategory]) : [];
  const renderDialer = () => {
    const active = dialer && ['active', 'paused'].includes(dialer.session.status);
    const current = dialer?.current;
    const buildQueue = async (event: FormEvent) => {
      event.preventDefault();
      if (supervisor && !employeeId) { setError('Выберите менеджера для обзвона.'); return; }
      await dialerAct('/dialer/sessions', {
        employee_id: employeeId, entity_type: dialerEntity, date_from: dialerFrom, date_to: dialerTo,
        category_id: dialerEntity === 'deal' ? dialerCategory : null, stage_ids: dialerStages,
        auto_advance: true,
      }, 'Очередь сформирована');
    };
    return <div className="dialer-page">
      {!active && <section className="card dialer-builder"><div className="section-heading compact"><div><h2>Сформировать очередь</h2><p className="muted">Только клиенты выбранного менеджера с номером телефона</p></div><span className="icon-tile blue"><Icon name="calls"/></span></div>
        <form onSubmit={buildQueue}>
          {supervisor && <label>Менеджер<select value={employeeId || ''} onChange={e => setEmployeeId(e.target.value ? Number(e.target.value) : undefined)} required><option value="">Выберите менеджера</option>{employees.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}</select></label>}
          <div className="dialer-form-grid"><label>Что обзваниваем<select value={dialerEntity} onChange={e => { setDialerEntity(e.target.value as 'lead' | 'deal'); setDialerStages([]); }}><option value="lead">Лиды</option><option value="deal">Сделки</option></select></label>{dialerEntity === 'deal' && <label>Воронка<select value={dialerCategory} onChange={e => { setDialerCategory(e.target.value as '0' | '2'); setDialerStages([]); }}><option value="0">Продажи</option><option value="2">Категория 2</option></select></label>}<label>Созданы с<input type="date" value={dialerFrom} onChange={e => setDialerFrom(e.target.value)} required/></label><label>Созданы по<input type="date" value={dialerTo} onChange={e => setDialerTo(e.target.value)} required/></label></div>
          <fieldset className="dialer-stages"><legend>Стадии <span className="muted">· ничего не выбирайте, чтобы взять все</span></legend>{availableStages.map(stage => <label className="checkbox" key={stage.id}><input type="checkbox" checked={dialerStages.includes(stage.id)} onChange={e => setDialerStages(old => e.target.checked ? [...old, stage.id] : old.filter(id => id !== stage.id))}/><span>{stage.name}</span></label>)}</fieldset>
          <button disabled={busy || dialerLoading || !dialerFilters}><Icon name="calls"/>Загрузить очередь</button>
        </form>
      </section>}
      {dialerLoading && !dialer ? <div className="empty"><div className="spinner"/><p>Загружаем обзвон…</p></div> : dialer && <>
        <div className="dialer-stats"><article className="card"><span>Всего</span><strong>{dialer.summary.total}</strong></article><article className="card"><span>Обработано</span><strong>{dialer.summary.completed}</strong></article><article className="card"><span>Ответили</span><strong>{dialer.summary.answered}</strong></article><article className="card"><span>Осталось</span><strong>{dialer.summary.remaining}</strong></article></div>
        {active && current && <section className="card dialer-current"><div className="section-heading compact"><div><p className="eyebrow">ТЕКУЩИЙ КЛИЕНТ · #{current.position}</p><h2>{current.client_name || `Клиент #${current.entity_id}`}</h2></div><span className={`badge ${current.status}`}>{dialerStatus[current.status] || current.status}</span></div><div className="dialer-contact"><div><span>Телефон</span><strong>+{current.phone}</strong></div><div><span>Стадия</span><strong>{current.stage_name || '—'}</strong></div><a href={current.bitrix_url} target="_blank" rel="noreferrer">Открыть в Bitrix24 →</a></div>
          <div className="dialer-actions">
            {['pending', 'error'].includes(current.status) && <button disabled={busy} onClick={() => void dialerAct(`/dialer/sessions/${dialer.session.id}/start`, undefined, 'Звонок запущен')}><Icon name="play"/>{current.status === 'error' ? 'Повторить звонок' : 'Начать обзвон'}</button>}
            {['starting', 'calling', 'connected'].includes(current.status) && <div className="notice"><span className="connection-dot"/>Ожидаем события от МегаФона. Телефон менеджера должен сейчас звонить.</div>}
            {current.status === 'awaiting_confirmation' && <><button disabled={busy} onClick={() => void dialerAct(`/dialer/sessions/${dialer.session.id}/result`, { item_id: current.id, result: 'answered' }, 'Дозвон подтверждён')}><Icon name="check"/>Клиент ответил</button><button className="secondary" disabled={busy} onClick={() => void dialerAct(`/dialer/sessions/${dialer.session.id}/result`, { item_id: current.id, result: 'failed' }, 'Недозвон сохранён')}>Не дозвонились</button><button className="secondary" disabled={busy} onClick={() => void dialerAct(`/dialer/sessions/${dialer.session.id}/result`, { item_id: current.id, result: 'voicemail' }, 'Автоответчик сохранён')}>Автоответчик</button></>}
            {dialer.session.status === 'paused' && current.status === 'pending' && <button disabled={busy} onClick={() => void dialerAct(`/dialer/sessions/${dialer.session.id}/start`, undefined, 'Обзвон продолжен')}><Icon name="arrow"/>Следующий клиент</button>}
            {!['starting', 'calling', 'connected', 'awaiting_confirmation'].includes(current.status) && <button className="quiet" disabled={busy} onClick={() => void dialerAct(`/dialer/sessions/${dialer.session.id}/result`, { item_id: current.id, result: 'skipped' }, 'Клиент пропущен')}>Пропустить</button>}
          </div>
        </section>}
        {active && !current && <section className="card empty"><Icon name="check" size={40}/><h3>Очередь обработана</h3><p>Все доступные клиенты пройдены.</p></section>}
        <section className="card dialer-queue"><div className="section-heading compact"><div><h2>Очередь</h2><p className="muted">{dialer.session.manager_name} · {dateLabel(dialer.session.date_from)} — {dateLabel(dialer.session.date_to)}</p></div>{active && <button className="quiet" disabled={busy} onClick={() => void dialerAct(`/dialer/sessions/${dialer.session.id}/cancel`, undefined, 'Очередь отменена')}>Завершить</button>}</div><div className="dialer-items">{dialer.items.map(item => <a href={item.bitrix_url} target="_blank" rel="noreferrer" className={`dialer-item ${item.id === current?.id ? 'selected' : ''}`} key={item.id}><span className="dialer-position">{item.position}</span><span><b>{item.client_name || `Клиент #${item.entity_id}`}</b><small>{item.stage_name || 'Без стадии'} · +{item.phone}</small></span><span className={`badge ${item.status}`}>{dialerStatus[item.status] || item.status}</span></a>)}</div></section>
      </>}
    </div>;
  };
  const renderHistory = () => <section className="card audit"><h3>История изменений</h3>{audit.length === 0 && <p className="muted">{loadErrors.history ? 'История не загружена. Повторите загрузку выше.' : 'Изменений пока нет.'}</p>}{audit.map(a => <article key={a.id}>
    <div><strong>{actionName[a.action] || 'Изменение'}</strong><small>{a.actor_name} · {new Date(a.created_at * 1000).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' })}</small></div>
    {a.entity === 'task' && <p>{String(a.after_json?.title || a.before_json?.title || `Задача №${a.entity_id}`)}</p>}
    {Object.entries(a.after_json || {}).filter(([key, val]) => key in fieldName && JSON.stringify(val) !== JSON.stringify(a.before_json?.[key])).map(([key, val]) => <p className="change" key={key}><span>{fieldName[key]}</span><span>{a.before_json && <><s>{String(a.before_json[key] ?? '—')}</s> → </>}{String(val ?? '—')}</span></p>)}
  </article>)}</section>;
  const renderReport = (p: Plan) => <section className="card report"><div className="section-heading"><h3>{p.status === 'closed' ? 'Итоговый отчёт' : 'Результаты дня'}</h3><span className="badge">{statusName[p.status]}</span></div><p className="muted">{p.employee_name} · {dateLabel(p.day)}</p>
    <div className="report-scroll"><table><thead><tr><th>Задача</th><th>Плановое время</th><th>Фактическое время</th><th>Результат</th></tr></thead><tbody>{p.tasks.map(t => <tr key={t.id}><td>{t.title}<small>{statusName[t.status]}</small></td><td data-label="Плановое время">{t.planned_minutes} мин</td><td data-label="Фактическое время">{minutes(p.status === 'closed' ? t.actual_seconds : elapsed(t))}</td><td data-label="Результат">{t.kind === 'quantitative' && <strong>{t.actual_value} / {t.target} </strong>}{t.result || (t.status === 'done' ? 'Выполнено' : '—')}{t.comment && <small>{t.comment}</small>}</td></tr>)}</tbody></table></div>
    <p className="report-total">Выполнено: {p.summary.done} · Перенесено: {p.summary.moved}</p>
    {p.status === 'closed' && <p className="notice">Отчёт сохранён при закрытии дня. Редактирование недоступно.</p>}
    {p.initial_snapshot && <details><summary>Первоначальный утверждённый план</summary>{p.initial_snapshot.tasks.map(t => <p key={t.id}>{t.title} · {t.planned_minutes} мин{t.target != null ? ` · цель ${t.target}` : ''}</p>)}</details>}
  </section>;

  const todayPlans = plans.filter(p => p.day === me.today);
  const dashboardPlans = supervisor ? todayPlans : plan ? [plan] : [];
  const dashboardTotal = dashboardPlans.reduce((sum, item) => sum + item.summary.total, 0);
  const dashboardDone = dashboardPlans.reduce((sum, item) => sum + item.summary.done, 0);
  const dashboardMinutes = dashboardPlans.reduce((sum, item) => sum + item.summary.planned_minutes, 0);
  const dashboardSeconds = dashboardPlans.reduce((sum, item) => sum + item.summary.actual_seconds, 0);
  const activeTask = tasks.find(task => task.timer_started !== null) || tasks.find(task => task.status === 'in_progress');
  const dashboardPercent = dashboardTotal ? Math.round(dashboardDone / dashboardTotal * 100) : 0;
  const renderDashboard = () => <>
    <div className="stats-grid">
      <article className="stat card"><div className="stat-top"><span>{supervisor ? 'Сотрудники с планом' : 'Задачи на сегодня'}</span><span className="icon-tile blue"><Icon name={supervisor ? 'people' : 'tasks'}/></span></div><strong className="stat-value">{supervisor ? todayPlans.length : dashboardTotal}</strong><small>{supervisor ? `из ${employees.filter(e => e.active).length} активных менеджеров` : 'в текущем плане'}</small></article>
      <article className="stat card"><div className="stat-top"><span>Завершено задач</span><span className="icon-tile green"><Icon name="check"/></span></div><strong className="stat-value">{dashboardDone} <small>/ {dashboardTotal}</small></strong><small>{dashboardPercent}% выполнения</small></article>
      <article className="stat card"><div className="stat-top"><span>Плановое время</span><span className="icon-tile orange"><Icon name="plan"/></span></div><strong className="stat-value">{dashboardMinutes} <small>мин</small></strong><small>на выбранный рабочий день</small></article>
      <article className="stat card"><div className="stat-top"><span>Фактическое время</span><span className="icon-tile violet"><Icon name="history"/></span></div><strong className="stat-value">{Math.floor(dashboardSeconds / 3600)}:{String(Math.floor(dashboardSeconds % 3600 / 60)).padStart(2, '0')}</strong><small>по серверному учёту задач</small></article>
    </div>
    {supervisor ? <section className="card team-overview"><div className="section-heading compact"><div><h2>Команда сегодня</h2><p className="muted">Реальные планы отдела на {dateLabel(me.today)}</p></div><button className="text-button" onClick={() => setSection('people')}>Все сотрудники →</button></div>
      <div className="employee-grid">{todayPlans.map(item => <article className="employee-card" key={item.id}><div className="employee-top"><span className="avatar">{item.employee_name[0]}</span><div><h3>{item.employee_name}</h3><small>{item.summary.done} из {item.summary.total} задач</small></div><span className={`badge ${item.status}`}>{statusName[item.status]}</span></div><div className="employee-numbers"><div><strong>{item.summary.done} / {item.summary.total}</strong><small>задач завершено</small></div><div><strong>{Math.floor(item.summary.actual_seconds / 60)} мин</strong><small>фактическое время</small></div></div><progress max={item.summary.total || 1} value={item.summary.done}/><button className="text-button" onClick={() => selectPlan(item)}>Открыть план →</button></article>)}</div>
      {!todayPlans.length && <div className="empty small"><p>На сегодня планы ещё не созданы.</p></div>}
    </section> : <div className="dashboard-grid"><section className="card current-card"><div className="section-heading compact"><h2>Текущая задача</h2>{activeTask ? <span className="live-label"><i/>В работе</span> : <span className="badge">Нет активной</span>}</div>{activeTask ? <><p className="eyebrow">ИЗ ПЛАНА НА СЕГОДНЯ</p><h3>{activeTask.title}</h3><p className="muted">{activeTask.description || kindName[activeTask.kind]}</p><div className="current-main"><div className="timer">{clock(elapsed(activeTask))}<small>фактическое время</small></div>{activeTask.kind === 'quantitative' && <div className="current-target"><strong>{activeTask.actual_value}<span> / {activeTask.target}</span></strong><small>факт / цель</small></div>}</div><div className="current-actions"><button className="button" disabled={busy} onClick={() => act(`/tasks/${activeTask.id}/timer`, 'POST', { action: activeTask.timer_started ? 'pause' : 'start' }, activeTask.timer_started ? 'Таймер на паузе' : 'Таймер запущен')}><Icon name={activeTask.timer_started ? 'pause' : 'play'}/>{activeTask.timer_started ? 'Пауза' : 'Начать'}</button><button className="button secondary" onClick={() => setSection('tasks')}>К задаче →</button></div></> : <div className="empty small"><p>{plan ? 'Запустите задачу из плана, когда приступите к работе.' : 'Создайте план на сегодня, чтобы начать рабочий день.'}</p><button className="text-button" onClick={() => setSection('plan')}>Открыть план →</button></div>}</section>
      <section className="card plan-card"><div className="section-heading compact"><h2>План рабочего дня</h2>{plan && <span className={`badge ${plan.status}`}>{statusName[plan.status]}</span>}</div><p className="muted">{plan ? `${plan.employee_name} · ${dateLabel(plan.day)}` : dateLabel(me.today)}</p><div className="plan-number">{dashboardPercent}<span>%</span><small>задач завершено</small></div><progress max={100} value={dashboardPercent}/><div className="plan-breakdown"><span><i className="legend green"/>Завершено <b>{dashboardDone}</b></span><span><i className="legend blue"/>Осталось <b>{Math.max(0, dashboardTotal - dashboardDone)}</b></span></div><div className="card-foot">План: {dashboardMinutes} мин <span>Факт: {minutes(dashboardSeconds)}</span></div></section></div>}
    <section className="metrics-prompt"><span className="icon-tile blue"><Icon name="results"/></span><div><h3>{supervisor ? 'Все детали доступны в рабочих разделах' : 'Следующий шаг всегда перед глазами'}</h3><p>{supervisor ? 'Откройте сотрудника, планы или согласования.' : 'План, задачи, результаты и история используют реальные данные Lead Flow.'}</p></div><button className="text-button" onClick={() => setSection(supervisor ? 'people' : 'plan')}>Перейти →</button></section>
  </>;

  return <div className="shell">
    {import.meta.env.DEV && demo && <div className="demo-banner">ДЕМОНСТРАЦИЯ · Пример интерфейса, данные вымышлены · <a href="/preview.html?role=manager">Менеджер</a> / <a href="/preview.html?role=supervisor">Руководитель</a></div>}
    <div className="workspace"><aside className="sidebar"><button className="brand" onClick={() => setSection('home')}><span className="brand-mark">П</span><span><b>План дня</b><small>РАБОЧЕЕ ПРОСТРАНСТВО</small></span></button><p className="nav-caption">{supervisor ? 'УПРАВЛЕНИЕ ОТДЕЛОМ' : 'ЛИЧНЫЙ КАБИНЕТ'}</p><nav aria-label="Разделы">{tabs.map(([key, title]) => <button key={key} className={section === key ? 'active' : ''} onClick={() => { setSection(key); setShowAudit(false); }}><Icon name={key === 'reports' ? 'results' : key === 'profile' ? 'people' : key}/><span>{title}</span>{key === 'requests' && pending > 0 && <em>{pending}</em>}</button>)}</nav><div className="sidebar-bottom"><span className="connection-dot"/>Lead Flow подключён<small>{me.crm.connected ? 'Bitrix24 и телефония доступны' : me.crm.message}</small></div><button className="profile" onClick={() => !supervisor && setSection('profile')}><div className="avatar">{me.user.name[0]}</div><div><b>{me.user.name}</b><span>{supervisor ? 'Руководитель' : 'Менеджер · открыть профиль'}</span></div></button></aside>
    <div className="main-shell"><header className="topbar"><span>Рабочее пространство <span className="crumb">/ {tabs.find(t => t[0] === section)?.[1]}</span></span><div><span className="top-date">{dateLabel(me.today)}</span>{!isTelegramSession() && <button className="quiet web-logout" onClick={webLogout}>Выйти</button>}<span className="avatar small">{me.user.name[0]}</span></div></header><main className="page-content">
      <div className="page-title"><div><p className="eyebrow">{supervisor ? 'КОМАНДА И РЕЗУЛЬТАТ' : 'РАБОЧЕЕ ПРОСТРАНСТВО'}</p><h1>{tabs.find(t => t[0] === section)?.[1]}</h1></div><button className="quiet" disabled={busy || loading || profileLoading || dialerLoading} onClick={() => section === 'profile' ? void loadPerformance() : section === 'calls' ? void loadDialer() : refresh().catch(e => setError(e.message))}>Обновить</button></div>
      {error && <div className="error" role="alert">{error}<button className="icon-button" aria-label="Закрыть ошибку" onClick={() => setError('')}><Icon name="close"/></button></div>}
      {Object.entries(loadErrors).map(([key, message]) => <div className="error" role="alert" key={key}><span>{({ employees: 'Сотрудники', plans: 'Планы', requests: 'Согласования', distributions: 'Уведомления', history: 'История', crm: 'Bitrix24 и звонки' } as Record<string, string>)[key]}: {message}</span><button className="secondary" onClick={() => void refresh(key)}>Повторить загрузку</button></div>)}
      {toast && <div className="toast" role="status"><Icon name="check"/>{toast}</div>}

      {section === 'home' ? renderDashboard() : section === 'profile' && !supervisor ? renderProfile() : section === 'calls' ? renderDialer() : section === 'people' && supervisor ? <>
        <section className="hero"><p className="eyebrow">ВМЕСТЕ К ЦЕЛИ</p><h2>У каждого — ясный план.<br/>У вас — общая картина.</h2><p>{employeesLoaded ? `${employees.filter(e => e.active).length} сотрудника в команде` : loadErrors.employees ? 'Сотрудники не загружены' : 'Загрузка сотрудников'}</p><button className="light" onClick={() => setModal({ type: 'employee' })}><Icon name="plus"/>Добавить сотрудника</button></section>
        <div className="section-heading"><h2>Команда</h2><span className="muted">{dateLabel(me.today)}</span></div>
        <div className="employee-grid">{employees.map(e => <section className="card employee" key={e.id}><div className="row"><div className="avatar">{e.name[0]}</div><div><h3>{e.name}</h3><span className="muted">{!e.active ? 'Доступ выключен' : e.telegram_id ? 'Менеджер' : 'Ожидает подключения Telegram'}</span></div></div><div className="actions"><button className="secondary" onClick={() => { setEmployeeId(e.id); setDay(me.today); setSection('plan'); }}>Открыть план <Icon name="arrow"/></button><button className="quiet" onClick={() => setModal({ type: 'employee', employee: e })}>Изменить</button></div></section>)}</div>
      </> : section === 'requests' ? loadErrors.requests ? <p className="notice">Согласования не загружены. Повторите загрузку выше.</p> : <Requests requests={requests} supervisor={!!supervisor} busy={busy} act={act}/> : <>
        <div className="toolbar">{supervisor && <label>Сотрудник<select aria-label="Сотрудник" value={employeeId || ''} onChange={e => setEmployeeId(e.target.value ? Number(e.target.value) : undefined)}><option value="">Все менеджеры</option>{employees.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}</select></label>}<label>Дата · Москва<input aria-label="Дата плана" type="date" value={day} onChange={e => { if (e.target.value) setDay(e.target.value); }}/></label>{day !== me.today && <button className="quiet" onClick={() => setDay(me.today)}>Сегодня</button>}</div>
        {distributions.filter(d => d.status !== 'imported').map(d => <aside className="notice" key={`${d.id}-${d.employee_id || 'system'}`}><strong>Требует рассмотрения</strong><p>{d.employee_name}{d.day && ` · ${dateLabel(d.day)}`}</p><p>{d.reason === 'plan_already_confirmed_or_closed' ? 'План уже подтверждён или день закрыт. Новое распределение сохранено отдельно, план не изменён.' : d.reason === 'import_retry' ? 'Запись не импортирована. Система повторяет попытки.' : 'Нужно проверить активность и привязку сотрудников. Распределение не применено автоматически.'}</p>{d.count !== null && <p>Назначено: {d.count} · Даты: {d.dates.map(dateLabel).join(', ')}</p>}</aside>)}
        {employeeId && ['plan', 'tasks', 'results'].includes(section) && renderCrm()}
        {loading ? <div className="empty"><div className="spinner"/><p>Загружаем планы…</p></div> : loadErrors.plans ? <p className="notice">Планы не удалось загрузить. Повторите загрузку выше; остальные разделы доступны.</p> : section === 'history' || section === 'reports' || (supervisor && !employeeId) ? <>
          <div className="section-heading"><h2>{section === 'history' ? 'Ваши рабочие дни' : section === 'reports' ? 'Закрытые дни' : 'Планы команды'}</h2><span className="muted">{section === 'history' || section === 'reports' ? 'Последние 366 планов' : dateLabel(day)}</span></div>
          <div className="plan-list">{plans.filter(p => section === 'reports' ? p.status === 'closed' : section === 'history' || p.day === day).map(p => <button className="card plan-row" key={p.id} onClick={() => selectPlan(p, section === 'reports' ? 'results' : 'plan')}><div><b>{dateLabel(p.day)}</b><span>{p.employee_name}</span></div><div><span className={`badge ${p.status}`}>{statusName[p.status]}</span><small>{p.summary.done} из {p.summary.total} задач · {p.summary.planned_minutes} мин</small></div><Icon name="arrow"/></button>)}</div>
          {!plans.some(p => section === 'reports' ? p.status === 'closed' : section === 'history' || p.day === day) && <div className="empty"><Icon name="plan" size={40}/><h3>Планов пока нет</h3><p>{supervisor ? 'Выберите сотрудника, чтобы создать его план.' : 'Создайте свой первый план во вкладке «Мой план дня».'}</p></div>}
        </> : !plan ? <section className="card empty"><div className="empty-symbol"><Icon name="plan" size={38}/></div><h2>Дадим этому дню направление</h2><p>{dateLabel(day)}{supervisor && ` · ${employees.find(e => e.id === employeeId)?.name}`}</p><p className="muted">Начните с чистого листа или возьмите за основу<br/>предыдущий рабочий день.</p><div className="actions"><button disabled={busy} onClick={() => createPlan(false)}><Icon name="plus"/>Создать план</button><button disabled={busy} className="secondary" onClick={() => createPlan(true)}>Из предыдущего дня</button></div></section> : <>
          <div className="plan-heading"><div><h2>{dateLabel(day)}</h2><p>{plan.employee_name} <span className={`badge ${plan.status}`}>{statusName[plan.status]}</span></p></div>{plan.status !== 'closed' && section !== 'results' && <button disabled={busy} onClick={() => setModal({ type: 'task' })}><Icon name="plus"/>Новая задача</button>}</div>
          {renderSummary()}
          {section === 'results' || plan.status === 'closed' ? renderReport(plan) : <>
            <div className="section-heading"><h2>Фокус на сегодня</h2><span className="muted">{tasks.length} задач</span></div>
            <div className="filters" aria-label="Фильтр задач">{[['all', 'Все'], ['todo', 'К выполнению'], ['in_progress', 'В работе'], ['done', 'Готово'], ['moved', 'Перенесено']].map(([key, title]) => <button className={filter === key ? 'selected' : ''} key={key} onClick={() => setFilter(key)}>{title}</button>)}</div>
            <div className="task-list">{selectedTasks.map(t => <section key={t.id} className={`card task ${t.status}`}>
              <div className="task-top"><span className={`task-kind ${t.kind}`}>{kindName[t.kind]}</span><span className={`badge ${t.status}`}>{statusName[t.status]}</span></div>
              <h3>{t.title}</h3>{t.description && <p className="description">{t.description}</p>}
              <div className="task-numbers"><div><span>Плановое время</span><b>{t.planned_minutes} мин</b></div><div><span>Затрачено</span><b className={t.timer_started ? 'running' : ''}>{t.timer_started ? clock(elapsed(t)) : minutes(elapsed(t))}</b></div>{t.kind === 'quantitative' && <div><span>Факт / цель</span><b>{t.actual_value} <small>/ {t.target}</small></b></div>}</div>
              {t.kind === 'quantitative' && <progress max={t.target || 1} value={Math.min(t.actual_value, t.target || 1)} aria-label="Выполнение цели"/>}
              {t.distribution_source && <p className="notice">Источник: {t.distribution_source.name} · Назначено: {t.distribution_source.count} · Даты: {t.distribution_source.dates.map(dateLabel).join(', ')}</p>}
              {!!t.locked && <p className="fine">{t.distribution_target_locked ? 'Количество задано подтверждённым распределением' : 'Плановые условия установлены руководителем'}</p>}
              {t.result && <p className="task-note"><b>Результат:</b> {t.result}</p>}{t.comment && <p className="task-note">{t.comment}</p>}
              {t.kind === 'bitrix' ? <p className="notice">Внешняя задача · Только чтение {t.external_url?.startsWith('https://') && <a href={t.external_url} target="_blank" rel="noreferrer">Открыть в Bitrix24 ↗</a>}</p> : t.status !== 'moved' && <div className="task-actions">
                {!supervisor && t.status !== 'done' && (day === me.today || t.timer_started) && <><button className="timer-button" disabled={busy} onClick={() => act(`/tasks/${t.id}/timer`, 'POST', { action: t.timer_started ? 'pause' : 'start' }, t.timer_started ? 'Таймер на паузе' : 'Таймер запущен')}><Icon name={t.timer_started ? 'pause' : 'play'}/>{t.timer_started ? 'Пауза' : 'Начать'}</button>{t.timer_started && <button className="quiet" disabled={busy} onClick={() => act(`/tasks/${t.id}/timer`, 'POST', { action: 'stop' }, 'Таймер остановлен')}>Стоп</button>}</>}
                <button className="secondary" onClick={() => setModal({ type: 'task', task: t })}>{supervisor ? 'Изменить' : 'Внести результат'}</button>
                {t.status !== 'done' && <button className="quiet" disabled={busy} onClick={() => act(`/tasks/${t.id}`, 'PATCH', { revision: t.revision, status: 'done' }, 'Задача выполнена')}><Icon name="check"/>Готово</button>}
                <details className="more"><summary>Ещё</summary><div>{!supervisor && <button className="quiet" onClick={() => setModal({ type: 'goal', task: t })}>{t.distribution_target_locked ? 'Изменить плановое время' : 'Изменить цель / время'}</button>}{t.status !== 'done' && !t.distribution_target_locked && <button className="quiet" onClick={() => setModal({ type: 'move', task: t })}>Перенести задачу</button>}</div></details>
              </div>}
              {t.status === 'moved' && <p className="notice">Продолжение — в плане другой даты. Перенос не считается выполнением.</p>}
            </section>)}</div>
            {!selectedTasks.length && <div className="empty small"><p>{tasks.length ? 'Задач с таким статусом нет.' : 'Добавьте первую задачу — остальное сложится в план.'}</p></div>}
          </>}
          <div className="day-actions">{supervisor && plan.status === 'draft' && <button disabled={busy} onClick={() => act(`/plans/${plan.id}/approve`, 'POST', undefined, 'План утверждён')}>Утвердить план</button>}{plan.status !== 'closed' && day <= me.today && <button className="secondary" disabled={busy} onClick={() => setModal({ type: 'close' })}>Завершить рабочий день</button>}<button className="quiet" onClick={() => setShowAudit(!showAudit)}>{showAudit ? 'Скрыть историю изменений' : 'История изменений'}</button></div>
          {!supervisor && plan.status === 'draft' && <button disabled={busy} onClick={() => act(`/plans/${plan.id}/confirm`, 'POST', undefined, 'План подтверждён')}>Подтвердить план</button>}
          {showAudit && renderHistory()}
        </>}
        {!supervisor && (section === 'results' || section === 'plan') && requests.length > 0 && <Requests requests={requests} supervisor={false} busy={busy} act={act}/>}
      </>}
      <footer><span className="connection-dot"/>Рабочий день {me.workday?.start || '10:00'}–{me.workday?.end || '19:00'} <span>·</span> Время по Москве<p>{me.crm.message}</p></footer>
    </main></div></div>
    {modal && <ModalForm modal={modal} plan={plan} supervisor={!!supervisor} busy={busy} error={error} onClose={() => { if (!busy) { setModal(undefined); setError(''); } }} onSubmit={act}/>}
  </div>;
}

type Act = (path: string, method?: string, data?: unknown, message?: string) => Promise<void>;
function Requests({ requests, supervisor, busy, act }: { requests: GoalRequest[]; supervisor: boolean; busy: boolean; act: Act }) {
  return <section className="requests"><div className="section-heading"><h2>{supervisor ? 'Согласование изменений' : 'Мои запросы'}</h2><span className="muted">{requests.filter(r => r.status === 'pending').length} ожидают</span></div>{!requests.length && <div className="card empty small"><Icon name="requests" size={32}/><p>Запросов пока нет</p></div>}{requests.map(r => <article className="card" key={r.id}><div className="task-top"><span className="muted">{r.employee_name} · {dateLabel(r.day)}</span><span className={`badge ${r.status}`}>{statusName[r.status]}</span></div><h3>{r.title}</h3><p className="goal-change">{r.field === 'target' ? 'Цель' : 'Время, мин'}: <s>{r.old_value}</s><Icon name="arrow"/><b>{r.proposed}</b></p><p>{r.reason}</p>{r.decision_comment && <p className="notice">Решение: {r.decision_comment}</p>}{r.decided_at && <p className="fine">Рассмотрено {new Date(r.decided_at * 1000).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' })}</p>}{supervisor && r.status === 'pending' && <form onSubmit={e => { e.preventDefault(); const data = new FormData(e.currentTarget); const decision = (e.nativeEvent as SubmitEvent).submitter?.getAttribute('value'); void act(`/requests/${r.id}/decision`, 'POST', { decision, comment: data.get('comment') }, 'Решение сохранено'); }}><label>Комментарий к решению<input name="comment" maxLength={2000} placeholder="Необязательно"/></label><div className="actions"><button type="submit" name="decision" value="approved" disabled={busy}>Утвердить</button><button type="submit" name="decision" value="rejected" className="secondary" disabled={busy}>Отклонить</button></div></form>}</article>)}</section>;
}

function ModalForm({ modal, plan, supervisor, busy, error, onClose, onSubmit }: { modal: Modal; plan?: Plan; supervisor: boolean; busy: boolean; error: string; onClose: () => void; onSubmit: Act }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const task = 'task' in modal ? modal.task : undefined;
  const employee = 'employee' in modal ? modal.employee : undefined;
  const [kind, setKind] = useState(task?.kind || 'general');
  const planningLocked = !!task && !supervisor && (!!task.locked || plan?.status === 'approved');
  useEffect(() => { dialog.current?.showModal(); }, []);
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const text = (key: string) => String(data.get(key) || '');
    const number = (key: string) => Number(data.get(key));
    if (modal.type === 'close') { void onSubmit(`/plans/${plan!.id}/close`, 'POST', undefined, 'День завершён. Отчёт сохранён'); return; }
    if (modal.type === 'employee') { void onSubmit(`/employees${employee ? `/${employee.id}` : ''}`, employee ? 'PATCH' : 'POST', { name: text('name'), telegram_id: text('telegram_id') ? number('telegram_id') : null, bitrix_id: text('bitrix_id') ? number('bitrix_id') : null, ...(employee ? { active: data.has('active') } : {}) }); return; }
    if (modal.type === 'goal') { void onSubmit(`/tasks/${task!.id}/requests`, 'POST', { field: text('field'), proposed: number('proposed'), reason: text('reason') }, 'Запрос отправлен руководителю'); return; }
    if (modal.type === 'move') { void onSubmit(`/tasks/${task!.id}/move`, 'POST', { day: text('day'), reason: text('reason'), revision: task!.revision }, 'Задача перенесена'); return; }
    if (!task) { void onSubmit(`/plans/${plan!.id}/tasks`, 'POST', { title: text('title'), description: text('description'), kind, planned_minutes: number('planned_minutes'), target: kind === 'quantitative' ? number('target') : null }, 'Задача добавлена'); return; }
    const updates: Record<string, unknown> = { revision: task.revision };
    // Send only changed fields: locked planning values are never sent back implicitly.
    for (const field of ['title', 'description', 'result', 'comment', 'status'] as const) if (data.has(field) && text(field) !== task[field]) updates[field] = text(field);
    for (const field of ['planned_minutes', 'target', 'actual_value'] as const) if (data.has(field) && number(field) !== task[field]) updates[field] = number(field);
    if (data.has('actual_minutes') && number('actual_minutes') !== Math.floor(task.actual_seconds / 60)) updates.actual_minutes = number('actual_minutes');
    void onSubmit(`/tasks/${task.id}`, 'PATCH', updates);
  }
  const title = modal.type === 'close' ? 'Завершить рабочий день?' : modal.type === 'employee' ? employee ? 'Настройки сотрудника' : 'Новый сотрудник' : modal.type === 'goal' ? 'Запрос на изменение' : modal.type === 'move' ? 'Перенести задачу' : task ? 'Задача и результат' : 'Новая задача';
  return <dialog ref={dialog} onCancel={e => { e.preventDefault(); onClose(); }}><form onSubmit={submit}><div className="modal-heading"><h2>{title}</h2><button type="button" className="icon-button" aria-label="Закрыть" onClick={onClose} disabled={busy}><Icon name="close"/></button></div>
    {error && <p role="alert" className="error">{error}</p>}
    {modal.type === 'close' ? <><p>Таймеры остановятся, а итоговый отчёт сохранится. Изменять задачи и результаты этого дня после закрытия нельзя.</p><p className="notice">Незавершённых задач: {plan!.tasks.filter(t => !['done', 'moved'].includes(t.status)).length}. При необходимости сначала перенесите их на другую дату. Ожидающие запросы будут отменены.</p></> : modal.type === 'employee' ? <>
      <label>Имя<input name="name" required maxLength={80} defaultValue={employee?.name}/></label><label>Telegram ID<input name="telegram_id" inputMode="numeric" pattern="[0-9]+" defaultValue={employee?.telegram_id || ''} placeholder="Можно заполнить позже"/></label><label>Bitrix24 ID<input name="bitrix_id" inputMode="numeric" pattern="[0-9]+" defaultValue={employee?.bitrix_id || ''}/></label>{employee && <label className="checkbox"><input name="active" type="checkbox" defaultChecked={!!employee.active}/>Доступ к планировщику включён</label>}<p className="notice">Роль — менеджер. Администратор бота также должен разрешить сотруднику доступ командой /adduser.</p>
    </> : modal.type === 'goal' ? <><p>{task!.title}</p><label>Что изменить<select name="field"><option value="planned_minutes">Плановое время (минуты)</option>{task!.kind === 'quantitative' && !task!.distribution_target_locked && <option value="target">Количественную цель</option>}</select></label><p className="fine">Сейчас: {task!.planned_minutes} мин{task!.target !== null && ` · цель ${task!.target}`}</p><label>Предложенное значение<input name="proposed" type="number" min="0" max="1000000000" step="any" required/></label><label>Причина<textarea name="reason" required maxLength={2000}/></label><p className="notice">До утверждения руководителем действует прежнее значение.</p></> : modal.type === 'move' ? <><p>{task!.title}</p><label>Новая дата<input name="day" type="date" required/></label><label>Причина переноса<textarea name="reason" required maxLength={2000}/></label><p className="notice">Факт и затраченное время останутся в исходном дне. На новой дате начнётся отдельный учёт. Перенос не завершает задачу.</p></> : <>
      <label>Название<input name="title" required maxLength={180} defaultValue={task?.title} disabled={!!task?.locked && !supervisor}/></label><label>Описание<textarea name="description" maxLength={4000} defaultValue={task?.description} disabled={!!task?.locked && !supervisor}/></label>
      {!task && <label>Тип задачи<select value={kind} onChange={e => setKind(e.target.value as Task['kind'])}><option value="general">Обычная задача</option><option value="quantitative">Количественная задача</option><option value="client">Задача по клиенту</option></select></label>}
      <div className="form-grid"><label>Плановое время, мин<input name="planned_minutes" type="number" min="0" max="1440" required defaultValue={task?.planned_minutes || 0} disabled={planningLocked}/></label>{kind === 'quantitative' && <label>Плановая цель<input name="target" type="number" min="0" max="1000000000" step="any" required defaultValue={task?.target ?? 0} disabled={planningLocked || !!task?.distribution_target_locked}/></label>}</div>
      {planningLocked && <p className="notice">Для изменения планового времени или цели отправьте запрос руководителю через «Ещё» в карточке задачи.</p>}
      {task && <><div className="form-divider">Фактический результат</div><div className="form-grid">{kind === 'quantitative' && <label>Факт<input name="actual_value" type="number" min="0" max="1000000000" step="any" required defaultValue={task.actual_value}/></label>}<label>Затрачено, мин<input name="actual_minutes" type="number" min="0" max="525600" required defaultValue={Math.floor(task.actual_seconds / 60)} disabled={!!task.timer_started}/></label></div>{task.timer_started && <p className="fine">Чтобы внести время вручную, сначала остановите таймер.</p>}<label>Результат<textarea name="result" maxLength={4000} defaultValue={task.result}/></label><label>Комментарий<textarea name="comment" maxLength={4000} defaultValue={task.comment}/></label><label>Статус<select name="status" defaultValue={task.status}><option value="todo">К выполнению</option><option value="in_progress">В работе</option><option value="done">Выполнена</option></select></label></>}
    </>}
    <div className="modal-actions"><button type="button" className="secondary" onClick={onClose} disabled={busy}>Отмена</button><button type="submit" disabled={busy}>{busy ? 'Сохраняем…' : modal.type === 'close' ? 'Завершить день' : modal.type === 'goal' ? 'Отправить запрос' : modal.type === 'move' ? 'Перенести' : 'Сохранить'}</button></div>
  </form></dialog>;
}
