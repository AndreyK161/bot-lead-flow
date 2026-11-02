const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { test } = require('node:test');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '../../static');
const day = '2026-10-30';
const staff = [1, 2].map(id => ({ id, name: `Manager ${id}`, role: 'manager', active: 1, telegram_id: 100 + id, bitrix_id: id }));
const plans = staff.map(e => ({
  id: e.id, employee_id: e.id, employee_name: e.name, day, status: 'draft',
  summary: { total: e.id, done: 0, moved: 0, planned_minutes: e.id * 30, actual_seconds: 0 },
}));

test('department overview resets employee filter through menu and logo', async () => {
  const server = http.createServer((req, res) => {
    const relative = req.url.replace(/^\/manager\//, '');
    let file = path.join(root, relative);
    try {
      if (fs.statSync(file).isDirectory()) file = path.join(file, 'index.html');
      res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
      res.end(fs.readFileSync(file));
    } catch { res.statusCode = 404; res.end(); }
  });
  let browser;
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    browser = await chromium.launch({ headless: true,
      ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } : {}),
    });
    const page = await browser.newPage();
    let failOverview = false;
    const requests = [];
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== base) return route.abort();
      if (!url.pathname.includes('/api/')) return route.continue();
      let data;
      if (url.pathname.endsWith('/session')) data = { authenticated: true };
      else if (url.pathname.endsWith('/me')) data = {
        user: { id: 9, name: 'Supervisor', role: 'supervisor', active: 1, telegram_id: 999, bitrix_id: null },
        today: day, timezone: 'Europe/Moscow', crm: { message: 'mock', connected: false },
      };
      else if (url.pathname.endsWith('/employees')) data = staff;
      else if (url.pathname.endsWith('/plans')) {
        requests.push(url.searchParams.get('employee_id'));
        if (failOverview && !url.searchParams.has('employee_id'))
          return route.fulfill({ status: 503, json: { detail: 'Synthetic plans failure' } });
        data = plans.filter(p => !url.searchParams.has('employee_id') || p.employee_id === Number(url.searchParams.get('employee_id')));
      } else if (/\/plans\/\d+$/.test(url.pathname)) data = {
        ...plans.find(p => p.id === Number(url.pathname.split('/').pop())), tasks: [], server_time: Date.now() / 1000,
      };
      else if (url.pathname.endsWith('/summary')) data = { available: false, day, message: 'mock' };
      else data = [];
      return route.fulfill({ json: data });
    });
    await page.goto(`${base}/manager/`);
    const cards = page.locator('.team-overview .employee-card');
    const expectTeam = async () => {
      await page.waitForFunction(() => document.querySelectorAll('.team-overview .employee-card').length === 2);
      assert.equal(await cards.count(), 2);
      assert.match(await page.locator('.stat-value').nth(1).innerText(), /0\s*\/\s*3/);
      assert.match(await page.locator('.stat-value').nth(2).innerText(), /90/);
      assert.equal(requests.at(-1), null, 'overview must request all employees');
    };
    await expectTeam();
    for (const entry of ['menu', 'logo']) {
      await cards.first().getByRole('button', { name: 'Открыть план' }).click();
      await page.waitForFunction(() => document.querySelector('select[aria-label="Сотрудник"]')?.value === '1');
      await page.getByRole('heading', { name: 'Фокус на сегодня' }).waitFor();
      assert.equal(requests.at(-1), '1', 'employee plan must retain its own filter');
      if (entry === 'menu') await page.getByRole('navigation').getByRole('button', { name: 'Обзор отдела' }).click();
      else await page.locator('button.brand').click();
      await expectTeam();
    }
    await cards.first().getByRole('button', { name: 'Открыть план' }).click();
    await page.getByRole('heading', { name: 'Фокус на сегодня' }).waitFor();
    failOverview = true;
    await page.getByRole('navigation').getByRole('button', { name: 'Обзор отдела' }).click();
    await page.getByText('Synthetic plans failure', { exact: false }).waitFor();
    assert.equal(await cards.count(), 0, 'failed overview must not show the employee as the department');
    assert.equal(await page.locator('.stats-grid').count(), 0);
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
});

test('manager home returns to today after opening a historical plan', async () => {
  const server = http.createServer((req, res) => {
    let file = path.join(root, req.url.replace(/^\/manager\//, ''));
    try {
      if (fs.statSync(file).isDirectory()) file = path.join(file, 'index.html');
      res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
      res.end(fs.readFileSync(file));
    } catch { res.statusCode = 404; res.end(); }
  });
  let browser;
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    browser = await chromium.launch({ headless: true,
      ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } : {}),
    });
    for (const hasToday of [true, false]) {
      const page = await browser.newPage();
      page.setDefaultTimeout(5000);
      const employee = staff[0];
      const dailyPlans = [
        ...(hasToday ? [{ ...plans[0] }] : []),
        { ...plans[0], id: 10, day: '2026-10-29', summary: { ...plans[0].summary, total: 9, planned_minutes: 90 } },
      ];
      const dailyTask = id => ({
        id: id * 100, plan_id: id, employee_id: employee.id,
        title: id === 10 ? 'Yesterday task' : 'Today task', description: '', kind: 'general',
        planned_minutes: 30, target: null, actual_value: 0, actual_seconds: 0,
        timer_started: null, status: 'in_progress', result: '', comment: '', locked: 0, revision: 1,
      });
      const requestedEmployees = [];
      await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.origin !== base) return route.abort();
        if (!url.pathname.includes('/api/')) return route.continue();
        let data = [];
        if (url.pathname.endsWith('/session')) data = { authenticated: true };
        else if (url.pathname.endsWith('/me')) data = {
          user: employee, today: day, timezone: 'Europe/Moscow', crm: { message: 'mock', connected: false },
        };
        else if (url.pathname.endsWith('/plans')) {
          requestedEmployees.push(url.searchParams.get('employee_id'));
          data = dailyPlans;
        } else if (/\/plans\/\d+$/.test(url.pathname)) {
          const id = Number(url.pathname.split('/').pop());
          data = { ...dailyPlans.find(p => p.id === id), tasks: [dailyTask(id)], server_time: Date.now() / 1000 };
        } else if (url.pathname.endsWith('/summary')) data = { available: false, day, message: 'mock' };
        return route.fulfill({ json: data });
      });
      try {
        await page.goto(`${base}/manager/`);
        await page.locator('.stats-grid').waitFor();
        for (const entry of ['menu', 'logo']) {
          await page.getByRole('navigation').getByRole('button', { name: 'История', exact: true }).click();
          await page.locator('.plan-row').filter({ hasText: '29 октября' }).click();
          await page.getByRole('heading', { name: 'Фокус на сегодня' }).waitFor();
          if (entry === 'menu') await page.getByRole('navigation').getByRole('button', { name: 'Главная', exact: true }).click();
          else await page.locator('button.brand').click();
          await page.locator('.stats-grid').waitFor();
          assert.equal((await page.locator('.stat-value').first().textContent()).trim(), hasToday ? '1' : '0',
            `home must use today's metrics (${entry}, hasToday=${hasToday})`);
          if (hasToday) assert.equal(await page.locator('.current-card h3').textContent(), 'Today task');
          else {
            assert.equal(await page.locator('.current-card h3').count(), 0);
            assert.match(await page.locator('.current-card').textContent(), /Создайте план на сегодня/);
          }
          assert(requestedEmployees.every(id => id === String(employee.id)), 'manager scope must stay unchanged');
        }
      } finally { await page.close(); }
    }
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
});
