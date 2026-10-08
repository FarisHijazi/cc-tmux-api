import { join } from 'node:path';

export async function auditMenus({ page, expect, detail, actionPath, verifyNative, nativeTail, evidence, report }) {
  const shot = async label => {
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'No horizontal menu overflow').toBe(true);
    await page.screenshot({ path: join(evidence, `${label}.png`), scale: 'css' });
  };
  const refresh = async () => {
    const button = page.getByRole('button', { name: 'Refresh session', exact: true });
    await expect(button).toBeEnabled();
    await button.click();
    await expect(button).toBeEnabled();
  };
  const action = async (locator, label) => {
    verifyNative();
    const [result] = await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname === actionPath && r.request().method() === 'POST'),
      locator.click(),
    ]);
    if (!result.ok()) {
      const body = await result.json();
      await shot(`${label}-error`);
      throw new Error(`${label}: HTTP ${result.status()} ${JSON.stringify(body)}; not retried`);
    }
    await expect(page.getByRole('button', { name: 'Refresh session', exact: true })).toBeEnabled();
    report.checks.push({ name: label, status: 'passed' });
  };
  const menu = async question => {
    await expect.poll(async () => (await detail()).menu?.question || '', { timeout: 45000 }).toContain(question);
    await refresh();
    await expect(page.getByRole('region', { name: 'Pending question' })).toBeVisible();
    await shot(`menu-${question}`);
  };
  const resuming = process.env.MOBILE_RESUME_MENU === '1';
  const marker = resuming ? 'BROWSER_MENU_RESULT_1791479390303' : `BROWSER_MENU_RESULT_${Date.now()}`;
  const prompt = `Call AskUserQuestion exactly once containing these three questions in order: (1) header Color, question "Which browser color?", multiSelect false, options Red (description Warm), Green (description Cool). (2) header Gear, question "Which browser gear?", multiSelect true, options Map (description Navigation), Lamp (description Light), Rope (description Climbing). (3) header Snack, question "Which browser snack?", multiSelect false, options Apple (description Fruit), Biscuit (description Baked). Wait for all answers, then print ${marker} followed by the exact received answers. Do not call any other tool.`;
  if (!resuming) {
  await refresh();
  await page.getByRole('textbox', { name: 'Message this session' }).fill(prompt);
  await action(page.getByRole('button', { name: 'Send message' }), 'real menu prompt sent');
  await menu('Which browser color?');
  await action(page.locator('.question-option').filter({ has: page.locator('strong', { hasText: /^Green$/ }) }), 'single-select Green');
  await menu('Which browser gear?');
  await action(page.locator('.question-option').filter({ has: page.locator('strong', { hasText: /^Map$/ }) }), 'multi-select Map');
  await refresh();
  await action(page.locator('.question-option').filter({ has: page.locator('strong', { hasText: /^Rope$/ }) }), 'multi-select Rope');
  await refresh();
  await expect(page.locator('.question-option.chosen')).toHaveCount(2);
  await shot('menu-multi-selected');
  await page.getByRole('button', { name: 'Continue', exact: true }).scrollIntoViewIfNeeded();
  await shot('menu-continue');
  await action(page.getByRole('button', { name: 'Continue', exact: true }), 'Continue multiselect without changing answers');
  } else {
    await menu('Which browser gear?');
    const current = await detail();
    if (!current.menu.options.find(o => o.label === 'Rope')?.selected) {
      await action(page.locator('.question-option').filter({ has: page.locator('strong', { hasText: /^Rope$/ }) }), 'restore Rope after Enter toggle');
    }
    await refresh();
    await page.locator('.controls summary').click();
    await action(page.getByRole('button', { name: 'Right', exact: true }), 'advance batch via native Right control');
  }
  await menu('Which browser snack?');
  for (let step = 0; step < 5 && !await page.getByRole('textbox', { name: 'Your answer', exact: true }).isVisible(); step++) {
    await action(page.locator('.question-option').filter({ has: page.locator('strong', { hasText: /Type something|Other|Custom/i }) }), 'focus custom option');
    await refresh();
  }
  await page.getByRole('textbox', { name: 'Your answer', exact: true }).fill('Oat biscuit, lightly salted');
  await action(page.getByRole('button', { name: 'Insert answer', exact: true }), 'insert custom answer');
  await shot('menu-custom-inserted');
  await action(page.getByRole('button', { name: 'Confirm typed answer', exact: true }), 'confirm custom answer');
  await expect.poll(async () => (await detail()).menu?.kind, { timeout: 15000 }).toBe('review');
  await refresh();
  for (const answer of ['Green', 'Map, Rope', 'Oat biscuit, lightly salted']) await expect(page.locator('.review-summary')).toContainText(answer);
  report.checks.push({ name: 'review exposes all three actual answers', status: 'passed' });
  await shot('menu-review');
  await action(page.getByRole('button', { name: 'Submit answer', exact: true }), 'submit batch review');
  await expect.poll(() => nativeTail(), { timeout: 45000 }).toMatch(new RegExp(`^[●⏺]\\s+${marker}`, 'm'));
  const tail = nativeTail();
  const latestResult = tail.slice(tail.lastIndexOf(marker));
  expect(latestResult).toContain('Green'); expect(latestResult).toContain('Map'); expect(latestResult).toContain('Rope'); expect(latestResult).toContain('Oat biscuit, lightly salted');
  report.checks.push({ name: 'real model result and native answers verified', status: 'passed', marker });
  report.checks.push({ name: 'API conversation mapping', status: 'observed', messages: (await detail()).messages.length, note: 'An unmapped safe-mode UUID may correctly fall back to terminal; no inferred mapping.' });
  report.pending = report.pending.filter(x => !['single-select', 'multi-select', 'free-text'].includes(x));
  await refresh();
  await shot('menu-completed-conversation');
}
