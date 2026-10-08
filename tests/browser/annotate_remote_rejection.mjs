import { readFile, writeFile } from 'node:fs/promises';
const path = '/private/tmp/cc-mobile-browser/evidence/toy-2026-10-08T17-04-41.009Z/report.json';
const report = JSON.parse(await readFile(path, 'utf8'));
report.diagnosis = {
  source: 'Actual UI screenshot toy-after-send.png and independent native pane capture; original runner did not record response body',
  visibleApiError: 'Text requires a verified empty Claude prompt or focused free-text option',
  nativeState: 'Normal empty input prompt showing gray built-in suggestion; no trust dialog; no sent text arrived',
  identity: { host: 'work-a', session: 'cc-mobile-browser-toy-20261008', pane: '%37', pid: 2265987,
    cwd: '/tmp/cc-mobile-browser-toy-c620be5c-5c58-4ad6-a039-97fe1673fa6f', command: 'claude' },
};
await writeFile(path, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
