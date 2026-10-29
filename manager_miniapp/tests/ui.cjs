const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'src', 'App.tsx'), 'utf8');
const response = fs.readFileSync(path.join(root, 'src', 'response.ts'), 'utf8');
const api = fs.readFileSync(path.join(root, 'src', 'api.ts'), 'utf8');
const main = fs.readFileSync(path.join(root, 'src', 'main.tsx'), 'utf8');
const builtIndex = fs.readFileSync(path.join(root, '..', 'app', 'static', 'manager', 'index.html'), 'utf8');

assert(source.includes('/crm/summary?employee_id='), 'CRM summary is not requested by the UI');
assert(source.includes('/crm/performance?'), 'self performance profile is not requested by the UI');
assert(source.includes("['home', 'Главная'], ['profile', 'Мой профиль']"), 'manager profile navigation is missing');
assert(source.includes('Этот месяц'), 'profile period presets are missing');
assert(source.includes('Только чтение'), 'read-only CRM label is missing');
assert(source.includes('Обработано лидов'), 'processed leads metric is missing');
assert(source.includes('Общая длительность'), 'total call duration metric is missing');
assert(source.includes("['calls', 'Обзвон']"), 'manager dialer navigation is missing');
assert(source.includes('/dialer/sessions'), 'dialer queue actions are missing');
assert(source.includes('Клиент ответил'), 'dialer confirmation action is missing');
assert(response.includes("route === '/dialer/session'"), 'dialer response is not validated');
assert(api.includes("WEB_TOKEN_KEY = 'access_token'"), 'shared Lead Flow web token is missing');
assert(api.includes("'/api/v1/auth/login'"), 'web login does not use Lead Flow auth');
assert(main.includes('Рабочее пространство отдела продаж'), 'desktop web login is missing');
assert(source.includes('Обзор отдела'), 'desktop supervisor dashboard is missing');
assert(source.includes('className="sidebar"'), 'desktop sidebar shell is missing');
assert(response.includes("route === '/crm/summary'"), 'CRM response is not validated');
assert(response.includes("route === '/crm/performance'"), 'performance response is not validated');
assert(response.includes('optional(v.leads,leads)'), 'processed leads response is not validated');
const asset = builtIndex.match(/src="(\/manager\/[^"]+\.js)"/);
assert(asset, 'built JavaScript asset is missing');
const relativeAsset = asset[1].replace(/^\/manager\//, '');
assert(fs.existsSync(path.join(root, '..', 'app', 'static', 'manager', relativeAsset)), 'referenced JavaScript asset does not exist');

console.log('Manager Mini App UI smoke checks passed');
