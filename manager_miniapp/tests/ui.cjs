const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'src', 'App.tsx'), 'utf8');
const response = fs.readFileSync(path.join(root, 'src', 'response.ts'), 'utf8');
const builtIndex = fs.readFileSync(path.join(root, '..', 'app', 'static', 'manager', 'index.html'), 'utf8');

assert(source.includes('/crm/summary?employee_id='), 'CRM summary is not requested by the UI');
assert(source.includes('Только чтение'), 'read-only CRM label is missing');
assert(response.includes("route === '/crm/summary'"), 'CRM response is not validated');
const asset = builtIndex.match(/src="(\/manager\/[^"]+\.js)"/);
assert(asset, 'built JavaScript asset is missing');
const relativeAsset = asset[1].replace(/^\/manager\//, '');
assert(fs.existsSync(path.join(root, '..', 'app', 'static', 'manager', relativeAsset)), 'referenced JavaScript asset does not exist');

console.log('Manager Mini App UI smoke checks passed');
