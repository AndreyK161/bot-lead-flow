const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { test } = require('node:test');
const { chromium } = require('playwright');

for (const role of ['manager', 'supervisor']) {
  test(`${role}: server day advances after midnight while historical dates and forms are preserved`, async () => {
    const root = path.resolve(__dirname, '../../static');
    const server = http.createServer((req, res) => {
      let file = path.join(root, req.url.replace(/^\/manager\//, ''));
      try {
        if (fs.statSync(file).isDirectory()) file = path.join(file, 'index.html');
        res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
        res.end(fs.readFileSync(file));
      } catch { res.statusCode = 404; res.end(); }
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const base = `http://127.0.0.1:${server.address().port}`;
    let browser;
    try {
      browser = await chromium.launch({ headless: true,
        ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } : {}),
      });
      const page = await browser.newPage({ timezoneId: 'Pacific/Honolulu' });
      await page.clock.install({ time: new Date('2026-11-02T20:59:00Z') });
      let serverDay = '2026-11-02';
      let failDate = false;
      const user = { id: role === 'manager' ? 1 : 9, name: 'Test user', role, active: 1, telegram_id: 100, bitrix_id: role === 'manager' ? 110 : null };
      const employee = { id: 1, name: 'Manager', role: 'manager', active: 1, telegram_id: 100, bitrix_id: 110 };
      const plans = ['2026-11-01', '2026-11-02', '2026-11-03', '2026-11-04'].map((day, index) => ({
        id: index + 1, employee_id: 1, employee_name: 'Manager', day, status: 'draft',
        summary: { total: 0, done: 0, moved: 0, planned_minutes: 0, actual_seconds: 0 },
        tasks: [], initial_snapshot: null, server_time: 1793653200,
      }));
      await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.origin !== base) return route.abort();
        if (!url.pathname.includes('/api/')) return route.continue();
        let data = [];
        if (url.pathname.endsWith('/session')) data = { authenticated: true };
        else if (url.pathname.endsWith('/me')) {
          if (failDate) return route.fulfill({ status: 503, json: { detail: 'Temporary failure' } });
          data = { user, today: serverDay, timezone: 'Europe/Moscow', crm: { connected: false, message: 'mock' } };
        } else if (url.pathname.endsWith('/employees')) data = [employee];
        else if (url.pathname.endsWith('/plans')) data = plans;
        else if (/\/plans\/\d+$/.test(url.pathname)) data = plans.find(p => p.id === Number(url.pathname.split('/').pop()));
        else if (url.pathname.endsWith('/summary')) data = { available: false, day: serverDay, message: 'mock' };
        return route.fulfill({ json: data });
      });
      await page.goto(`${base}/manager/`);
      const expectTopDay = async day => page.waitForFunction(value => document.querySelector('.top-date')?.textContent.includes(value), day, { timeout: 5000 });
      await expectTopDay('2 ноября');
      // The browser is still on November 2 in Honolulu. Only the server decides today.
      serverDay = '2026-11-03';
      failDate = true;
      await page.clock.fastForward(30001);
      assert.match(await page.locator('.top-date').innerText(), /2 ноября/);
      failDate = false;
      await page.clock.fastForward(30001);
      await expectTopDay('3 ноября');
      const nav = page.getByRole('navigation');
      await nav.getByRole('button', { name: role === 'manager' ? 'Мой план' : 'Планы', exact: true }).click();
      const input = page.getByLabel('Дата плана', { exact: true });
      assert.equal(await input.inputValue(), '2026-11-03');
      await input.fill('2026-11-01');
      serverDay = '2026-11-04';
      await page.evaluate(role => role === 'manager' ? window.dispatchEvent(new Event('focus')) : document.dispatchEvent(new Event('visibilitychange')), role);
      await expectTopDay('4 ноября');
      assert.equal(await input.inputValue(), '2026-11-01', 'historical selection must remain');
      await nav.getByRole('button', { name: role === 'manager' ? 'Главная' : 'Обзор отдела', exact: true }).click();
      await nav.getByRole('button', { name: role === 'manager' ? 'Мой план' : 'Планы', exact: true }).click();
      assert.equal(await input.inputValue(), '2026-11-04', 'home must use the latest server day');
      if (role === 'supervisor') {
        await nav.getByRole('button', { name: 'Сотрудники', exact: true }).click();
        await page.getByRole('button', { name: 'Добавить сотрудника', exact: true }).click();
        const form = page.getByRole('dialog');
        await form.locator('input[name="name"]').fill('Unsaved employee');
        serverDay = '2026-11-05';
        await page.clock.fastForward(30001);
        await page.evaluate(() => window.dispatchEvent(new Event('focus')));
        assert.equal(await form.locator('input[name="name"]').inputValue(), 'Unsaved employee');
        assert.match(await page.locator('.top-date').innerText(), /4 ноября/);
      }
    } finally {
      if (browser) await browser.close();
      await new Promise(resolve => server.close(resolve));
    }
  });
}
