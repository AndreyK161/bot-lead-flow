const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { test } = require('node:test');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '../../static');

test('profile presets use the Moscow calendar date in every browser timezone', async () => {
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
    for (const timezoneId of ['Europe/Moscow', 'UTC', 'Pacific/Honolulu', 'Pacific/Kiritimati']) {
      const page = await browser.newPage({ timezoneId }); page.setDefaultTimeout(5000);
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.origin !== base) return route.abort();
        if (!url.pathname.includes('/api/')) return route.continue();
        let data = [];
        if (url.pathname.endsWith('/session')) data = { authenticated: true };
        else if (url.pathname.endsWith('/me')) data = {
          user: { id: 1, name: 'Manager', role: 'manager', active: 1, telegram_id: 101, bitrix_id: 110 },
          today: '2026-11-02', timezone: 'Europe/Moscow', crm: { connected: false, message: 'mock' },
        };
        else if (url.pathname.endsWith('/summary')) data = { available: false, day: '2026-11-02', message: 'mock' };
        else if (url.pathname.endsWith('/performance')) data = { available: true,
          period: { start: url.searchParams.get('start'), end: url.searchParams.get('end') }, leads: { processed: 0, stages: {} } };
        return route.fulfill({ json: data });
      });
      await page.goto(`${base}/manager/`);
      await page.getByRole('navigation').getByRole('button', { name: 'Мой профиль' }).click();
      const fields = page.locator('.period-fields input');
      await fields.first().waitFor();
      for (const [preset, start] of [['Этот месяц', '2026-11-01'], ['7 дней', '2026-10-27'], ['Сегодня', '2026-11-02']]) {
        await page.getByRole('button', { name: preset, exact: true }).click();
        assert.deepEqual(await fields.evaluateAll(inputs => inputs.map(input => input.value)), [start, '2026-11-02'], `${timezoneId}: ${preset}`);
      }
      await page.close();
    }
  } finally { if (browser) await browser.close(); await new Promise(resolve => server.close(resolve)); }
});
