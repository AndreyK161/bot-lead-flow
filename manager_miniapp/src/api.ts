import type { Client } from './types';
import { validateResponse } from './response';

export const REQUEST_TIMEOUT_MS = 15000;

export const api: Client = async <T,>(path: string, method = 'GET', data?: unknown): Promise<T> => {
  const initData = window.Telegram?.WebApp?.initData;
  if (!initData) throw new Error('Откройте «План дня» в личном чате с Telegram-ботом.');
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
      method, headers: { Authorization: `tma ${initData}`, 'Content-Type': 'application/json' },
      body: data === undefined ? undefined : JSON.stringify(data), cache: 'no-store', signal: controller.signal,
      }); } catch {
        if (controller.signal.aborted) throw new Error(timeoutMessage);
        throw new Error('Нет связи с сервером. Проверьте интернет и повторите загрузку.');
      }
      if (!response.ok) {
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
