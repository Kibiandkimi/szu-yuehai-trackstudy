// 浏览器验收：页面无报错、JS 规划与 Python OD 一致、各标签页可渲染、KML 拖入预览可用。
// 运行：NODE_PATH=/home/kibi/Work/ChineseDungeonSplit4/node_modules node tests/browser_check.cjs [docs/index.html]
const path = require('path'), fs = require('fs');
const { chromium } = require('playwright-core');
const arg = process.argv[2] || path.join(__dirname, '..', 'site', 'index.html');
const url = /^https?:/.test(arg) ? arg : 'file://' + path.resolve(arg);
const shots = path.join('/tmp', 'trackstudy-shots'); fs.mkdirSync(shots, { recursive: true });
(async () => {
  const exe = fs.readdirSync(path.join(process.env.HOME, '.cache/ms-playwright')).filter(d => d.startsWith('chromium-')).sort().pop();
  const browser = await chromium.launch({ executablePath: path.join(process.env.HOME, '.cache/ms-playwright', exe, 'chrome-linux64/chrome') });
  const fails = [];
  for (const [w, h, tag] of [[1400, 900, 'desktop'], [390, 844, 'mobile']]) {
    const page = await browser.newPage({ viewport: { width: w, height: h } });
    const errors = [];
    page.on('pageerror', e => errors.push(String(e))); page.on('console', m => m.type() === 'error' && errors.push(m.text()));
    page.on('request', r => { if (!r.url().startsWith('file:') && !r.url().startsWith('data:') && !r.url().startsWith(new URL(url).origin)) errors.push('外部请求 ' + r.url()); });
    await page.goto(url);
    await page.waitForFunction(() => document.getElementById('map-status').textContent.includes('就绪'), null, { timeout: 15000 });
    if (tag === 'desktop') {
      // JS 与 Python 规划一致（OD 表由 Python 计算）
      const diff = await page.evaluate(() => {
        const { placeRoute, D } = window.__trackstudy, out = [];
        const names = Object.keys(D.places);
        for (const r of D.od) {
          if (!['斋区', '汇文楼', '致理楼', '乔区'].includes(r.origin)) continue;
          const js = placeRoute(r.origin, r.dest, `${r.mode}_${r.period}`);
          out.push(js ? Math.abs(js.t / 60 - r.time_min) / r.time_min : 1);
        }
        return { n: out.length, custom: D.od.filter(r => r.custom).length, max: Math.max(...out), bad: out.filter(x => x > 0.01).length };
      });
      console.log('JS/Python OD 对比', diff);
      if (diff.bad) fails.push(`JS 与 Python 规划不一致：${diff.bad}/${diff.n}，最大 ${diff.max}`);
      await page.screenshot({ path: `${shots}/route.png` });
      const rule = await page.evaluate(() => {
        const T = window.__trackstudy, { D } = T;
        const peakFaster = D.od.filter(r => r.period === 'peak' && r.time_min < D.od.find(q => q.origin === r.origin && q.dest === r.dest && q.mode === r.mode && q.period === 'off').time_min - 0.01).length;
        const before = T.placeRoute('致理楼', '计软楼', 'bike_off'); const [e, d] = before.arcs[Math.floor(before.arcs.length / 2)];
        T.setRule(e, 'bike', [0, 1], 'block');
        const after = T.placeRoute('致理楼', '计软楼', 'bike_off');
        const avoided = !after || !after.arcs.some(a => a[0] === e);
        T.setRule(e, 'bike', [0, 1], 'none');
        return { peakFaster, avoided, before: before.t, after: after && after.t, dirty: T.RULES().edges.length };
      });
      console.log('规则/高峰检查', rule);
      if (rule.peakFaster) fails.push(`有 ${rule.peakFaster} 组 OD 高峰比平峰快`);
      if (!rule.avoided || rule.after < rule.before) fails.push('封禁路段后路线没有绕开');
      await page.evaluate(() => { document.querySelector('[name=tool][value=rules]').click(); });
      // 真实点击：学生西路上一段 → 弹窗 → 禁行 → 出现保存栏 → 放弃
      const pt = await page.evaluate(() => { const T = window.__trackstudy, e = T.EDGES.find(x => x.name === '学生西路' && x.len > 60);
        const mid = e.ll[Math.floor(e.ll.length / 2)], ll = e.ll.length > 2 ? mid : [(e.ll[0][0] + e.ll[1][0]) / 2, (e.ll[0][1] + e.ll[1][1]) / 2];
        T.map.setView(ll, 18, {animate: false}); const p = T.map.latLngToContainerPoint(ll); const r = T.map.getContainer().getBoundingClientRect(); return [p.x + r.left, p.y + r.top]; });
      await page.waitForTimeout(300);
      await page.mouse.click(pt[0], pt[1]);
      await page.waitForSelector('.leaflet-popup [data-set=block]', { timeout: 3000 }).catch(() => fails.push('规则弹窗未出现'));
      if (await page.$('.leaflet-popup [data-set=block]')) {
        await page.screenshot({ path: `${shots}/rule-popup.png` });
        await page.click('.leaflet-popup [data-set=block][data-dirs="0,1"]');
        if (!(await page.isVisible('#savebar'))) fails.push('封禁后保存栏未出现');
        await page.screenshot({ path: `${shots}/rules.png` });
        await page.click('#rdiscard');
        if (await page.isVisible('#savebar')) fails.push('放弃修改后保存栏仍在');
      }
      await page.evaluate(() => { document.querySelector('[name=tool][value=route]').click(); });
      for (const t of ['teach', 'dorm', 'od', 'iso', 'model', 'kml', 'about']) {
        await page.click(`[data-tab=${t}]`);
        const txt = await page.textContent('#dbody');
        if (!txt || txt.length < 50) fails.push(`标签页 ${t} 内容为空`);
        if (t === 'teach' || t === 'model') await page.screenshot({ path: `${shots}/tab-${t}.png` });
      }
      await page.click('[data-tab=model]');
      await page.hover('.chart[data-md=bike] .hit', { position: { x: 200, y: 60 } });
      if (!(await page.isVisible('.chart[data-md=bike] .tt'))) fails.push('坡度图提示未显示');
      await page.screenshot({ path: `${shots}/tab-model-hover.png` });
      // 可达圈：到达/出发
      await page.check('[name=tool][value=iso]');
      await page.selectOption('#io', '汇文楼');
      const out1 = await page.textContent('#ires');
      await page.check('[name=idir][value=in]');
      const in1 = await page.textContent('#ires');
      if (!out1.includes('分钟') || out1 === in1) fails.push('可达圈出发/到达结果异常');
      await page.screenshot({ path: `${shots}/iso.png` });
      await page.selectOption('#layer', 'slope'); await page.screenshot({ path: `${shots}/slope.png` });
      // KML 拖入
      const kml = process.env.KML_SAMPLE;
      if (kml) {
        await page.click('[data-tab=kml]');
        await page.setInputFiles('#kfile', kml);
        await page.waitForFunction(() => document.getElementById('kres').textContent.includes('自动识别'));
        const k = await page.textContent('#kres'); console.log('KML 预览：', k.slice(0, 200));
        await page.screenshot({ path: `${shots}/kml.png` });
      }
    } else {
      await page.screenshot({ path: `${shots}/mobile.png` });
    }
    if (errors.length) fails.push(`${tag} 控制台错误：${errors.slice(0, 5).join(' | ')}`);
    await page.close();
  }
  await browser.close();
  if (fails.length) { console.error('失败：\n' + fails.join('\n')); process.exit(1); }
  console.log('浏览器验收通过；截图在', shots);
})();
