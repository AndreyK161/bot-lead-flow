import { useState, type FormEvent } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { hasWebSession, isTelegramSession, webLogin } from './api';
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
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError('');
    try { await webLogin(email, password); window.location.reload(); }
    catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  };
  return <main className="web-login"><section className="web-login-brand"><div className="brand-mark">П</div><p className="eyebrow">LEAD FLOW · ПЛАН ДНЯ</p><h1>Рабочее пространство отдела продаж</h1><p>Планы, задачи, показатели и обзвон клиентов — в едином интерфейсе.</p></section><section className="web-login-panel"><form onSubmit={submit}><p className="eyebrow">ДОБРО ПОЖАЛОВАТЬ</p><h2>Вход в систему</h2><p className="muted">Используйте рабочую учётную запись Lead Flow</p><label>Email<input type="email" autoComplete="username" value={email} onChange={event => setEmail(event.target.value)} required autoFocus/></label><label>Пароль<input type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} required/></label>{error && <p className="error" role="alert">{error}</p>}<button disabled={busy}>{busy ? 'Входим…' : 'Войти →'}</button></form></section></main>;
}

createRoot(document.getElementById('root')!).render(
  isTelegramSession() || hasWebSession() ? <App /> : <WebLogin />,
);
