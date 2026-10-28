// Isolated checks for the inbox ordering/paging and the session view layout.
// Run: MOBILE_BROWSER_ENV=/path/to/playwright-environment node tests/frontend_layout_test.mjs
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(join(process.env.MOBILE_BROWSER_ENV || '/private/tmp/cc-mobile-browser', 'package.json'));
const { chromium } = require('playwright');
const root = resolve('src/cc_tmux_api/static');
const now = Date.now() / 1000;
const base = { host:'local', host_label:'Local', session:'s', workdir:'/workspace/project', status:'idle', lifecycle:'alive', alive:true, revision:'1', command:'claude' };
const ended = Array.from({ length:150 }, (_, i) => ({ ...base, id:`local/ended-${i}`, title:`Ended ${i}`, lifecycle:'ended', alive:false, ended_at:now - 3600 - i }));
const live = [
  { ...base, id:'local/old-idle', title:'Old idle', updated_at:now - 7200 },
  { ...base, id:'local/new-idle', title:'New idle', updated_at:now - 60 },
  { ...base, id:'local/busy', title:'Busy', status:'working', updated_at:now - 3000 },
  { ...base, id:'native/ask', title:'Asks', transport:'native', status:'waiting', updated_at:now - 5000,
    native_control:{ request_id:'perm-1', tool_name:'Bash', input:{ command:'ls' } } },
  { ...base, id:'native/run', title:'Runs', transport:'native', status:'working', updated_at:now - 10, model:'claude-opus-5-5',
    permission_mode:'default', capabilities:{ set_model:true, set_permission_mode:true } },
  { ...base, id:'native/done', title:'Done native', transport:'native', lifecycle:'ended', alive:false, ended_at:now - 30 },
];
const many = Array.from({ length:300 }, (_, i) => ({ role:i % 2 ? 'assistant' : 'user', id:`m${i}`, text:`Message number ${i}` }));
const browser = await chromium.launch({ channel:'chrome', headless:true });
const failures = [];
try {
  for (const width of [390, 1440]) {
    const origin = 'http://localhost:9877';
    const context = await browser.newContext({ viewport:{ width, height:844 }, serviceWorkers:'block' });
    const page = await context.newPage();
    page.on('pageerror', e => failures.push(e.message));
    const actions = [];
    const until = async check => { for (let i = 0; i < 100 && !check(); i++) await page.waitForTimeout(50); assert.ok(check()); };
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) return route.abort();
      const json = data => route.fulfill({ json:data });
      if (url.pathname === '/api/auth') return json({ authenticated:true, required:false });
      if (url.pathname === '/api/sessions') return json({ hosts:[{ id:'local', label:'Local', available:true }], sessions:[...ended, ...live], updated_at:now });
      if (url.pathname === '/api/push/preferences') return json({ muted:[] });
      if (url.pathname.endsWith('/action')) { actions.push(route.request().postDataJSON()); return json({ ok:true }); }
      if (url.pathname.startsWith('/api/sessions/')) {
        const id = decodeURIComponent(url.pathname.slice('/api/sessions/'.length));
        const s = [...ended, ...live].find(x => x.id === id);
        // Slow detail: the list snapshot renders first, which is what used to strand the scroll.
        await new Promise(r => setTimeout(r, 300));
        return json({ ...s, messages:s.transport === 'native' && s.lifecycle === 'alive' ? many : [] });
      }
      const path = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/cc-static\//, '');
      try { return route.fulfill({ body:await readFile(join(root, path)), contentType:path.endsWith('css') ? 'text/css' : path.endsWith('html') ? 'text/html' : 'text/javascript' }); }
      catch { return route.fulfill({ status:404, body:'' }); }
    });
    await page.goto(`${origin}/`);
    await page.locator('.session-row').first().waitFor();
    const titles = async () => page.locator('.session-row .row-title').allTextContents();
    assert.deepEqual(await titles(), ['Asks', 'Runs', 'Busy', 'New idle', 'Old idle'], 'Default Active view: needs-you, working, then idle by recency; no ended rows');
    await page.getByRole('button', { name:'Ended', exact:true }).click();
    assert.equal(await page.locator('.session-row').count(), 60, 'Long lists are paged');
    assert.equal(await page.locator('.session-row .row-title').first().textContent(), 'Done native', 'Ended rows are newest first');
    await page.getByRole('button', { name:'Show 91 more of 91' }).click();
    assert.equal(await page.locator('.session-row').count(), 151);
    await page.reload();
    await page.locator('.session-row').first().waitFor();
    assert.equal(await page.getByRole('button', { name:'Ended', exact:true }).getAttribute('aria-pressed'), 'true', 'The chosen filter survives a reload');
    await page.getByRole('button', { name:'Done native' }).click();
    await page.getByRole('heading', { name:'No messages recorded', exact:true }).waitFor();
    assert.equal(await page.locator('.detail-header .status').count(), 0, 'Ended sessions show no live status in the header');
    if (width === 390) await page.getByRole('button', { name:'Back to sessions', exact:true }).click();
    await page.getByRole('button', { name:'Active', exact:true }).click();

    await page.getByRole('button', { name:/Asks/ }).click();
    const card = page.getByRole('region', { name:'Tool permission' });
    await card.waitFor();
    await page.locator('.show-earlier').waitFor();
    await page.waitForTimeout(200);
    const visible = await page.evaluate(() => {
      const c = document.querySelector('.question-card').getBoundingClientRect();
      const t = document.querySelector('.transcript').getBoundingClientRect();
      return c.top >= t.top && c.top < t.bottom;
    });
    assert.ok(visible, 'A pending approval is in view once the history behind it has loaded');
    assert.equal(await page.locator('.messages .message').count(), 120, 'Only the latest messages render at first');
    await page.getByRole('button', { name:'Show 180 earlier messages' }).click();
    assert.equal(await page.locator('.messages .message').count(), 300);
    assert.equal(await page.getByRole('tab').count(), 0, 'A native session has no single-tab tab bar');
    if (width === 390) await page.getByRole('button', { name:'Back to sessions', exact:true }).click();

    await page.getByRole('button', { name:/Runs/ }).click();
    await page.locator('.working-indicator').waitFor();
    const model = page.getByRole('combobox', { name:'Model' });
    assert.equal(await model.inputValue(), 'claude-opus-5-5');
    await model.selectOption('sonnet');
    await until(() => actions.length === 1);
    assert.deepEqual([actions.at(-1).action, actions.at(-1).text], ['set_model', 'sonnet']);
    assert.equal(await model.inputValue(), 'claude-opus-5-5', 'The shown model stays the confirmed one until Claude reports a change');
    await page.waitForFunction(() => !document.querySelector('.native-bar select:disabled'));
    await page.getByRole('combobox', { name:'Permission mode' }).selectOption('plan');
    await until(() => actions.length === 2);
    assert.deepEqual([actions.at(-1).action, actions.at(-1).text], ['set_permission_mode', 'plan']);
    const reply = page.getByRole('textbox', { name:'Message this session', exact:true });
    await reply.fill('first line');
    await reply.press('Shift+Enter');
    await reply.pressSequentially('second line');
    assert.equal(await reply.inputValue(), 'first line\nsecond line', 'Shift+Enter adds a line');
    const before = actions.length;
    await page.waitForFunction(() => !document.querySelector('.send-button').disabled);
    await reply.press('Enter');
    await until(() => actions.length === before + 1);
    await page.waitForFunction(() => document.querySelector('#reply').value === '');
    assert.deepEqual([actions.at(-1).action, actions.at(-1).text], ['send', 'first line\nsecond line'], 'Enter sends with a fine pointer');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    console.log(`PASS ${width}px: inbox order/paging/filter memory, approval in view, message paging, model/mode pickers, Enter to send`);
    await context.close();
  }
  assert.deepEqual(failures, []);
} finally { await browser.close(); }
