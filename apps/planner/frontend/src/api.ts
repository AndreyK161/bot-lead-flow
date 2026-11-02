import type { Client } from './types';
import { validateResponse } from './response';

export const REQUEST_TIMEOUT_MS = 15000;
export const WEB_TOKEN_KEY = 'access_token';
export const TELEGRAM_WEB_TOKEN_KEY = 'planner_telegram_session';
export const isTelegramSession = () => Boolean(window.Telegram?.WebApp?.initData);
export const hasWebSession = () => Boolean(localStorage.getItem(TELEGRAM_WEB_TOKEN_KEY) || localStorage.getItem(WEB_TOKEN_KEY));

// Bound the entire auth exchange, including reading the response body.
// Login/session/logout must not leave the screen waiting indefinitely.
async function authRequest(path: string, options: RequestInit, connectionError: string) {
  const controller = new AbortController();
  const timeoutError = new Error('Сервис авторизации не ответил за 15 секунд. Повторите попытку.');
  let timer: ReturnType<typeof setTimeout>;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => { controller.abort(); reject(timeoutError); }, REQUEST_TIMEOUT_MS);
  });
  try {
    return await Promise.race([timeout, (async () => {
      let response: Response;
      try { response = await fetch(path, { ...options, credentials: 'same-origin', signal: controller.signal }); }
      catch { throw controller.signal.aborted ? timeoutError : new Error(connectionError); }
      const body = await response.json().catch(() => null);
      return { response, body };
    })()]);
  } finally { clearTimeout(timer!); }
}

export type TelegramLoginPayload = {
  id: number; first_name: string; last_name?: string; username?: string;
  photo_url?: string; auth_date: number; hash: string;
};

export async function telegramLogin(payload: TelegramLoginPayload): Promise<void> {
  const { response, body } = await authRequest('/manager/api/auth/telegram', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload), cache: 'no-store',
  }, 'Нет связи с сервером авторизации.');
  if (!response.ok || typeof body?.access_token !== 'string')
    throw new Error(typeof body?.detail === 'string' ? body.detail : 'Не удалось войти через Telegram.');
  localStorage.setItem(TELEGRAM_WEB_TOKEN_KEY, body.access_token);
  localStorage.removeItem(WEB_TOKEN_KEY);
}

export async function telegramLoginConfig(): Promise<{ bot_username: string; auth_url: string }> {
  const { response, body } = await authRequest('/manager/api/auth/telegram/config', { cache: 'no-store' }, 'Вход через Telegram пока недоступен.');
  if (!response.ok || typeof body?.bot_username !== 'string' || typeof body?.auth_url !== 'string') throw new Error('Вход через Telegram пока недоступен.');
  return body;
}

export async function hasTelegramCookieSession(): Promise<boolean> {
  try {
    const { response, body } = await authRequest('/manager/api/auth/telegram/session', { cache: 'no-store' }, 'Нет связи с сервером авторизации.');
    return response.ok && body?.authenticated === true;
  } catch { return false; }
}

export async function webLogin(email: string, password: string): Promise<void> {
  const { response, body } = await authRequest('/api/v1/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }), cache: 'no-store',
  }, 'Нет связи с сервером авторизации.');
  if (!response.ok || typeof body?.access_token !== 'string')
    throw new Error(typeof body?.detail === 'string' ? body.detail : 'Не удалось войти.');
  localStorage.setItem(WEB_TOKEN_KEY, body.access_token);
  localStorage.removeItem(TELEGRAM_WEB_TOKEN_KEY);
}

export async function webLogout(): Promise<void> {
  await authRequest('/manager/api/auth/logout', { method: 'POST', cache: 'no-store' }, 'Нет связи с сервером авторизации.').catch(() => undefined);
  localStorage.removeItem(WEB_TOKEN_KEY);
  localStorage.removeItem(TELEGRAM_WEB_TOKEN_KEY);
  window.location.assign('/manager/');
}

export const api: Client = async <T,>(path: string, method = 'GET', data?: unknown): Promise<T> => {
  const initData = window.Telegram?.WebApp?.initData;
  const webToken = localStorage.getItem(WEB_TOKEN_KEY);
  const telegramWebToken = localStorage.getItem(TELEGRAM_WEB_TOKEN_KEY);
  // A Telegram redirect session is HttpOnly: it is sent by the browser and
  // deliberately has no JavaScript-readable token. The server decides access.
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (initData) headers.Authorization = `tma ${initData}`;
  else if (telegramWebToken) headers.Authorization = `tgweb ${telegramWebToken}`;
  else if (webToken) headers.Authorization = `Bearer ${webToken}`;
  const controller = new AbortController();
  const timeoutMessage = 'Сервер не ответил за 15 секунд. ' + (method === 'GET' ? 'Повторите загрузку.' : 'Изменение могло сохраниться. Сначала обновите данные перед повторной отправкой.');
  let timer: ReturnType<typeof setTimeout>;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error(timeoutMessage));
    }, REQUEST_TIMEOUT_MS);
  });
  try {
    return await Promise.race([timeout, (async () => {
      let response: Response;
      try { response = await fetch(`/manager/api${path}`, {
      method, headers, credentials: 'same-origin',
      body: data === undefined ? undefined : JSON.stringify(data), cache: 'no-store', signal: controller.signal,
      }); } catch {
        if (controller.signal.aborted) throw new Error(timeoutMessage);
        throw new Error('Нет связи с сервером. Проверьте интернет и повторите загрузку.');
      }
      if (!response.ok) {
        if (response.status === 401 && !initData) { localStorage.removeItem(WEB_TOKEN_KEY); localStorage.removeItem(TELEGRAM_WEB_TOKEN_KEY); }
        const body = await response.json().catch(() => null);
        throw new Error(typeof body?.detail === 'string' ? body.detail : `Не удалось загрузить данные (HTTP ${response.status}). Повторите загрузку.`);
      }
      let value: unknown;
      try { value = await response.json(); }
      catch { throw new Error('Сервер вернул некорректный JSON. Повторите загрузку.'); }
      validateResponse(path, method, value);
      return value as T;
    })()]);
  } finally { clearTimeout(timer!); }
};
