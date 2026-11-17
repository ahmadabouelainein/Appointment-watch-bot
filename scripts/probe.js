// Discovery probe: opens the booking page, logs what it sees, walks into option 3.
const { chromium } = require('playwright');
const fs = require('fs');
const URL = process.env.BOOKING_URL;
(async () => {
  fs.mkdirSync('out', { recursive: true });
  const b = await chromium.launch();
  const ctx = await b.newContext({ locale: 'de-DE', timezoneId: 'Europe/Berlin' });
  const p = await ctx.newPage();
  const log = [];
  p.on('request', r => { if (!/\.(css|png|jpe?g|svg|woff2?|ico|gif)(\?|$)/.test(r.url())) log.push(`> ${r.method()} ${r.url()} ${r.postData() ? 'BODY ' + r.postData().slice(0, 500) : ''}`); });
  p.on('response', async r => { const ct = r.headers()['content-type'] || ''; if (ct.includes('json')) { try { log.push(`< ${r.status()} ${r.url()} ${(await r.text()).slice(0, 1500)}`); } catch {} } });
  const dump = async (tag) => {
    console.log(`\n===== ${tag}: ${p.url()} | ${await p.title()}`);
    console.log((await p.innerText('body').catch(() => '')).slice(0, 4000));
    const els = await p.$$eval('button, a, input, select, [role=button], label', es => es.map(e => `${e.tagName} id=${e.id} class=${e.className} name=${e.getAttribute('name')} type=${e.getAttribute('type')} text=${(e.innerText || e.value || '').trim().slice(0, 80)}`));
    console.log('--- controls ---\n' + els.join('\n'));
    await p.screenshot({ path: `out/${tag}.png`, fullPage: true });
    fs.writeFileSync(`out/${tag}.html`, await p.content());
  };
  const resp = await p.goto(URL, { waitUntil: 'networkidle', timeout: 60000 });
  console.log('HTTP status', resp && resp.status());
  await p.waitForTimeout(3000);
  await dump('1-landing');
  console.log('\n===== NETWORK\n' + log.join('\n'));
  fs.writeFileSync('out/network.log', log.join('\n'));
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
