const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { test } = require('node:test');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '../../static');
const day = '2026-11-02';
const employees = [1, 2].map(id => ({ id, name: `Manager ${id}`, role: 'manager', active: 1, telegram_id: 100 + id, bitrix_id: 110 + id }));
const plans = [
  { id: 1, employee_id: 1, employee_name: 'Manager 1', day, status: 'draft' },
  { id: 2, employee_id: 1, employee_name: 'Manager 1', day: '2026-11-01', status: 'closed' },
  { id: 3, employee_id: 2, employee_name: 'Manager 2', day, status: 'approved' },
].map(p => ({ ...p, summary: { total: 4, done: 1, moved: 1, planned_minutes: 90, actual_seconds: 600 } }));
function detail(plan) {
  return { ...plan, server_time: Date.now() / 1000, initial_snapshot: null,
    tasks: ['todo', 'in_progress', 'done', 'moved'].map((status, i) => ({
      id: plan.id * 10 + i, plan_id: plan.id, employee_id: plan.employee_id, title: `Task ${i + 1}`,
      description: 'Task details', kind: 'quantitative', planned_minutes: 30, target: 10,
      actual_value: i, actual_seconds: 120, timer_started: null, status, result: 'Result', comment: 'Comment',
      locked: 0, revision: 1, external_url: null, moved_to: status === 'moved' ? 99 : null,
    })),
  };
}
const requests = ['pending', 'approved', 'rejected', 'cancelled'].map((status, i) => ({
  id: i + 1, task_id: 10, employee_name: 'Manager 1', title: 'Task 1', day, field: 'target',
  old_value: 10, proposed: 20, reason: 'Change request', status, decision_comment: 'Decision', created_at: 1, decided_at: i ? 2 : null,
}));
const item = { id: 1, session_id: 'queue', position: 1, entity_type: 'lead', entity_id: '77', client_name: 'Test client', phone: '79990000000',
  stage_id: 'NEW', stage_name: 'New lead', source_name: '', bitrix_url: 'https://example.invalid/crm/lead/details/77/',
  status: 'awaiting_confirmation', attempts_count: 1, last_result: null, last_call_id: 'test-call' };
const dialer = { session: { id: 'queue', telegram_id: 101, bitrix_user_id: '111', manager_name: 'Manager 1', entity_type: 'lead',
  date_from: day, date_to: day, category_id: null, stage_ids_json: '[]', status: 'paused', auto_advance: 1, created_at: '', updated_at: '' },
  summary: { total: 4, completed: 2, answered: 1, failed: 1, remaining: 2 }, current: item,
  items: ['awaiting_confirmation', 'answered', 'error', 'pending'].map((status, i) => ({ ...item, id: i + 1, position: i + 1, status })) };

// Uses computed styles, alpha compositing and both gradient endpoints.
// This is a regression check for these screens, not a full accessibility audit.
async function contrastFailures(page) {
  return page.evaluate(() => {
    const rgb = value => (value.match(/[\d.]+/g) || []).map(Number);
    const mix = (top, base) => top.slice(0, 3).map((v, i) => v * (top[3] ?? 1) + base[i] * (1 - (top[3] ?? 1)));
    const lum = c => c.slice(0, 3).map(v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; })
      .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
    const ratio = (a, b) => (Math.max(lum(a), lum(b)) + .05) / (Math.min(lum(a), lum(b)) + .05);
    const scope = document.querySelector('dialog[open]') || document.body;
    const failures = [];
    for (const el of scope.querySelectorAll('*')) {
      if (!Array.from(el.childNodes).some(n => n.nodeType === Node.TEXT_NODE && n.textContent.trim())) continue;
      if (el.closest('script,style,.brand-mark,button:disabled')) continue;
      const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
      if (!rect.width || !rect.height || style.visibility === 'hidden' || style.opacity === '0') continue;
      const chain = []; for (let p = el; p; p = p.parentElement) chain.unshift(p);
      let backgrounds = [[255, 255, 255]];
      for (const ancestor of chain) {
        const css = getComputedStyle(ancestor), color = rgb(css.backgroundColor);
        if (color.length >= 3) backgrounds = backgrounds.map(bg => mix(color, bg));
        if (css.backgroundImage !== 'none') {
          const stops = css.backgroundImage.match(/rgba?\([^)]+\)/g) || [];
          if (stops.length) backgrounds = backgrounds.flatMap(bg => stops.map(stop => mix(rgb(stop), bg)));
        }
      }
      const fg = rgb(style.color), actual = Math.min(...backgrounds.map(bg => ratio(mix(fg, bg), bg)));
      const large = parseFloat(style.fontSize) >= 24 || (parseFloat(style.fontSize) >= 18.66 && Number(style.fontWeight) >= 700);
      const minimum = large ? 3 : 4.5;
      if (actual + .02 < minimum) failures.push({ text: el.textContent.trim().slice(0, 65), selector: el.tagName + '.' + el.className,
        ratio: Number(actual.toFixed(2)), minimum, color: style.color });
    }
    return failures;
  });
}

test('readable screens and dialogs in both themes and roles', async () => {
  const server = http.createServer((req, res) => {
    let file = path.join(root, req.url.replace(/^\/manager\//, ''));
    try { if (fs.statSync(file).isDirectory()) file = path.join(file, 'index.html');
      res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
      res.end(fs.readFileSync(file));
    } catch { res.statusCode = 404; res.end(); }
  });
  let browser;
  await new Promise(r => server.listen(0, '127.0.0.1', r)); const base = `http://127.0.0.1:${server.address().port}`;
  const failures = [];
  try {
    browser = await chromium.launch({ headless: true,
      ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } : {}),
    });
    for (const width of [1440, 390]) for (const theme of ['light', 'dark']) for (const role of ['supervisor', 'manager', 'anonymous']) {
      const page = await browser.newPage({ viewport: { width, height: 1100 }, colorScheme: theme }); page.setDefaultTimeout(5000);
      await page.route('**/*', route => {
        const url = new URL(route.request().url()); if (url.origin !== base) return route.abort();
        if (!url.pathname.includes('/api/')) return route.continue();
        let data = [];
        if (url.pathname.endsWith('/session')) return route.fulfill({ status: role === 'anonymous' ? 401 : 200, json: { authenticated: role !== 'anonymous' } });
        if (url.pathname.endsWith('/me')) data = { user: role === 'manager' ? employees[0] : { ...employees[0], id: 9, role, name: 'Supervisor' }, today: day, timezone: 'Europe/Moscow', crm: { connected: true, message: 'mock' } };
        else if (url.pathname.endsWith('/employees')) data = employees;
        else if (url.pathname.endsWith('/plans')) data = plans.filter(p => !url.searchParams.has('employee_id') || p.employee_id === Number(url.searchParams.get('employee_id')));
        else if (/\/plans\/\d+(\/report)?$/.test(url.pathname)) data = detail(plans.find(p => p.id === Number(url.pathname.match(/\/plans\/(\d+)/)[1])));
        else if (url.pathname.endsWith('/requests')) data = requests;
        else if (url.pathname.endsWith('/summary')) data = { available: false, day, message: 'CRM unavailable' };
        else if (url.pathname.endsWith('/performance')) data = { available: true, period: { start: url.searchParams.get('start'), end: url.searchParams.get('end') }, leads: { processed: 5, stages: { Processed: 5 } } };
        else if (url.pathname.endsWith('/dialer/session')) data = dialer;
        else if (url.pathname.endsWith('/dialer/filters')) data = { lead: [{ id: 'NEW', name: 'New' }], deal: { '0': [], '2': [] } };
        else if (url.pathname.endsWith('/telegram/config')) return route.fulfill({ status: 503, json: { detail: 'Widget not configured' } });
        return route.fulfill({ json: data });
      });
      const check = async label => {
        failures.push(...(await contrastFailures(page)).map(f => ({ theme, role, width, screen: label, ...f })));
        const overflow = await page.evaluate(() => ({ width: innerWidth, page: document.documentElement.scrollWidth,
          offenders: Array.from(document.querySelectorAll('body *')).filter(el => el.getBoundingClientRect().right > innerWidth + 1)
            .slice(0, 14).map(el => ({ tag: el.tagName, class: el.className, right: el.getBoundingClientRect().right })) }));
        assert(overflow.page <= overflow.width + 1, `${role}/${theme}/${width}/${label}: page overflows horizontally: ${JSON.stringify(overflow)}`);
      };
      await page.goto(`${base}/manager/`);
      if (role === 'anonymous') { await page.locator('input[type=email]').waitFor(); await check('login'); }
      else {
        await page.locator('.stats-grid').waitFor();
        const navigation = page.getByRole('navigation').getByRole('button');
        for (let index = 0; index < await navigation.count(); index++) {
          const label = await navigation.nth(index).textContent();
          await navigation.nth(index).click();
          await page.waitForFunction(() => !document.querySelector('.page-content .spinner'));
          await check(label);
          await navigation.nth(index).hover(); await check(label + ' hover');
          await page.locator('.page-title').hover();
          if (label === 'Сотрудники') {
            await page.getByRole('button', { name: 'Добавить сотрудника' }).click(); await page.locator('dialog[open]').waitFor(); await check('employee dialog');
            await page.getByRole('button', { name: 'Отмена', exact: true }).click();
          }
          if (role === 'manager' && label === 'Мой план') {
            await page.getByRole('button', { name: 'Новая задача', exact: true }).click(); await page.locator('dialog[open]').waitFor(); await check('task dialog');
            await page.getByRole('button', { name: 'Отмена', exact: true }).click();
            await page.getByRole('button', { name: 'В работе', exact: true }).click(); await check('selected filter');
          }
        }
      }
      await page.close();
    }
    const unique = [...new Map(failures.map(f => [JSON.stringify([f.theme, f.width, f.selector, f.text, f.ratio]), f])).values()];
    assert.equal(unique.length, 0, JSON.stringify(unique, null, 2));
  } finally { if (browser) await browser.close(); await new Promise(r => server.close(r)); }
});

test('an outdated dialer poll cannot erase a newly created queue', async () => {
  const server = http.createServer((req, res) => {
    let file = path.join(root, req.url.replace(/^\/manager\//, ''));
    try { if (fs.statSync(file).isDirectory()) file = path.join(file, 'index.html');
      res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
      res.end(fs.readFileSync(file));
    } catch { res.statusCode = 404; res.end(); }
  });
  let browser;
  await new Promise(r => server.listen(0, '127.0.0.1', r)); const base = `http://127.0.0.1:${server.address().port}`;
  try {
    browser = await chromium.launch({ headless: true,
      ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } : {}),
    });
    const page = await browser.newPage(); page.setDefaultTimeout(7000);
    let polls = 0, stalePoll, created = false;
    await page.route('**/*', route => {
      const url = new URL(route.request().url()); if (url.origin !== base) return route.abort();
      if (!url.pathname.includes('/api/')) return route.continue();
      let data = [];
      if (url.pathname.endsWith('/session') && !url.pathname.endsWith('/dialer/session')) data = { authenticated: true };
      else if (url.pathname.endsWith('/me')) data = { user: employees[0], today: day, timezone: 'Europe/Moscow', crm: { connected: false, message: 'mock' } };
      else if (url.pathname.endsWith('/plans')) data = [];
      else if (url.pathname.endsWith('/summary')) data = { available: false, day, message: 'mock' };
      else if (url.pathname.endsWith('/dialer/filters')) data = { lead: [], deal: { '0': [], '2': [] } };
      else if (url.pathname.endsWith('/dialer/session')) {
        if (++polls === 3) { stalePoll = route; return; }
        data = created ? dialer : null;
      } else if (url.pathname.endsWith('/dialer/sessions')) { created = true; data = dialer; }
      return route.fulfill({ json: data });
    });
    await page.goto(`${base}/manager/`);
    await page.getByRole('navigation').getByRole('button', { name: 'Обзвон', exact: true }).click();
    const deadline = Date.now() + 7000;
    while (!stalePoll) { assert(Date.now() < deadline, 'background poll should start'); await new Promise(r => setTimeout(r, 20)); }
    await page.getByRole('button', { name: 'Загрузить очередь', exact: true }).click();
    await page.locator('.dialer-current').waitFor();
    const response = page.waitForResponse(r => r.url().endsWith('/dialer/session'));
    await stalePoll.fulfill({ json: null }); await response;
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
    assert.equal(await page.locator('.dialer-current').count(), 1, 'new queue must survive the stale poll');
  } finally { if (browser) await browser.close(); await new Promise(r => server.close(r)); }
});
