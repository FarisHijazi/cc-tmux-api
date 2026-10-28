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
  { ...base, id:'native/stream', title:'Stream', transport:'native', status:'working', updated_at:now - 5, slash_commands:['clear', 'compact'] },
];
// Mirrors server.py: `ended=0` drops ended rows; `after` returns only newer messages;
// `commands` omits unchanged slash commands.
const listFor = url => {
  const sessions = [...ended, ...live];
  if (url.searchParams.get('ended') !== '0') return { sessions };
  const kept = sessions.filter(s => s.lifecycle !== 'ended');
  return { sessions:kept, ended_count:sessions.length - kept.length };
};
const detailFor = (s, messages, url) => {
  const result = { ...s, messages };
  const after = url.searchParams.get('after');
  const at = after ? messages.findLastIndex(m => m.id === after) : -1;
  if (at >= 0) Object.assign(result, { messages:messages.slice(at + 1), messages_after:after });
  if (s.slash_commands) {
    result.slash_commands_digest = `digest-${s.slash_commands.length}`;
    if (url.searchParams.get('commands') === result.slash_commands_digest) delete result.slash_commands;
  }
  return result;
};
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
    const listRequests = [];
    const streamRequests = [];
    const streamResponses = [];
    const stream = [
      ...Array.from({ length:5 }, (_, i) => ({ role:i % 2 ? 'assistant' : 'user', id:`st${i}`, text:`Stream message ${i}` })),
      { role:'tool', id:'st5', name:'Bash', input:'{"command":"ls"}', text:'' },
    ];
    const until = async check => { for (let i = 0; i < 100 && !check(); i++) await page.waitForTimeout(50); assert.ok(check()); };
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) return route.abort();
      const json = data => route.fulfill({ json:data });
      if (url.pathname === '/api/auth') return json({ authenticated:true, required:false });
      if (url.pathname === '/api/sessions') { listRequests.push(url.search); return json({ hosts:[{ id:'local', label:'Local', available:true }], ...listFor(url), updated_at:now }); }
      if (url.pathname === '/api/push/preferences') return json({ muted:[] });
      if (url.pathname.endsWith('/action')) { actions.push(route.request().postDataJSON()); return json({ ok:true }); }
      if (url.pathname.startsWith('/api/sessions/')) {
        const id = decodeURIComponent(url.pathname.slice('/api/sessions/'.length));
        const s = [...ended, ...live].find(x => x.id === id);
        if (id === 'native/stream') {
          streamRequests.push(url.searchParams);
          const result = detailFor(s, structuredClone(stream), url);
          streamResponses.push(result);
          return json(result);
        }
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
    assert.deepEqual(await titles(), ['Asks', 'Stream', 'Runs', 'Busy', 'New idle', 'Old idle'], 'Default Active view: needs-you, working, then idle by recency; no ended rows');
    assert.ok(listRequests.length && listRequests.every(q => q === '?ended=0'), 'The Active view never downloads ended rows');
    await page.getByRole('button', { name:'Ended', exact:true }).click();
    await page.getByRole('button', { name:/^Show 91 more/ }).waitFor();
    assert.equal(await page.locator('.session-row').count(), 60, 'Long lists are paged');
    assert.equal(await page.locator('.session-row .row-title').first().textContent(), 'Done native', 'Ended rows are newest first');
    await page.getByRole('button', { name:'Show 91 more of 91' }).click();
    assert.equal(await page.locator('.session-row').count(), 151);
    assert.equal(listRequests.at(-1), '', 'The Ended view asks for every row');
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
    if (width === 390) await page.getByRole('button', { name:'Back to sessions', exact:true }).click();

    await page.getByRole('button', { name:/Stream/ }).click();
    await until(() => streamRequests.length >= 2);
    assert.equal(streamRequests[0].get('after'), null, 'The first detail request fetches everything');
    assert.equal(streamRequests[1].get('after'), 'st4', 'The cursor stays before a tool call that is still waiting for its result');
    assert.equal(streamRequests[1].get('commands'), 'digest-2');
    assert.equal(streamResponses[1].slash_commands, undefined, 'Unchanged slash commands are not resent');
    assert.deepEqual(streamResponses[1].messages.map(m => m.id), ['st5']);
    Object.assign(stream[5], { text:'file-a file-b', done:true });
    stream.push({ role:'assistant', id:'st6', text:'Stream reply 6' });
    await page.getByText('Stream reply 6').waitFor();
    const seen = streamRequests.length;
    await until(() => streamRequests.length >= seen + 1);
    assert.equal(streamRequests.at(-1).get('after'), 'st6', 'Once the tool finished, the cursor moves to the newest message');
    assert.deepEqual(streamResponses.at(-1).messages, [], 'A poll with nothing new carries no messages');
    assert.equal(await page.locator('.messages .message, .messages .tool-call').count(), 7, 'Deltas are merged without duplicates');
    assert.equal(await page.locator('.messages .tool-call.running').count(), 0, 'A completed tool call is no longer shown as running');
    assert.ok(await page.locator('.messages').getByText('file-a file-b').count(), 'A completed tool call shows its result');
    const composer = page.getByRole('textbox', { name:'Message this session', exact:true });
    await composer.fill('/co');
    await page.getByRole('option', { name:'/compact' }).waitFor();
    console.log(`PASS ${width}px: inbox order/paging/filter memory, approval in view, message paging, model/mode pickers, Enter to send, incremental list and detail polling`);
    await context.close();
  }
  assert.deepEqual(failures, []);
} finally { await browser.close(); }
