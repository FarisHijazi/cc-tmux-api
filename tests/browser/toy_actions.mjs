import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { auditMenus } from './remote_menus.mjs';
import { testOrigin, decodeRoute } from './federation_checks.mjs';

const require = createRequire(join(process.env.MOBILE_BROWSER_ENV || '/private/tmp/cc-mobile-browser', 'package.json'));
const { chromium, devices, expect } = require('@playwright/test');
if (process.env.MOBILE_TOY_ACTIONS !== '1' || process.env.MOBILE_READY !== '1') throw new Error('Requires parent readiness and explicit toy-action gate');
const origin = testOrigin();
const remote = process.env.MOBILE_REMOTE_TOY === '1';
const freshBatchOnly = process.env.MOBILE_FRESH_BATCH_ONLY === '1';
if (process.env.MOBILE_MENU_VARIANT === 'current-review' && (!freshBatchOnly || origin === 'http://127.0.0.1:8391')) {
  throw new Error('Current-review retest requires one fresh batch through root, not the stopped relay');
}
if (freshBatchOnly && (!remote || process.env.MOBILE_MENU_TESTS !== '1' ||
    ['MOBILE_RESUME_MENU', 'MOBILE_VERIFY_COMPLETED', 'MOBILE_CONTROLS_ONLY'].some(k => process.env[k] === '1'))) {
  throw new Error('Fresh batch requires remote menu mode with no resume, verification-only, or control-only mode');
}
const target = remote ? 'cc-mobile-browser-toy-20261008:0.0' : 'cc-mobile-research-toy:0.0';
const expected = remote
  ? ['cc-mobile-browser-toy-20261008', '%37', '/tmp/cc-mobile-browser-toy-c620be5c-5c58-4ad6-a039-97fe1673fa6f', 'claude', '2265987']
  : ['cc-mobile-research-toy', '%121', '/private/tmp/cc-mobile-research', '2.1.294', '6697'];
// The remote toy's ssh alias (and the host name HQ reports for it).
const remoteHost = process.env.CC_REMOTE_HOST ?? 'work-a';
const expectedHost = remote ? remoteHost : 'local';
const quote = value => "'" + value.replaceAll("'", "'\\''") + "'";
const tmuxRead = args => remote
  ? execFileSync('ssh', [remoteHost, ['tmux', ...args].map(quote).join(' ')], { encoding: 'utf8' })
  : execFileSync('tmux', args, { encoding: 'utf8' });
let routedIdentity;
function verifyNative() {
  const actual = tmuxRead(['display-message', '-p', '-t', target,
    '#{session_name}|#{pane_id}|#{pane_current_path}|#{pane_current_command}|#{pane_pid}']).trim().split('|');
  expect(actual, 'Reserved toy native identity').toEqual(expected);
  if (routedIdentity) {
    expect(tmuxRead(['display-message', '-p', '#{pid}|#{start_time}|#{socket_path}']).trim(), 'Native tmux generation').toBe(routedIdentity[0]);
    expect(tmuxRead(['display-message', '-p', '-t', target, '#{session_created}']).trim(), 'Native session generation').toBe(String(routedIdentity[3]));
    const args = ['ps', '-p', String(routedIdentity[4]), '-o', 'lstart='];
    const birth = remote ? execFileSync('ssh', [remoteHost, args.map(quote).join(' ')], { encoding: 'utf8' })
      : execFileSync(args[0], args.slice(1), { encoding: 'utf8' });
    expect(birth.trim().replace(/\s+/g, ' '), 'Native Claude process birth').toBe(routedIdentity[5]);
  }
}
function nativeTail() {
  verifyNative();
  return tmuxRead(['capture-pane', '-p', '-t', target, '-S', '-70']);
}
const evidence = join('/private/tmp/cc-mobile-browser/evidence', `toy-${new Date().toISOString().replaceAll(':', '-')}`);
await mkdir(evidence, { recursive: true, mode: 0o700 });
const report = { kind: 'real-owned-toy-actions', checks: [], pending: ['single-select', 'multi-select', 'free-text', 'physical iPhone keyboard'] };
let browser;
let key = '';
try {
  verifyNative();
  browser = await chromium.launch({ channel: 'chrome' });
  const context = await browser.newContext({ ...devices['iPhone 13'], baseURL: origin });
  const authResponse = await context.request.get('/api/auth');
  expect(authResponse.ok()).toBe(true);
  const auth = await authResponse.json();
  expect(typeof auth.required).toBe('boolean');
  if (process.env.MOBILE_EXPECT_NO_PAIRING === '1') expect(auth.required).toBe(false);
  if (auth.required) {
    if (!process.env.MOBILE_KEY_FILE) throw new Error('Pairing required but no key file supplied');
    key = (await readFile(process.env.MOBILE_KEY_FILE, 'utf8')).trim();
    const login = await context.request.post('/api/auth/login', { data: { token: key }, headers: { Origin: origin } });
    expect(login.ok(), 'Pairing succeeds').toBe(true);
  }
  report.checks.push({ name: 'auth discovery', status: 'passed', required: auth.required, loginSent: auth.required });
  const fleetResponse = await context.request.get('/api/sessions');
  expect(fleetResponse.ok()).toBe(true);
  const fleet = await fleetResponse.json();
  report.identity = { host: expectedHost, session: expected[0], pane: expected[1], cwd: expected[2], pid: expected[4] };
  const matches = fleet.sessions.filter(s => s.session === expected[0] && s.pane === expected[1] && s.workdir === expected[2]);
  if (matches.length !== 1) {
    report.blocked = 'Reserved toy not uniquely discovered; no action sent';
    process.exitCode = 2;
  } else {
    const toy = matches[0];
    const { hops, identity, ownerId } = decodeRoute(toy.id);
    expect(ownerId).toBe(toy.owner_session_id);
    expect(hops).toContain(expectedHost);
    expect(hops.length - 1).toBeGreaterThanOrEqual(Number(process.env.MOBILE_MIN_PEER_HOPS || 0));
    report.route = { origin, hops, hostPath: toy.host_path, ownerNode: toy.owner_node_id, ownerSession: ownerId, originId: toy.origin_id };
    expect(identity[1]).toBe(expected[1]);
    expect(String(identity[2])).toBe(expected[4]);
    routedIdentity = identity;
    verifyNative();
    const detailPath = `/api/sessions/${encodeURIComponent(toy.id)}`;
    const actionPath = `${detailPath}/action`;
    const detail = async () => {
      const response = await context.request.get(detailPath);
      expect(response.ok()).toBe(true);
      const data = await response.json();
      expect(data.session).toBe(expected[0]); expect(data.pane).toBe(expected[1]); expect(data.workdir).toBe(expected[2]);
      expect(data.id).toBe(toy.id);
      return data;
    };
    const initial = await detail();
    if (process.env.MOBILE_RESUME_MENU !== '1') expect(initial.menu, 'Toy starts without an open menu').toBeFalsy();
    let staleRevision;
    let sent = 0;
    let prompts = 0;
    await context.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
        if (url.origin !== origin || url.pathname !== actionPath || request.method() !== 'POST') return route.abort('blockedbyclient');
        verifyNative();
        const payload = request.postDataJSON();
        if (freshBatchOnly && payload.action === 'send' && ++prompts > 1) return route.abort('blockedbyclient');
        const menuAction = remote && process.env.MOBILE_MENU_TESTS === '1' && ['select', 'type', 'submit', 'key'].includes(payload.action);
        if (payload.action !== 'send' && !menuAction && !(process.env.MOBILE_CONTROLS_ONLY === '1' && payload.action === 'key' && ['Left', 'Right'].includes(payload.key))) return route.abort('blockedbyclient');
        sent++;
        if (staleRevision) return route.continue({ postData: JSON.stringify({ ...payload, revision: staleRevision }) });
      }
      await route.continue();
    });
    const page = await context.newPage();
    const pageErrors = [];
    page.on('pageerror', error => pageErrors.push(error.message));
    await page.goto(`/#session=${encodeURIComponent(toy.id)}`);
    const composer = page.getByRole('textbox', { name: 'Message this session' });
    await expect(page.locator('.detail .loading')).toHaveCount(0);
    await expect(page.locator('.detail-title h2')).toHaveText(toy.title || toy.session);
    if (process.env.MOBILE_RESUME_MENU !== '1' && process.env.MOBILE_VERIFY_COMPLETED !== '1' && process.env.MOBILE_MENU_VARIANT !== 'current-review') {
    const draft = 'Unsent owned toy browser draft';
    await composer.fill(draft);
    await page.reload();
    await expect(composer).toHaveValue(draft);
    await expect(page.locator('.detail-title h2')).toHaveText(toy.title || toy.session);
    await expect(page.getByRole('button', { name: 'Refresh session', exact: true })).toBeEnabled();
    await context.setOffline(true);
    await expect(page.locator('.offline-banner')).toBeVisible();
    await expect(composer).toHaveValue(draft);
    await context.setOffline(false);
    await expect(page.locator('.offline-banner')).toHaveCount(0);
    await expect(page.locator('.detail [role="alert"]')).toHaveCount(0);
    await composer.fill('');
    report.checks.push({ name: 'toy draft survives reload and offline return', status: 'passed' });
    }
    if (remote && process.env.MOBILE_VERIFY_COMPLETED === '1') {
      const tail = nativeTail();
      expect(tail).toMatch(/^[●⏺]\s+BROWSER_MENU_RESULT_1791479390303/m);
      for (const answer of ['Which browser color? → Green', 'Which browser gear? → Map, Rope', 'Which browser snack? → Oat biscuit, lightly salted']) expect(tail).toContain(answer);
      report.checks.push({ name: 'native model result confirms real single/multi/custom browser answers', status: 'passed', precedingEvidence: 'toy-2026-10-08T17-14-00.239Z' });
      const snapshot = await detail();
      report.checks.push({ name: 'remote conversation mapping', status: snapshot.messages.length ? 'observed' : 'failed', messages: snapshot.messages.length });
      report.pending = ['physical iPhone keyboard', 'dedicated multi advance button fix', 'review answer summary fix'];
      await page.screenshot({ path: join(evidence, 'completed-conversation.png'), scale: 'css' });
    } else if (remote && process.env.MOBILE_MENU_TESTS === '1') {
      await auditMenus({ page, expect, detail, actionPath, verifyNative, nativeTail, evidence, report });
    } else if (process.env.MOBILE_CONTROLS_ONLY === '1') {
      await page.getByRole('tab', { name: 'Terminal', exact: true }).click();
      const beforeControls = (await detail()).messages;
      for (const keyName of ['Left', 'Right']) {
        await page.getByRole('button', { name: 'Refresh session', exact: true }).click();
        await expect(page.getByRole('button', { name: 'Refresh session', exact: true })).toBeEnabled();
        verifyNative();
        const response = page.waitForResponse(r => new URL(r.url()).pathname === actionPath && r.request().method() === 'POST');
        await page.getByRole('button', { name: keyName, exact: true }).click();
        const controlResponse = await response;
        const controlBody = await controlResponse.json();
        report.checks.push({ name: `${keyName} API response`, status: controlResponse.ok() ? 'passed' : 'failed', http: controlResponse.status(), body: controlBody });
        expect(controlResponse.status(), JSON.stringify(controlBody)).toBe(200);
        await expect(page.locator('.detail [role="alert"]')).toHaveCount(0);
        report.checks.push({ name: `real UI ${keyName} control accepted`, status: 'passed' });
      }
      expect((await detail()).messages).toEqual(beforeControls);
      report.checks.push({ name: 'cursor-only controls added no conversation messages', status: 'passed' });
      if (!remote) report.blocked = 'Native toy: weekly usage limit; resets Oct 11 at 10am (Europe/Prague). No new model prompts sent.';
    } else {
    const marker = `HQ_BROWSER_ECHO_${Date.now()}`;
    const prompt = `Reply exactly ${marker}. Do not use tools.`;
    const before = await detail();
    verifyNative();
    await composer.fill(prompt);
    const action = page.waitForResponse(r => new URL(r.url()).pathname === actionPath && r.request().method() === 'POST');
    await page.getByRole('button', { name: 'Send message' }).click();
    const actionResponse = await action;
    const actionBody = await actionResponse.json();
    report.checks.push({ name: 'UI text action response', status: actionResponse.ok() ? 'passed' : 'failed', http: actionResponse.status(), body: actionBody });
    await page.screenshot({ path: join(evidence, 'toy-after-send.png'), scale: 'css' });
    if (!actionResponse.ok()) throw new Error(`Toy send rejected: HTTP ${actionResponse.status()} ${JSON.stringify(actionBody)}; not retried`);
    await expect(composer).toHaveValue('');
    await expect.poll(() => nativeTail(), { timeout: 15000 }).toContain(prompt);
    report.checks.push({ name: 'sent prompt verified in native toy pane', status: 'passed', marker });
    // A stale UI submission exercises the real API error path, not a fake response.
    await expect.poll(async () => (await detail()).revision, { timeout: 15000 }).not.toBe(before.revision);
    staleRevision = before.revision;
    const rejectedText = `MUST_NOT_REACH_TOY_${Date.now()}`;
    await composer.fill(rejectedText);
    const staleResponse = page.waitForResponse(r => new URL(r.url()).pathname === actionPath && r.request().method() === 'POST');
    await page.getByRole('button', { name: 'Send message' }).click();
    expect((await staleResponse).status()).toBe(409);
    await expect(page.locator('.detail [role="alert"]')).toContainText('Screen changed');
    await expect(composer).toHaveValue(rejectedText);
    expect(nativeTail()).not.toContain(rejectedText);
    await page.screenshot({ path: join(evidence, 'toy-stale-error.png'), scale: 'css' });
    await composer.fill('');
    report.checks.push({ name: 'real stale revision 409, visible error, retained draft, no native input', status: 'passed' });
    const until = Date.now() + 45000;
    let result;
    while (Date.now() < until) {
      result = await detail();
      const tail = nativeTail();
      if (/hit your (?:weekly |daily |session )?limit|out of extra usage|usage limit|quota exceeded|limit reached/i.test(tail.split('\n').slice(-25).join('\n'))) {
        report.blocked = 'Native toy reports a usage limit; menu prompts were not sent';
        break;
      }
      if (result.messages?.some(m => m.role === 'assistant' && m.text.trim() === marker) || tail.includes(`⏺ ${marker}`)) {
        report.checks.push({ name: 'real Claude model echo', status: 'passed' });
        break;
      }
      await page.waitForTimeout(1000);
    }
    if (!report.blocked && !report.checks.some(c => c.name === 'real Claude model echo')) report.blocked = 'Model echo not verified within 45 seconds; no automatic resend';
    }
    await writeFile(join(evidence, 'native-toy-tail.txt'), nativeTail(), { mode: 0o600 });
    await page.getByRole('tab', { name: 'Terminal', exact: true }).click();
    await page.screenshot({ path: join(evidence, 'toy-terminal.png'), scale: 'css' });
    report.requests = sent;
    if (freshBatchOnly) expect(prompts, 'Exactly one fresh model batch prompt').toBe(1);
    report.pageErrors = pageErrors;
    expect(pageErrors).toEqual([]);
    await context.close();
  }
} catch (error) {
  report.error = key ? error.message.replaceAll(key, '[REDACTED]') : error.message;
  process.exitCode = 1;
} finally {
  await browser?.close();
  await writeFile(join(evidence, 'report.json'), JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  console.log(JSON.stringify({ evidence, blocked: report.blocked, error: report.error }));
}
