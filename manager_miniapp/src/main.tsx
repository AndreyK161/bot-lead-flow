import { useEffect, useRef, useState, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { hasWebSession, isTelegramSession, telegramLogin, telegramLoginConfig, webLogin, type TelegramLoginPayload } from './api';
import './style.css';

const webapp = window.Telegram?.WebApp;
function applyTheme() { document.documentElement.dataset.theme = webapp?.colorScheme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); }
applyTheme();
webapp?.ready(); webapp?.expand(); webapp?.onEvent('themeChanged', applyTheme);

function WebLogin() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const telegramMount = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let active = true;
    telegramLoginConfig().then(({ bot_username }) => {
      if (!active || !telegramMount.current) return;
      window.onTelegramAuth = async (user: TelegramLoginPayload) => {
        setBusy(true); setError('');
        try { await telegramLogin(user); window.location.reload(); }
        catch (reason) { setError((reason as Error).message); setBusy(false); }
      };
      const script = document.createElement('script');
      script.src = 'https://telegram.org/js/telegram-widget.js?22';
      script.async = true;
      script.setAttribute('data-telegram-login', bot_username);
      script.setAttribute('data-size', 'large');
      script.setAttribute('data-radius', '8');
      script.setAttribute('data-request-access', 'write');
      script.setAttribute('data-onauth', 'onTelegramAuth(user)');
      telegramMount.current.replaceChildren(script);
    }).catch(() => undefined);
    return () => { active = false; delete window.onTelegramAuth; };
  }, []);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError('');
    try { await webLogin(email, password); window.location.reload(); }
    catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  };
  return <main className="web-login"><section className="web-login-brand"><div className="brand-mark">П</div><p className="eyebrow">LEAD FLOW · ПЛАН ДНЯ</p><h1>Рабочее пространство отдела продаж</h1><p>Планы, задачи, показатели и обзвон клиентов — в едином интерфейсе.</p></section><section className="web-login-panel"><div className="web-login-box"><p className="eyebrow">ДОБРО ПОЖАЛОВАТЬ</p><h2>Вход в систему</h2><p className="muted">Быстрый вход через рабочий Telegram</p><div className="telegram-login" ref={telegramMount}>{busy && <span>Подтверждаем вход…</span>}</div><div className="login-divider"><span>или по рабочей учётной записи</span></div><form onSubmit={submit}><label>Email<input type="email" autoComplete="username" value={email} onChange={event => setEmail(event.target.value)} required/></label><label>Пароль<input type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} required/></label>{error && <p className="error" role="alert">{error}</p>}<button disabled={busy}>{busy ? 'Входим…' : 'Войти →'}</button></form></div></section></main>;
}

declare global { interface Window { onTelegramAuth?: (user: TelegramLoginPayload) => void } }

createRoot(document.getElementById('root')!).render(
  isTelegramSession() || hasWebSession() ? <App /> : <WebLogin />,
);
