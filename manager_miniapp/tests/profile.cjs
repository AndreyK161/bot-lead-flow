const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { test } = require('node:test');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '../../app/static/manager');
const day = '2026-10-30';

test('profile ignores outdated results, errors and loading completion', async () => {
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
    const page = await browser.newPage();
    page.setDefaultTimeout(5000);
    const pending = [];
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== base) return route.abort();
      if (!url.pathname.includes('/api/')) return route.continue();
      let data;
      if (url.pathname.endsWith('/session')) data = { authenticated: true };
      else if (url.pathname.endsWith('/me')) data = {
        user: { id: 1, name: 'Manager', role: 'manager', active: 1, telegram_id: 101, bitrix_id: 110 },
        today: day, timezone: 'Europe/Moscow', crm: { connected: false, message: 'mock' },
      };
      else if (url.pathname.endsWith('/summary')) data = { available: false, day, message: 'mock' };
      else if (url.pathname.endsWith('/performance')) {
        pending.push({ route, start: url.searchParams.get('start'), end: url.searchParams.get('end') });
        return;
      } else data = [];
      await route.fulfill({ json: data });
    });
    async function takeRequest() {
      const deadline = Date.now() + 5000;
      while (!pending.length) {
        assert(Date.now() < deadline, 'expected a performance request');
        await new Promise(resolve => setTimeout(resolve, 10));
      }
      return pending.shift();
    }
    async function answer(request, processed, status = 200) {
      const response = page.waitForResponse(r => r.url() === request.route.request().url());
      await request.route.fulfill({ status, json: status === 200 ? {
        available: true, period: { start: request.start, end: request.end }, leads: { processed, stages: {} },
      } : { detail: 'Outdated request failed' } });
      await response;
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    }
    const metric = page.locator('.profile-stats article strong').first();
    await page.goto(`${base}/manager/`);
    await page.getByRole('navigation').getByRole('button', { name: 'Мой профиль' }).click();
    await answer(await takeRequest(), 30);
    const fields = page.locator('.period-fields input');

    await fields.nth(0).fill('2026-10-29'); const oldResult = await takeRequest();
    await fields.nth(1).fill('2026-10-31'); const newResult = await takeRequest();
    await answer(newResult, 31);
    await answer(oldResult, 29);
    assert.equal(await metric.textContent(), '31', 'old success must not replace latest data');
    assert.match(await page.locator('.stage-summary .section-heading .muted').textContent(), /31/);

    await fields.nth(0).fill('2026-10-28'); const oldError = await takeRequest();
    await fields.nth(1).fill('2026-11-01'); const newSuccess = await takeRequest();
    await answer(newSuccess, 99);
    await answer(oldError, 0, 503);
    assert.equal(await metric.textContent(), '99');
    assert.equal(await page.getByRole('alert').count(), 0, 'old error must not appear for latest period');

    await fields.nth(0).fill('2026-10-27'); const oldLoading = await takeRequest();
    await fields.nth(1).fill('2026-11-02'); const newLoading = await takeRequest();
    await answer(oldLoading, 27);
    assert.equal(await page.getByRole('button', { name: 'Показать', exact: true }).isDisabled(), true,
      'old completion must not stop latest loading');
    assert.equal(await metric.count(), 0);
    await answer(newLoading, 102);
    assert.equal(await metric.textContent(), '102');

    await fields.nth(0).fill('2026-10-26'); const beforeInvalid = await takeRequest();
    await fields.nth(0).fill('2026-11-03');
    await page.getByRole('alert').filter({ hasText: 'Дата начала не может быть позже' }).waitFor();
    await answer(beforeInvalid, 26);
    assert.equal(await metric.count(), 0, 'invalid period must not show old data');
    assert.equal(await page.getByRole('button', { name: 'Показать', exact: true }).isEnabled(), true);
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
});
