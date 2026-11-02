const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { test } = require('node:test');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '../../static');

test('denied web sessions can return to login and sign in as another account', async () => {
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
    for (const [transport, deniedStatus] of ['bearer', 'telegram-token', 'cookie'].flatMap(transport => [401, 403].map(status => [transport, status]))) {
      const context = await browser.newContext();
      try {
        const page = await context.newPage();
        page.setDefaultTimeout(5000);
        if (transport !== 'cookie') {
          // Seed once; a reload after logout must not restore the rejected token.
          await page.addInitScript(({ key }) => {
            if (!sessionStorage.getItem('seeded')) {
              localStorage.setItem(key, 'synthetic-denied-token');
              sessionStorage.setItem('seeded', '1');
            }
          }, { key: transport === 'bearer' ? 'access_token' : 'planner_telegram_session' });
        }
        let loggedOut = false;
        let logouts = 0;
        await page.route('**/*', async route => {
          const url = new URL(route.request().url());
          if (url.origin !== base) return route.abort();
          if (!url.pathname.includes('/api/')) return route.continue();
          if (url.pathname.endsWith('/auth/logout')) {
            assert.equal(route.request().method(), 'POST');
            loggedOut = true; logouts++;
            return route.fulfill({ json: { ok: true } });
          }
          if (url.pathname.endsWith('/auth/telegram/session'))
            return route.fulfill({ status: loggedOut ? 401 : 200, json: { authenticated: !loggedOut } });
          if (url.pathname.endsWith('/auth/telegram/config'))
            return route.fulfill({ status: 503, json: { detail: 'Synthetic widget unavailable' } });
          if (url.pathname === '/api/v1/auth/login') {
            assert.deepEqual(route.request().postDataJSON(), { email: 'other@example.invalid', password: 'synthetic-password' });
            return route.fulfill({ json: { access_token: 'synthetic-new-token' } });
          }
          if (url.pathname.endsWith('/me')) {
            if (route.request().headers().authorization !== 'Bearer synthetic-new-token')
              return route.fulfill({ status: deniedStatus, json: { detail: 'Synthetic account denied' } });
            return route.fulfill({ json: {
              user: { id: 9, name: 'Other account', role: 'supervisor', active: 1, telegram_id: 999, bitrix_id: null },
              today: '2026-10-30', timezone: 'Europe/Moscow', crm: { connected: false, message: 'mock' },
            } });
          }
          return route.fulfill({ json: [] });
        });
        await page.goto(`${base}/manager/`);
        await page.getByRole('button', { name: 'Попробовать снова', exact: true }).waitFor();
        assert.match(await page.locator('.welcome').textContent(), /Synthetic account denied/);
        await page.getByRole('button', { name: 'Вернуться ко входу', exact: true }).click();
        await page.locator('input[type=email]').waitFor();
        assert.equal(logouts, 1, transport);
        assert.deepEqual(await page.evaluate(() => [localStorage.getItem('access_token'), localStorage.getItem('planner_telegram_session')]), [null, null]);
        await page.locator('input[type=email]').fill('other@example.invalid');
        await page.locator('input[type=password]').fill('synthetic-password');
        await page.getByRole('button', { name: 'Войти →', exact: true }).click();
        await page.locator('.workspace').waitFor();
        assert.match(await page.locator('.sidebar .profile').textContent(), /Other account/);
      } finally { await context.close(); }
    }
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
});
