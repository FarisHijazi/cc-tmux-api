import { join } from 'node:path';

// Exercise the actual frontend with real discovered sessions. No API response fixtures.
export async function auditReadOnly({ page, context, expect, fleet, name, evidence, report }) {
  const sessions = fleet.sessions;
  const check = async (label, run) => {
    try {
      await run();
      report.checks.push({ name: `${name}: ${label}`, status: 'passed' });
    } catch (error) {
      report.checks.push({ name: `${name}: ${label}`, status: 'failed', error: error.message });
      await page.screenshot({ path: join(evidence, `${name}-${label.replaceAll(/[^a-z0-9]/gi, '-')}-failure.png`) });
      process.exitCode = 1;
    }
  };
  const screenshot = async label => {
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.screenshot({ path: join(evidence, `${name}-${label}.png`), scale: 'css' });
  };
  const rows = page.locator('.session-row');
  const toy = sessions.find(s => s.host === 'work-a' && s.pane === '%37' && s.session === 'cc-mobile-browser-toy-20261008' && s.workdir === '/tmp/cc-mobile-browser-toy-c620be5c-5c58-4ad6-a039-97fe1673fa6f');
  report.checks.push({ name: `${name}: fleet discovery`, status: 'observed', count: sessions.length,
    hosts: fleet.hosts.map(h => ({ id: h.id, available: h.available })), reservedToyDiscovered: Boolean(toy) });
  await check('live session count', () => expect(rows).toHaveCount(sessions.length));
  await check('host grouping', async () => {
    await page.getByRole('button', { name: 'Host', exact: true }).click();
    await expect(page.locator('.session-group')).toHaveCount(new Set(sessions.map(s => s.host_label || s.host)).size);
  });
  await check('directory grouping', async () => {
    await page.getByRole('button', { name: 'Directory', exact: true }).click();
    await expect(page.locator('.session-group')).toHaveCount(new Set(sessions.map(s => s.workdir || 'Directory unavailable')).size);
    await screenshot('directory-groups');
  });
  const search = page.getByRole('searchbox', { name: 'Search sessions' });
  await check('search no results and recovery', async () => {
    await search.fill('cc-mobile-audit-no-match-7c6dfb');
    await expect(rows).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'No matching sessions' })).toBeVisible();
    await search.fill('');
    await expect(rows).toHaveCount(sessions.length);
  });
  const chosen = toy || sessions[0];
  if (!chosen) throw new Error('No real sessions available for detail audit');
  await check('search real session', async () => {
    const query = chosen.title || chosen.session;
    await search.fill(query);
    const matches = sessions.filter(s => [s.title, s.host_label, s.workdir, s.session].join(' ').toLowerCase().includes(query.toLowerCase()));
    await expect(rows).toHaveCount(matches.length);
    await screenshot('search');
    await search.fill('');
  });
  await check('attention filter', async () => {
    await page.getByRole('button', { name: /^Needs you/ }).click();
    await expect(rows).toHaveCount(sessions.filter(s => !!s.menu || /waiting|question|menu|attention|blocked|error|failed|permission|accident/i.test([s.status, s.status_label].filter(Boolean).join(' '))).length);
    await screenshot('attention');
    await page.getByRole('button', { name: 'All sessions', exact: true }).click();
  });
  await page.getByRole('button', { name: 'All sessions', exact: true }).click();
  await page.getByRole('button', { name: 'Host', exact: true }).click();
  // Live titles change; stable API identity owns navigation.
  await page.evaluate(id => { location.hash = `session=${encodeURIComponent(id)}`; }, chosen.id);
  await expect(page.getByRole('region', { name: 'Session detail', exact: true })).toBeVisible();
  await expect(page.locator('.detail .loading')).toHaveCount(0);
  await screenshot('conversation');
  await check('detail horizontal overflow', async () => {
    const dimensions = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }));
    expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width + 1);
  });
  await check('terminal tab', async () => {
    await page.getByRole('tab', { name: 'Terminal', exact: true }).click();
    await expect(page.locator('.detail .terminal-output')).toBeVisible();
    await expect(page.locator('.detail .terminal-output')).not.toHaveText('No terminal output is available yet.');
    await screenshot('terminal');
    await page.getByRole('tab', { name: 'Conversation', exact: true }).click();
  });
  if (toy) {
    await check('toy local draft reload', async () => {
      const composer = page.getByRole('textbox', { name: 'Message this session' });
      await composer.fill('Unsent browser audit draft');
      await page.reload();
      await expect(composer).toHaveValue('Unsent browser audit draft');
      await expect(page.getByRole('button', { name: 'Refresh session', exact: true })).toBeEnabled();
      await composer.fill('');
    });
  }
  if (name === 'iphone') {
    await check('short viewport composer bounds', async () => {
      await page.setViewportSize({ width: 390, height: 480 });
      await page.getByRole('textbox', { name: 'Message this session' }).focus();
      await page.waitForTimeout(300);
      await expect(async () => {
        const box = await page.getByRole('button', { name: 'Send message' }).boundingBox();
        expect(box).not.toBeNull();
        expect(box.y).toBeGreaterThanOrEqual(0);
        expect(box.y + box.height).toBeLessThanOrEqual(480);
      }).toPass({ timeout: 5000 });
      const geometry = await page.evaluate(() => {
        const button = document.querySelector('.send-button');
        const rect = button.getBoundingClientRect();
        const visible = visualViewport;
        return { viewport: { width: visible.width, height: visible.height, top: visible.offsetTop, left: visible.offsetLeft },
          button: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
          hit: button.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)) };
      });
      expect(geometry.hit).toBe(true);
      report.checks.push({ name: 'iphone: short viewport geometry', status: 'observed', geometry });
      await screenshot('short-composer');
      await page.setViewportSize({ width: 390, height: 844 });
    });
  }
  await check('offline banner and recovery', async () => {
    try {
      await context.setOffline(true);
      await expect(page.locator('.offline-banner')).toBeVisible();
      await screenshot('offline');
    } finally {
      await context.setOffline(false);
    }
    await expect(page.locator('.offline-banner')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Refresh session', exact: true })).toBeEnabled();
    await expect(page.locator('.detail [role="alert"]')).toHaveCount(0);
  });
  await page.emulateMedia({ colorScheme: 'dark' });
  await screenshot('dark-detail');
}
