const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

function client(fetch, { initData, storage = {} } = {}) {
  const values = new Map(Object.entries(storage));
  const context = vm.createContext({
    window: { Telegram: { WebApp: { initData } } },
    localStorage: {
      getItem: key => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: key => values.delete(key),
    },
    fetch, AbortController, setTimeout, clearTimeout,
  });
  function load(filename, dependencies = {}) {
    const source = fs.readFileSync(path.join(__dirname, '../src', filename), 'utf8');
    const compiled = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    context.module = { exports: {} };
    context.exports = context.module.exports;
    context.require = name => {
      assert(name in dependencies, `Unexpected dependency ${name}`);
      return dependencies[name];
    };
    vm.runInContext(`(function(require, module, exports) {${compiled}\n})(require, module, exports)`, context);
    return context.module.exports;
  }
  const validators = load('response.ts');
  return load('api.ts', { './response': validators });
}

const me = {
  user: { id: 1, name: 'Synthetic supervisor', telegram_id: 123, bitrix_id: null, role: 'supervisor', active: 1 },
  today: '2026-10-30', timezone: 'Europe/Moscow',
  crm: { connected: false, message: 'Synthetic test data' },
};

test('a successful cookie session probe can load /me without any JS-readable token', async () => {
  const calls = [];
  const api = client(async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith('/auth/telegram/session')) {
      return Response.json({ authenticated: true, telegram_id: 123 });
    }
    assert.equal(url, '/manager/api/me');
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.headers.Authorization, undefined);
    return Response.json(me);
  });
  assert.equal(await api.hasTelegramCookieSession(), true);
  const result = await api.api('/me');
  assert.equal(result.user.role, 'supervisor');
  assert.equal(calls.length, 2);
});

for (const [name, options, expected] of [
  ['Telegram Mini App', { initData: 'synthetic-init', storage: { planner_telegram_session: 'tg', access_token: 'web' } }, 'tma synthetic-init'],
  ['legacy Telegram web token', { storage: { planner_telegram_session: 'synthetic-tg', access_token: 'web' } }, 'tgweb synthetic-tg'],
  ['Lead Flow bearer token', { storage: { access_token: 'synthetic-web' } }, 'Bearer synthetic-web'],
]) {
  test(`${name} retains its Authorization transport and precedence`, async () => {
    const api = client(async (_url, request) => {
      assert.equal(request.headers.Authorization, expected);
      return Response.json(me);
    }, options);
    await api.api('/me');
  });
}

test('an anonymous request reaches server authorization and surfaces its 401', async () => {
  let calls = 0;
  const api = client(async (_url, request) => {
    calls++;
    assert.equal(request.headers.Authorization, undefined);
    return Response.json({ detail: 'Войдите через Telegram.' }, { status: 401 });
  });
  await assert.rejects(api.api('/me'), /Войдите через Telegram/);
  assert.equal(calls, 1);
});

test('cookie mutations preserve their body and surface 403 without retry', async () => {
  let calls = 0;
  const api = client(async (url, request) => {
    calls++;
    assert.equal(url, '/manager/api/plans');
    assert.equal(request.method, 'POST');
    assert.equal(request.credentials, 'same-origin');
    assert.equal(request.headers.Authorization, undefined);
    assert.equal(request.body, JSON.stringify({ day: '2026-10-30' }));
    return Response.json({ detail: 'Нет доступа к этому плану.' }, { status: 403 });
  });
  await assert.rejects(api.api('/plans', 'POST', { day: '2026-10-30' }), /Нет доступа/);
  assert.equal(calls, 1);
});

test('cookie responses still pass runtime validation', async () => {
  const api = client(async () => Response.json({ user: { role: 'supervisor' } }));
  await assert.rejects(api.api('/me'), /неожиданном формате/);
});
