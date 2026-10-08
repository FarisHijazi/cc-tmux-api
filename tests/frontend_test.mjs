// Isolated frontend contract checks; no real server, sessions, or account data.
// Run: MOBILE_BROWSER_ENV=/private/tmp/cc-mobile-browser node tests/frontend_test.mjs
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import assert from 'node:assert/strict';
import vm from 'node:vm';
const require = createRequire(join(process.env.MOBILE_BROWSER_ENV || '/private/tmp/cc-mobile-browser', 'package.json'));
const { chromium } = require('playwright');
const root = resolve('src/cc_tmux_api/static');
const listeners = {};
let opened = '';
const swContext = {
  URL, URLSearchParams, Response,
  self:{location:{origin:'https://hq.test'},addEventListener:(name, fn) => { listeners[name] = fn; },
    clients:{matchAll:async () => [],openWindow:async url => { opened = url; } }},
};
vm.runInNewContext(await readFile(join(root,'sw.js'),'utf8'),swContext);
let intercepted = false;
listeners.fetch({request:{url:'https://hq.test/api/sessions',method:'GET'},respondWith:() => { intercepted = true; }});
assert.equal(intercepted,false,'Session API must never enter the service-worker cache');
let click;
listeners.notificationclick({notification:{data:{url:'/#session=host%2Fpane'},close(){}},waitUntil:p => {click=p;}});
await click;
assert.equal(opened,'https://hq.test/#session=host%2Fpane');
const browser = await chromium.launch({ channel:'chrome', headless:true });
const failures = [];
try {
  for (const width of [390, 1440]) {
    const origin = width === 390 ? 'http://hq.test:9876' : 'http://localhost:9876';
    const context = await browser.newContext({ viewport:{ width, height:844 }, serviceWorkers:'block' });
    const page = await context.newPage();
    const remoteRequests = [];
    await context.route('**/*', route => {
      if (new URL(route.request().url()).origin !== origin) {
        remoteRequests.push(route.request().url());
        return route.abort();
      }
      return route.fallback();
    });
    page.on('pageerror', e => failures.push(e.message));
    let revision = 1;
    let actionCount = 0;
    let failAction = false;
    let failDetail = false;
    let messageText = 'Review ready.\n\n'.repeat(60) + '<script>window.injected = true</script>';
    const session = { id:'host/pane', host:'local', host_label:'Local', session:'research', pane:'%1', title:'Review the implementation', workdir:'/workspace/project', status:'idle', status_label:'Permission required', status_emoji:'🔐', lifecycle:'alive',alive:true,last_seen:Date.now()/1000,updated_at:Date.now()/1000, revision:'1', managed:false, command:'claude' };
    if (width === 390) { delete session.lifecycle; delete session.alive; }
    let extraSessions = [];
    let includePrimary = true;
    let hosts = [{ id:'local',label:'Local',available:true }];
    let releaseInitial;
    const initialIndex = new Promise(resolve => {releaseInitial=resolve;});
    let menu = { question:'Which option should we use?', options:[{ index:0,label:'First option',description:'Keep the current behavior',selected:false },{index:1,label:'Second option',description:'Use the revised behavior',selected:false}],multi_select:false,focused_index:0,kind:'question' };
    const actions = [];
    const workerActions = [];
    await context.route(`${origin}/**`, async route => {
      const request = route.request();
      const url = new URL(request.url());
      let data;
      if (url.pathname === '/api/auth') data = { authenticated:true, required:false };
      else if (url.pathname === '/api/sessions') { await initialIndex; data = { hosts, sessions:[...(includePrimary ? [session] : []),...extraSessions],updated_at:Date.now()/1000 }; }
      else if (url.pathname === '/api/workers/action') {
        workerActions.push(request.postDataJSON());
        data = {ok:true};
      } else if (url.pathname.endsWith('/action')) {
        actionCount++; actions.push(request.postDataJSON());
        if (failAction) return route.fulfill({ status:409, json:{detail:'Session changed'} });
        const action = actions.at(-1);
        assert.equal(action.revision, String(revision));
        revision++;
        if (action.action === 'select') {
          menu.focused_index = action.index;
          if (menu.multi_select) menu.options[action.index].selected = !menu.options[action.index].selected;
        }
        if (action.key === 'Down') menu.focused_index++;
        if (action.action === 'type') {
          menu.signature = 'changed-after-typing';
          menu.options[menu.focused_index].label = action.text;
        }
        data = {ok:true};
      } else if (url.pathname.startsWith('/api/sessions/')) {
        if (failDetail) return route.fulfill({status:503,json:{detail:'Host cannot be reached'}});
        const target = extraSessions.find(s => s.id === decodeURIComponent(url.pathname.slice('/api/sessions/'.length))) || session;
        data = target === session && (session.lifecycle === undefined || session.lifecycle === 'alive') ? {...session, revision:String(revision), menu, tail:menu?.kind === 'review' ? 'Review your answers\n\nWhich items?\n→ Map, Rope\n\nReady to submit your answers?' : 'Ready for input',messages:[{role:'assistant',text:messageText}]} : {...target,revision:String(revision),tail:target.tail || '',messages:[]};
      }
      else if (url.pathname === '/api/push/preferences') data = {muted:[]};
      else if (url.pathname === '/api/push') data = {available:false,public_key:'',reason:'Not configured'};
      else if (url.pathname === '/api/workers') data = {workers:[
        {session:'active-worker',title:'Active worker',status:'idle',control:{session_id:'host/worker',revision:'worker-revision'}},
        {session:'inactive-worker',title:'Inactive worker',status:'done',control:null},
      ],routines:[],others:[]};
      if (data) return route.fulfill({json:data});
      const path = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/cc-static\//,'');
      try { return route.fulfill({body:await readFile(join(root,path)),contentType:path.endsWith('.js') || path.endsWith('.mjs') ? 'text/javascript' : path.endsWith('.css') ? 'text/css' : 'text/html'}); }
      catch { return route.fulfill({status:404,body:'Not found'}); }
    });
    await page.goto(`${origin}/`);
    await page.getByText('Loading sessions…',{exact:true}).waitFor();
    assert.equal(await page.locator('.inbox .total,.inbox .filter-count,.inbox .empty').count(),0,'Fresh loading must not show zero counts or empty states');
    releaseInitial();
    if (width === 390) {
      assert.equal(await page.evaluate(() => window.isSecureContext),false);
      assert.equal(await page.evaluate(() => typeof crypto.randomUUID),'undefined');
      assert.equal(await page.evaluate(() => 'serviceWorker' in navigator),false);
    }
    await page.getByRole('button', { name:/Review the implementation/ }).waitFor();
    assert.equal(await page.locator('.session-row .status-emoji').textContent(),'🔐');
    assert.ok((await page.locator('.session-row .status').textContent()).includes('Permission required'));
    await page.getByRole('button',{name:/Needs you/}).click();
    for (const label of ['WAITING for input','Blocked','Failed','Attention required','Permission required','Accident reported']) {
      session.status_label = label;
      await page.getByRole('button',{name:'Refresh sessions',exact:true}).click();
      await page.waitForFunction(label => document.querySelector('.session-row .status')?.textContent.includes(label),label);
      assert.equal(await page.locator('.session-row').count(),1,'Canonical labels must drive attention even when raw status is idle');
    }
    session.status_label = 'Finished';
    await page.getByRole('button',{name:'Refresh sessions',exact:true}).click();
    await page.getByRole('heading',{name:'No matching sessions'}).waitFor();
    session.status = 'waiting';
    delete session.status_label;
    delete session.status_emoji;
    await page.getByRole('button',{name:'Refresh sessions',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('.session-row .status')?.textContent === 'waiting');
    assert.equal(await page.locator('.session-row .status-emoji').count(),0,'No emoji should be invented');
    session.status = 'idle';
    session.menu = {kind:'question'};
    await page.getByRole('button',{name:'Refresh sessions',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('.session-row .status')?.textContent === 'idle');
    assert.equal(await page.locator('.session-row').count(),1,'A present menu must require attention');
    delete session.menu;
    session.has_question = true;
    session.status_label = 'Finished';
    await page.getByRole('button',{name:'Refresh sessions',exact:true}).click();
    await page.locator('.session-row .action-label').getByText('Question',{exact:true}).waitFor();
    session.has_question = false;
    session.attention_kind = 'permission';
    await page.getByRole('button',{name:'Refresh sessions',exact:true}).click();
    await page.locator('.session-row .action-label').getByText('Needs permission',{exact:true}).waitFor();
    delete session.has_question; delete session.attention_kind;
    session.status_label = 'Permission required';
    session.status_emoji = '🔐';
    await page.getByRole('button',{name:'All sessions',exact:true}).click();
    await page.getByRole('button',{name:'Refresh sessions',exact:true}).click();
    await page.waitForFunction(() => !!document.querySelector('.session-row .status-emoji'));
    await page.getByLabel('Search sessions').fill('unmatched query');
    await page.getByRole('heading', {name:'No matching sessions'}).waitFor();
    await page.getByLabel('Search sessions').fill('');
    await page.getByRole('button', {name:'Directory',exact:true}).click();
    await page.getByRole('heading', {name:'/workspace/project 1',exact:true}).waitFor();
    await page.screenshot({path:`/private/tmp/hq-inbox-${width}.png`,fullPage:true});
    await page.getByRole('button', { name:/Review the implementation/ }).click();
    await page.getByRole('heading', {name:'Which option should we use?'}).waitFor();
    assert.equal(await page.locator('.detail-lifecycle .alive').count(),1,'Verified legacy API records without lifecycle fields must remain controllable');
    await page.waitForFunction(() => {
      const question = document.querySelector('.question-card h3').getBoundingClientRect();
      const area = document.querySelector('.transcript').getBoundingClientRect();
      return question.top >= area.top && question.bottom <= area.bottom;
    });
    assert.equal(await page.getByLabel('Message this session').isVisible(),false,'Questions must not compete with an expanded message composer');
    await page.locator('.transcript').evaluate(el => { el.scrollTop = 0; });
    await page.getByRole('button',{name:'Refresh session',exact:true}).click();
    await page.waitForFunction(() => !document.querySelector('.detail-header button[aria-label="Refresh session"]').disabled);
    assert.equal(await page.locator('.transcript').evaluate(el => el.scrollTop),0,'Polling the same question must not jump the scroll position');
    assert.equal(await page.locator('.detail-header .status-emoji').textContent(),'🔐');
    assert.ok((await page.locator('.detail-header .status').textContent()).includes('Permission required'));
    assert.equal(await page.evaluate(() => window.injected), undefined);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.getByRole('button',{name:'Draft a message',exact:true}).click();
    await page.getByLabel('Message this session').fill('A retained draft');
    if (width === 390) {
      await page.setViewportSize({width,height:540});
      await page.waitForFunction(() => document.documentElement.style.getPropertyValue('--app-height') === '540px');
      const composer = await page.getByLabel('Message this session').boundingBox();
      assert.ok(composer.y + composer.height <= 540, 'Composer must fit the shortened viewport');
      await page.setViewportSize({width,height:844});
    }
    await page.getByRole('button', {name:/Second option/}).click();
    await page.waitForFunction(() => document.querySelector('.question-option.focused')?.textContent.includes('Second option'));
    assert.equal(actions[0].revision,'1');
    assert.equal(actions[0].action,'select');
    assert.equal(actions[0].index,1);
    assert.equal(typeof actions[0].request_id,'string');
    await page.reload();
    await page.getByRole('button',{name:/Draft a message/}).click();
    await page.getByLabel('Message this session').waitFor();
    assert.equal(await page.getByLabel('Message this session').inputValue(),'A retained draft');
    menu.multi_select = true;
    menu.options.forEach(option => { option.kind = 'option'; });
    menu.options.push({index:2,label:'Submit',kind:'submit',selected:false,description:''});
    await page.getByRole('button', {name:'Refresh session',exact:true}).click();
    await page.getByRole('button', {name:'Continue',exact:true}).waitFor();
    assert.equal(await page.locator('.question-options').getByRole('button',{name:'Submit',exact:true}).count(),0,'The Submit navigation row must not duplicate Continue');
    await page.getByRole('button', {name:/First option/}).click();
    await page.waitForFunction(() => document.querySelector('.question-option.chosen')?.textContent.includes('First option'));
    assert.equal(actions.at(-1).action,'select');
    assert.equal(actions.at(-1).index,0);
    await page.getByRole('button', {name:'Continue',exact:true}).click();
    await page.waitForFunction(() => !document.querySelector('.question-card .primary').disabled);
    assert.equal(actions.at(-1).action,'select');
    assert.equal(actions.at(-1).index,2,'Review must target the native Submit navigation row');
    menu.options[2] = {...menu.options[2],kind:'next',label:'Next'};
    await page.getByRole('button',{name:'Refresh session',exact:true}).click();
    await page.waitForFunction(() => !document.querySelector('.detail-header button[aria-label="Refresh session"]').disabled);
    assert.equal(await page.locator('.question-options').getByRole('button',{name:'Next',exact:true}).count(),0);
    await page.getByRole('button',{name:'Continue',exact:true}).click();
    await page.waitForFunction(() => !document.querySelector('.question-card .primary').disabled);
    assert.equal(actions.at(-1).action,'select');
    assert.equal(actions.at(-1).index,2,'Batch Continue must target the native Next navigation row');
    menu = {...menu,kind:'review',question:'Review your answers',options:[{index:0,label:'Submit answers',selected:false,description:''}]};
    await page.getByRole('button', {name:'Refresh session',exact:true}).click();
    await page.getByRole('button', {name:'Submit answer',exact:true}).click();
    assert.ok((await page.locator('.review-summary').textContent()).includes('Map, Rope'),'Native review answers must remain visible before final submission');
    await page.waitForFunction(() => !document.querySelector('.question-card .primary').disabled);
    assert.equal(actions.at(-1).action,'submit');
    menu = {question:'Write a custom answer',multi_select:false,kind:'question',focused_index:0,options:[{index:0,label:'Default',selected:false,description:''},{index:1,label:'Type something else…',kind:'free_text',selected:false,description:''}]};
    await page.getByRole('button', {name:'Refresh session',exact:true}).click();
    await page.getByRole('button', {name:/Type something else/}).click();
    await page.getByLabel('Your answer', {exact:true}).waitFor();
    assert.equal(actions.at(-1).action,'select','Custom choice uses backend focus-only selection');
    assert.equal(actions.at(-1).index,1);
    assert.equal(actions.at(-1).key,undefined,'Custom choice must not implicitly submit');
    await page.getByLabel('Your answer', {exact:true}).fill('An answer, with punctuation.');
    await page.getByRole('button', {name:'Insert answer',exact:true}).click();
    await page.getByText('Text inserted. Confirm when ready.',{exact:true}).waitFor();
    await page.getByRole('button',{name:/An answer, with punctuation/}).waitFor();
    assert.equal(await page.getByRole('button',{name:'Confirm typed answer',exact:true}).isEnabled(),true,'Native placeholder replacement must preserve custom confirmation');
    await page.getByRole('button',{name:'Refresh session',exact:true}).click();
    await page.waitForFunction(() => !document.querySelector('.detail-header button[aria-label="Refresh session"]').disabled);
    assert.equal(await page.getByRole('button',{name:'Confirm typed answer',exact:true}).isEnabled(),true,'Subsequent refresh must retain custom confirmation');
    menu.options[1].kind = 'free_text';
    await page.getByRole('button',{name:'Refresh session',exact:true}).click();
    await page.waitForFunction(() => !document.querySelector('.detail-header button[aria-label="Refresh session"]').disabled);
    assert.equal(await page.getByRole('button',{name:'Confirm typed answer',exact:true}).isEnabled(),true,'Backend kind must preserve confirmation for a non-placeholder label');
    assert.equal(actions.at(-1).action,'type');
    assert.equal(actions.at(-1).text,'An answer, with punctuation.');
    assert.equal(actions.at(-1).key,undefined,'Type must not add Enter');
    await page.getByRole('button', {name:'Confirm typed answer',exact:true}).click();
    await page.waitForFunction(() => !document.querySelector('.question-card .primary').disabled);
    assert.equal(actions.at(-1).key,'Enter');
    failAction = true;
    const beforeFailure = actionCount;
    await page.getByLabel('Your answer', {exact:true}).fill('Keep this on error');
    await page.getByRole('button', {name:'Insert answer',exact:true}).click();
    await page.getByRole('alert').filter({hasText:'Session changed'}).waitFor();
    await page.waitForTimeout(300);
    assert.equal(actionCount,beforeFailure+1,'Failed actions must not be automatically retried');
    assert.equal(await page.getByLabel('Your answer',{exact:true}).inputValue(),'Keep this on error');
    assert.equal(await page.getByRole('button',{name:'Confirm typed answer',exact:true}).isDisabled(),true,'Uninserted text must not be silently excluded from confirmation');
    assert.equal(new Set(actions.map(a => a.request_id)).size,actions.length);
    assert.ok(actions.every(a => /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(a.request_id)),'Both UUID implementations must generate valid v4 IDs');
    await page.screenshot({path:`/private/tmp/hq-frontend-${width}.png`,fullPage:true});
    menu.question = 'A different question';
    await page.getByRole('button',{name:'Refresh session',exact:true}).click();
    await page.getByRole('heading',{name:'A different question'}).waitFor();
    assert.equal(await page.getByRole('button',{name:'Confirm typed answer',exact:true}).isDisabled(),true,'Inserted custom state must not leak into another question');
    menu = null;
    messageText = [
      '## Findings',
      'The response now has **clear sections**, readable paragraphs, and `inline code`.',
      '## What was done',
      '1. **Kept the existing app.** Improved the mobile conversation.\n2. Added safe formatting.\n   - Preserved nested lists.\n   - Kept long code inside its own scroller.',
      '| Area | Result |\n| --- | --- |\n| Conversation | Readable on mobile |\n| Links | Safe protocols only |',
      '> A short note stays separate from the main response.',
      '```js\nconst result = "' + 'a'.repeat(180) + '";\n<script>window.injected = true</script>\n```',
      '[Documentation](https://example.com/guide) and [relative link](/guide).',
      '[Unsafe](javascript:alert%281%29) [Encoded](jav&#x61;script:alert%281%29)',
      '<a href="data:text/html,unsafe" onclick="window.injected=true" ping="https://tracker.invalid/ping" target="_blank" style="color:red">Unsafe data link</a>',
      '![Tracker](https://tracker.invalid/image.png)',
      '<img src="https://tracker.invalid/raw.png" srcset="https://tracker.invalid/large.png 2x" onerror="window.injected=true">',
      '<iframe src="https://tracker.invalid/frame"></iframe><script>window.injected=true</script><style>body{display:none}</style>',
      '<svg onload="window.injected=true"><image href="https://tracker.invalid/svg"/></svg><video poster="https://tracker.invalid/poster"></video>',
      '<form action="https://tracker.invalid/form"><input autofocus onfocus="window.injected=true"></form><link rel="stylesheet" href="https://tracker.invalid/style.css">',
    ].join('\n\n');
    await page.getByRole('button',{name:'Refresh session',exact:true}).click();
    const formatted = page.locator('.message-text');
    await formatted.getByRole('heading',{name:'Findings',exact:true}).waitFor();
    assert.equal(await formatted.locator('ol > li').count(),2);
    assert.equal(await formatted.locator('ul > li').count(),2);
    assert.equal(await formatted.locator('table tbody tr').count(),2);
    assert.equal(await formatted.locator('pre code').count(),1);
    assert.equal(await formatted.locator('blockquote').count(),1);
    assert.equal(await formatted.getByRole('link',{name:'Documentation',exact:true}).getAttribute('href'),'https://example.com/guide');
    assert.equal(await formatted.locator('script,style,img,iframe,svg,math,video,audio,object,embed,form,input,link').count(),0);
    assert.equal(await formatted.evaluate(el => [...el.querySelectorAll('*')].some(node => [...node.attributes].some(attr => /^(?:on|style$|src|ping$|target$)/i.test(attr.name)))),false);
    assert.equal(await formatted.evaluate(el => [...el.querySelectorAll('a[href]')].some(a => !['http:','https:','mailto:'].includes(new URL(a.href).protocol))),false);
    assert.equal(await page.evaluate(() => window.injected),undefined);
    assert.deepEqual(remoteRequests,[],'Neither vendored rendering nor hostile transcripts may load remote resources');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),false,'Formatted content must not overflow the viewport');
    await page.reload();
    await page.locator('.message-text').getByRole('heading',{name:'Findings',exact:true}).waitFor();
    await page.locator('.transcript').evaluate(el => { el.scrollTop = 0; });
    await page.screenshot({path:`/private/tmp/hq-markdown-${width}.png`,fullPage:true});
    const actionsBeforeOffline = actions.length;
    session.lifecycle = 'unreachable'; session.alive = null;
    failDetail = true;
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    await page.locator('.detail-lifecycle .unreachable').waitFor();
    assert.equal(await page.locator('.message-text').getByRole('heading',{name:'Findings',exact:true}).count(),1,'Previously viewed history must survive unreachable records without history');
    assert.equal(await page.locator('.detail .composer,.detail .key-row,.detail .question-card').count(),0,'Unreachable sessions must have no sending or terminal actions');
    assert.equal(await page.locator('.detail-lifecycle .ended').count(),0,'Unreachable must never mean ended');
    failDetail = false;
    session.lifecycle = 'alive'; session.alive = true;
    extraSessions = [
      {...session,id:'host/ended',title:'Archived session',lifecycle:'ended',alive:false,ended_at:1700000000,status:'waiting',status_label:'Waiting for input'},
      {...session,id:'remote/pane',host:'remote',host_label:'Remote',title:'Disconnected session',lifecycle:'unreachable',alive:null,last_seen:1700000000,status:'running',status_label:'Working',tail:'Last known terminal output'},
    ];
    hosts = [{id:'local',label:'Local',available:false,error:'Host cannot be reached'},{id:'remote',label:'Remote',available:false,error:'Host cannot be reached'}];
    if (width === 390) await page.getByRole('button',{name:'Back to sessions',exact:true}).click();
    await page.getByRole('button',{name:'Refresh sessions',exact:true}).click();
    await page.getByRole('button',{name:/Archived session/}).waitFor();
    assert.equal((await page.locator('.inbox .total').textContent()).replace(/\s/g,''),'2active','Ended records must not inflate the active count');
    await page.getByRole('button',{name:'Ended',exact:true}).click();
    assert.equal(await page.locator('.session-row').count(),1);
    assert.equal(await page.locator('.session-row .lifecycle.ended').count(),1,'Known ended sessions remain ended even when their host is offline');
    assert.ok((await page.locator('.session-row').textContent()).includes('Archived session'));
    await page.getByRole('button',{name:/Needs you/}).click();
    assert.equal(await page.locator('.session-row').count(),1,'Stale waiting activity must not make ended sessions actionable');
    assert.ok((await page.locator('.session-row').textContent()).includes('Review the implementation'));
    await page.getByRole('button',{name:'Active',exact:true}).click();
    assert.equal(await page.locator('.session-row').count(),2,'Unreachable sessions stay in Active, not Ended');
    await page.getByRole('button',{name:/Disconnected session/}).click();
    await page.locator('.detail-lifecycle .unreachable').waitFor();
    assert.equal(await page.locator('.detail .composer,.detail .key-row,.detail .question-card').count(),0);
    await page.getByRole('tab',{name:'Terminal',exact:true}).click();
    await page.getByText('Last known terminal output',{exact:true}).waitFor();
    if (width === 390) await page.getByRole('button',{name:'Back to sessions',exact:true}).click();
    await page.getByRole('button',{name:'Ended',exact:true}).click();
    await page.getByRole('button',{name:/Archived session/}).click();
    await page.locator('.detail-lifecycle .ended').waitFor();
    assert.ok((await page.locator('.detail-lifecycle').textContent()).includes('Nov'));
    assert.equal(await page.locator('.detail .composer,.detail .key-row,.detail .question-card').count(),0);
    await page.getByRole('heading',{name:'Conversation not available',exact:true}).waitFor();
    await page.getByRole('tab',{name:'Terminal',exact:true}).click();
    await page.getByText('No last known terminal output is available.',{exact:true}).waitFor();
    assert.equal(actions.length,actionsBeforeOffline,'Read-only lifecycle views must not send actions');
    await page.screenshot({path:`/private/tmp/hq-ended-${width}.png`,fullPage:true});
    for (const [status, explanation] of [
      ['identity_missing','HQ couldn’t identify the exact Claude conversation for this session.'],
      ['file_missing','Claude hasn’t saved a conversation file for this session.'],
      ['parser_empty','Claude’s conversation file has no readable messages yet.'],
      ['available','No conversation messages are available yet.'],
    ]) {
      const id = `host/${status}`;
      extraSessions.push({...session,id,title:'Unmapped conversation',lifecycle:'alive',alive:true,transcript_status:status,tail:'Live embedded terminal is available'});
      await page.evaluate(id => {location.hash = `session=${encodeURIComponent(id)}`;},id);
      const empty = page.locator('.transcript-empty');
      await empty.getByRole('heading',{name:'Conversation not available',exact:true}).waitFor();
      assert.ok((await empty.textContent()).includes(`${explanation} You can still use the live terminal below.`));
      assert.equal(await empty.locator('pre').textContent(),'Live embedded terminal is available');
      await empty.getByRole('button',{name:'Open terminal',exact:true}).click();
      assert.equal(await page.getByRole('tab',{name:'Terminal',exact:true}).getAttribute('aria-selected'),'true');
      await page.getByText('Live embedded terminal is available',{exact:true}).waitFor();
    }
    if (width === 390) await page.getByRole('button',{name:'Back to sessions',exact:true}).click();
    hosts = [
      {id:'local',label:'Local',host_path:['local'],available:true},
      {id:'vm',label:'VM',host_path:['vm'],available:true},
      {id:'vm/local',label:'VM / VM',host_path:['vm','local'],available:true},
      {id:'other',label:'Other',host_path:['other'],available:false},
      {id:'other/local',label:'Other / Local',host_path:['other','local'],available:false},
      {id:'remote',label:'Remote',host_path:['remote'],available:true},
      {id:'remote/local',label:'Remote / Local',host_path:['remote','local'],available:true},
    ];
    extraSessions = [
      {...session,id:'vm/session',host:'vm/local',host_label:'VM / VM',title:'Federated session',lifecycle:'alive',alive:true},
      {...session,id:'remote/session',host:'remote/local',host_label:'Remote / Local',title:'Remote session',lifecycle:'alive',alive:true},
    ];
    await page.getByRole('button',{name:'All sessions',exact:true}).click();
    await page.getByRole('button',{name:'Host',exact:true}).click();
    await page.getByRole('button',{name:'Refresh sessions',exact:true}).click();
    await page.getByRole('heading',{name:'VM 1',exact:true}).waitFor();
    await page.getByRole('heading',{name:'Remote 1',exact:true}).waitFor();
    assert.ok((await page.locator('.inbox-footer').textContent()).includes('3 of 4 hosts connected'),'Peer roots must not count as extra machines');
    assert.equal(await page.locator('.host-error').count(),1,'Only the unavailable physical leaf should render an error');
    assert.ok((await page.locator('.host-error').textContent()).includes('Other is unavailable'));
    await page.getByRole('button',{name:/Federated session/}).click();
    assert.equal(await page.evaluate(() => new URLSearchParams(location.hash.slice(1)).get('session')),'vm/session','Display normalization must preserve routing identity');
    if (width === 390) await page.getByRole('button',{name:'Back to sessions',exact:true}).click();
    hosts.push({id:'unknown-peer',label:'Offline peer',host_path:['unknown-peer'],available:false});
    await page.getByRole('button',{name:'Refresh sessions',exact:true}).click();
    await page.getByText('Offline peer is unavailable',{exact:true}).waitFor();
    assert.ok((await page.locator('.inbox-footer').textContent()).includes('3 of 5 hosts connected'),'An offline root without known leaves must remain visible');
    includePrimary = false; extraSessions = [];
    await page.getByRole('button',{name:'All sessions',exact:true}).click();
    await page.getByRole('button',{name:'Refresh sessions',exact:true}).click();
    await page.getByRole('heading',{name:'Waiting for hosts',exact:true}).waitFor();
    assert.equal(await page.locator('.inbox .total').count(),0,'An unavailable empty snapshot must not claim zero active sessions');
    assert.equal(await page.getByRole('heading',{name:'A clear workspace',exact:true}).count(),0,'Unavailable hosts must not be reported as an empty workspace');
    await page.getByRole('button',{name:'Workers',exact:true}).click();
    const activeWorker = page.locator('.worker-card').filter({has:page.getByRole('heading',{name:'Active worker',exact:true})});
    const inactiveWorker = page.locator('.worker-card').filter({has:page.getByRole('heading',{name:'Inactive worker',exact:true})});
    await activeWorker.getByRole('heading').waitFor();
    await activeWorker.getByRole('textbox').fill('Continue the task');
    await activeWorker.getByRole('button',{name:'Send',exact:true}).click();
    await page.getByText('Action completed.',{exact:true}).waitFor();
    assert.equal(workerActions[0].session_id,'host/worker');
    assert.equal(workerActions[0].revision,'worker-revision');
    assert.equal(workerActions[0].session,'active-worker');
    assert.equal(workerActions[0].text,'Continue the task');
    assert.ok(workerActions[0].request_id);
    assert.match(workerActions[0].request_id,/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    assert.equal(await inactiveWorker.getByRole('textbox').isDisabled(),true);
    assert.equal(await inactiveWorker.getByRole('button',{name:'Send',exact:true}).isDisabled(),true);
    await inactiveWorker.getByText('No active controllable Claude session. Sending is unavailable.',{exact:true}).waitFor();
    if (width === 390) {
      await page.getByRole('button',{name:'Settings',exact:true}).click();
      await page.getByText('Notifications are unavailable over HTTP. Sessions and controls still work.',{exact:true}).waitFor();
      assert.equal(await page.getByRole('button',{name:'Enable notifications',exact:true}).count(),0);
    }
    console.log(`PASS ${width}px: lifecycle/attention filters, offline versus ended/read-only/history, honest loading/unavailable counts, sanitized Markdown, menu controls, HTTP UUIDs, drafts, guarded worker sends, error/no retry`);
    await context.close();
  }
  assert.deepEqual(failures,[]);
} finally { await browser.close(); }
