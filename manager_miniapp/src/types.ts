export interface Employee { id: number; name: string; telegram_id: number | null; bitrix_id: number | null; role: 'manager' | 'supervisor'; active: number }
export interface Task {
  id: number; plan_id: number; employee_id: number; title: string; description: string;
  kind: 'quantitative' | 'general' | 'client' | 'bitrix'; planned_minutes: number; target: number | null;
  actual_value: number; actual_seconds: number; elapsed_seconds: number; timer_started: number | null;
  status: 'todo' | 'in_progress' | 'done' | 'moved'; result: string; comment: string;
  locked: number; revision: number; external_url: string | null; moved_to: number | null;
  distribution_target_locked?: boolean;
  distribution_source?: { name: string; dates: string[]; count: number };
}
export interface Summary { total: number; done: number; moved: number; planned_minutes: number; actual_seconds: number }
export interface Plan {
  id: number; employee_id: number; employee_name: string; day: string; status: 'draft' | 'approved' | 'closed';
  tasks: Task[]; summary: Summary; server_time: number; initial_snapshot: Plan | null;
}
export interface GoalRequest {
  id: number; task_id: number; employee_name: string; title: string; day: string; field: 'target' | 'planned_minutes';
  old_value: number; proposed: number; reason: string; status: 'pending' | 'approved' | 'rejected' | 'cancelled';
  decision_comment: string; created_at: number; decided_at: number | null;
}
export interface Audit {
  id: number; actor_name: string; action: string; entity: string; entity_id: number; created_at: number;
  before_json: Record<string, unknown> | null; after_json: Record<string, unknown> | null;
}
export interface Me { user: Employee; today: string; timezone: string; workday?: { start: string; end: string; timezone: string; reminders_enabled: boolean }; crm: { connected: boolean; message: string } }
export interface CrmSummary {
  available: boolean; day: string; message?: string;
  calls?: { configured: boolean; total: number; outbound: number; answered: number; long_calls: number; duration_seconds: number };
  pipelines?: Array<{ category_id: number; total: number; amount: number; stages: Record<string, number> }>;
}
export interface TelegramApp {
  initData: string; colorScheme: 'light' | 'dark'; ready(): void; expand(): void;
  onEvent(event: string, callback: () => void): void; offEvent(event: string, callback: () => void): void;
}
declare global { interface Window { Telegram?: { WebApp?: TelegramApp } } }
export type Client = <T>(path: string, method?: string, data?: unknown) => Promise<T>;
