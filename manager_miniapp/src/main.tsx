import { createRoot } from 'react-dom/client';
import App from './App';
import './style.css';

const webapp = window.Telegram?.WebApp;
function applyTheme() { document.documentElement.dataset.theme = webapp?.colorScheme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'); }
applyTheme();
webapp?.ready(); webapp?.expand(); webapp?.onEvent('themeChanged', applyTheme);
createRoot(document.getElementById('root')!).render(<App />);
