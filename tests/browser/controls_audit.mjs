import { createRequire } from 'node:module';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { testOrigin } from './federation_checks.mjs';
if (process.env.MOBILE_READY !== '1') throw new Error('Requires parent readiness');
const require = createRequire(join(process.env.MOBILE_BROWSER_ENV || '/private/tmp/cc-mobile-browser', 'package.json'));
const { chromium, devices, expect } = require('@playwright/test');
const evidence = `/private/tmp/cc-mobile-browser/evidence/controls-${new Date().toISOString().replaceAll(':', '-')}`;
await mkdir(evidence, {recursive:true, mode:0o700});
const report = {kind:'read-only-control-availability', checks:[], errors:[]};
const browser = await chromium.launch({channel:'chrome'});
try {
  for (const [name, options] of [['iphone',{...devices['iPhone 13'],viewport:{width:390,height:844}}],['desktop',{viewport:{width:1440,height:1000}}]]) {
    const context=await browser.newContext({...options,baseURL:testOrigin()});
    try {
      await context.route('**/*',route=> ['GET','HEAD','OPTIONS'].includes(route.request().method()) ? route.continue() : route.abort('blockedbyclient'));
      const auth=await (await context.request.get('/api/auth')).json();
      expect(auth.required).toBe(false);
      const fleet=await (await context.request.get('/api/sessions')).json();
      const page=await context.newPage();
      page.on('pageerror',e=>report.errors.push(e.message));
      await page.goto('/');
      await expect(page.locator('.session-row')).toHaveCount(fleet.sessions.length);
      await expect(page.locator('.inbox-footer')).toContainText('4 of 4 hosts connected');
      await page.screenshot({path:join(evidence,`${name}-footer.png`),scale:'css'});
      for (const row of fleet.sessions) {
        await page.goto(`/#session=${encodeURIComponent(row.id)}`);
        await expect(page.locator('.detail-title h2')).toHaveText(row.title || row.session);
        await expect(page.locator('.detail .loading')).toHaveCount(0);
        await expect(page.locator('.lifecycle-notice')).toHaveCount(0);
        await page.getByRole('tab',{name:'Terminal',exact:true}).click();
        for (const key of ['Up','Down','Left','Right','Space','Enter','Escape','Tab','Interrupt']) {
          await expect(page.getByRole('button',{name:key,exact:true})).toBeEnabled();
        }
        expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
        report.checks.push({viewport:name,session:row.session,host:row.host,status:'passed',enabledControls:9});
        if (row.session==='ansible-proxmox-1') {
          await page.getByRole('tab',{name:'Conversation',exact:true}).click();
          await expect(page.locator('.message')).toHaveCount(19);
          await page.screenshot({path:join(evidence,`${name}-ansible-19-messages.png`),scale:'css'});
        }
      }
    } finally {await context.close();}
  }
  expect(report.errors).toEqual([]);
} catch(error) {report.error=error.message;process.exitCode=1;}
finally {
  await browser.close();
  await writeFile(join(evidence,'report.json'),JSON.stringify(report,null,2)+'\n',{mode:0o600});
  console.log(JSON.stringify({evidence,error:report.error,checks:report.checks.length}));
}
