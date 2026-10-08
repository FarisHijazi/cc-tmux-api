import { createRequire } from 'node:module';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { auditReadOnly } from './read_only_audit.mjs';
import { testOrigin, auditFleet } from './federation_checks.mjs';

// Dependencies deliberately live outside the repository's package metadata.
const require = createRequire(join(process.env.MOBILE_BROWSER_ENV || '/private/tmp/cc-mobile-browser', 'package.json'));
const { chromium, devices, expect } = require('@playwright/test');
const selfTest = process.argv.includes('--self-test');
const baseURL = testOrigin();
const origin = new URL(baseURL);
if (!selfTest && process.env.MOBILE_READY !== '1') throw new Error('Wait for parent readiness, then set MOBILE_READY=1');
const evidence = resolve(process.env.MOBILE_EVIDENCE_DIR || '/private/tmp/cc-mobile-browser/evidence', new Date().toISOString().replaceAll(':', '-'));
await mkdir(evidence, { recursive: true, mode: 0o700 });
const report = {
  kind: selfTest ? 'harness-self-test-only' : 'real-api-read-only-browser-smoke',
  started: new Date().toISOString(), checks: [],
  pending: ['real toy text send and native echo', 'single-select menu', 'multi-select menu', 'free-text menu', 'host/project grouping and filtering', 'draft retention and reconnect', 'stale revision rejection', 'explicit action errors', 'physical iPhone keyboard'],
};
let browser;
let secret = '';
const redact = text => secret ? String(text).replaceAll(secret, '[REDACTED]') : String(text);
try {
  browser = await chromium.launch({ channel: 'chrome', headless: process.env.MOBILE_HEADED !== '1' });
  for (const [name, options] of [
    ['iphone', { ...devices['iPhone 13'], defaultBrowserType: undefined, viewport: { width: 390, height: 844 } }],
    ['desktop', { viewport: { width: 1440, height: 1000 } }],
  ]) {
    const context = await browser.newContext({ ...options, baseURL });
    const page = await context.newPage();
    let fleet;
    const errors = [];
    page.on('pageerror', error => errors.push(redact(error.message)));
    // No worker endpoints or session writes are allowed by this initial harness.
    await context.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin === origin.origin && url.pathname.startsWith('/api/') && !['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
        errors.push(`Blocked unexpected UI write: ${request.method()} ${url.pathname}`);
        return route.abort('blockedbyclient');
      }
      return route.continue();
    });
    try {
      if (selfTest) {
        await page.setContent('<meta name="viewport" content="width=device-width, initial-scale=1"><main><h1>Harness runtime check</h1><p>This is not app evidence.</p></main>');
        await expect(page.getByRole('heading')).toHaveText('Harness runtime check');
      } else {
        // APIRequestContext shares cookies with this fresh browser context. Never save auth state or traces.
        expect(await context.cookies(), 'Context starts without cookies').toEqual([]);
        const authResponse = await context.request.get('/api/auth');
        if (!authResponse.ok()) throw new Error(`Auth discovery failed with HTTP ${authResponse.status()}`);
        const auth = await authResponse.json();
        expect(typeof auth.required).toBe('boolean');
        if (process.env.MOBILE_EXPECT_NO_PAIRING === '1') expect(auth.required).toBe(false);
        if (auth.required) {
          if (!process.env.MOBILE_KEY_FILE) throw new Error('Pairing is required; set MOBILE_KEY_FILE');
          secret = (await readFile(process.env.MOBILE_KEY_FILE, 'utf8')).trim();
          if (!secret) throw new Error('Pairing key file is empty');
          const login = await context.request.post('/api/auth/login', { data: { token: secret }, headers: { Origin: origin.origin } });
          if (!login.ok()) throw new Error(`Pairing failed with HTTP ${login.status()}`);
        }
        report.checks.push({ name: `${name}: auth discovery`, status: 'passed', required: auth.required, loginSent: auth.required });
        const sessions = await context.request.get('/api/sessions');
        if (!sessions.ok()) throw new Error(`Session discovery failed with HTTP ${sessions.status()}`);
        fleet = await sessions.json();
        if (process.env.MOBILE_ALL_DETAILS === '1') await auditFleet({ context, fleet, report, name });
        report.checks.push({ name: `${name}: real sessions API`, status: 'passed', sessions: fleet.sessions.length, hosts: fleet.hosts.length, connectedHosts: fleet.hosts.filter(h => h.available).length });
        await Promise.all([
          page.waitForResponse(response => new URL(response.url()).pathname === '/api/sessions' && response.request().method() === 'GET' && response.ok()),
          page.goto(process.env.MOBILE_UI_PATH || '/', { waitUntil: 'domcontentloaded' }),
        ]);
        // Parent may supply the actual loaded-view selector once the frontend is ready.
        await page.locator(process.env.MOBILE_READY_SELECTOR || 'main').waitFor({ state: 'visible' });
        await expect(page.getByRole('region', { name: 'Session inbox' })).toBeVisible();
        await expect(page.locator('.session-row')).toHaveCount(fleet.sessions.length);
        await expect(page.locator('.pairing')).toHaveCount(0);
        if (!auth.required) expect(await context.cookies(), 'No pairing cookie needed').toEqual([]);
        report.checks.push({ name: `${name}: live inbox without pairing screen`, status: 'passed' });
        await page.evaluate(() => document.fonts.ready);
      }
      const dimensions = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }));
      await expect(dimensions.scroll, `${name}: horizontal overflow`).toBeLessThanOrEqual(dimensions.width + 1);
      await page.screenshot({ path: join(evidence, `${name}.png`), fullPage: true });
      report.checks.push({ name: `${name}: viewport and no horizontal overflow`, status: 'passed', dimensions, screenshot: `${name}.png` });
      if (name === 'iphone' && !selfTest) {
        await page.setViewportSize({ width: 390, height: 480 });
        await page.screenshot({ path: join(evidence, 'iphone-short-viewport.png'), fullPage: false });
        report.checks.push({ name: 'short viewport screenshot', status: 'captured-only', note: 'Viewport reduction is not a real iOS keyboard test.' });
      }
      if (!selfTest && process.env.MOBILE_AUDIT === '1') {
        await page.setViewportSize(options.viewport);
        await auditReadOnly({ page, context, expect, fleet, name, evidence, report });
      }
      expect(errors, `${name}: browser errors`).toEqual([]);
    } catch (error) {
      await page.screenshot({ path: join(evidence, `${name}-failure.png`), fullPage: true }).catch(() => {});
      report.checks.push({ name, status: 'failed', error: redact(error.message), errors });
      process.exitCode = 1;
    } finally {
      await context.close();
    }
  }
} catch (error) {
  report.checks.push({ name: 'runtime', status: 'failed', error: redact(error.message) });
  process.exitCode = 1;
} finally {
  await browser?.close();
  const passed = label => ['iphone', 'desktop'].every(name => report.checks.some(c => c.name === `${name}: ${label}` && c.status === 'passed'));
  if (passed('host grouping') && passed('directory grouping') && passed('search real session') && passed('attention filter')) {
    report.pending = report.pending.filter(label => label !== 'host/project grouping and filtering');
  }
  report.finished = new Date().toISOString();
  await writeFile(join(evidence, 'report.json'), JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  console.log(JSON.stringify({ kind: report.kind, evidence, failed: Boolean(process.exitCode) }));
}
