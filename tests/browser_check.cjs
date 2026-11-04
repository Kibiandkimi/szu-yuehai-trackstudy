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
        for (const r of D.od) {
          const js = placeRoute(r.origin, r.dest, `${r.mode}_${r.period}`);
          out.push(js ? Math.abs(js.t / 60 - r.time_min) / r.time_min : 1);
        }
        return { n: out.length, custom: D.od.filter(r => r.custom).length, max: Math.max(...out), bad: out.filter(x => x > 0.01).length };
      });
      console.log('JS/Python OD 对比', diff);
      if (diff.bad) fails.push(`JS 与 Python 规划不一致：${diff.bad}/${diff.n}，最大 ${diff.max}`);
      const custom = await page.evaluate(() => {
        const { placeRoute, EDGES } = window.__trackstudy, out = [];
        for (const dest of ['汇文楼', '致理楼']) for (const reverse of [false, true]) for (const per of ['off', 'peak']) {
          const r = placeRoute(reverse ? dest : '斋区', reverse ? '斋区' : dest, `bike_${per}`);
          const edges = r ? r.arcs.filter(a => a[2] > 1).map(a => EDGES[a[0]]) : [];
          out.push({ dest, reverse, per, custom: !!(r && r.custom), minutes: r && r.t / 60,
            valid: dest === '汇文楼' ? edges.length > 0 && !edges.some(e => e.hw === 'steps') : edges.some(e => e.name === '召南天桥') });
        }
        return out;
      });
      console.log('指定路线检查', custom);
      if (custom.some(r => !r.custom || !r.valid)) fails.push('斋区自定义路线未避开台阶或未经过召南天桥');
      const iso = await page.evaluate(() => {
        const T = window.__trackstudy, out = {n: 0, maxKm: 0, badLength: 0, badPlaces: 0, partialEdge: null};
        for (const row of T.D.iso) {
          const p = T.D.places[row.place], key = `${row.mode}_off`, reverse = row.direction === '到达';
          const limits = row.mode === 'walk' ? [300, 600, 900] : [180, 300, 600];
          const S = T.snap(p.lon, p.lat, key), dist = T.tree(S, key, reverse, Math.max(...limits) + 60);
          const places = T.isoPlaces([p.lon, p.lat], key, reverse, row.place);
          for (const limit of limits) {
            let length = 0;
            for (const segs of T.reachSegments(dist, limit, key, reverse, S).values())
              for (const [lo, hi] of segs) length += hi - lo;
            const delta = Math.abs(length / 1000 - row[`net_km_${limit / 60}min`]);
            out.n++; out.maxKm = Math.max(out.maxKm, delta);
            if (delta > 0.011) out.badLength++;
            const expected = new Set(row[`places_${limit / 60}min`]);
            // CSV 的 OD 时间四舍五入至 0.01 分钟，阈值边界允许 0.3 秒差。
            for (const [name, seconds] of places)
              if (expected.has(name) !== (seconds <= limit) && Math.abs(seconds - limit) > 0.31) out.badPlaces++;
            if ([...expected].some(name => !places.some(p => p[0] === name))) out.badPlaces++;
          }
        }
        const e = T.EDGES.find(e => e.oneway && e.len > 20 && !T.D.rules.edges.some(r => r.key === e.key));
        if (e) {
          const limit = T.D.cost.bike_off[e.id][0] * 0.1, S = [[e.id, e.len * 0.95, 0]];
          const get = reverse => T.reachSegments(new Map(), limit, 'bike_off', reverse, S).get(e.id);
          const forward = get(false), backward = get(true);
          out.partialEdge = !!(forward && backward && Math.abs(forward[0][0] - e.len * 0.95) < 1e-6 &&
            Math.abs(forward[0][1] - e.len) < 1e-6 && Math.abs(backward[0][0] - e.len * 0.85) < 1e-6 &&
            Math.abs(backward[0][1] - e.len * 0.95) < 1e-6);
        }
        return out;
      });
      console.log('JS/Python 可达圈对比', iso);
      if (iso.badLength || iso.badPlaces || iso.partialEdge === false) fails.push('可达圈路网长度、地点或单行路起点片段异常');
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
      // 真实编辑途经点：撤销一点，再点地图补回，预览双向路线后放弃修改。
      await page.click('#vedit');
      await page.click('#vundo');
      const via = await page.evaluate(() => {
        const T = window.__trackstudy, point = T.D.rules.routes.find(r => r.origin === '斋区' && r.dest === '汇文楼').via.at(-1);
        T.map.setView([point[1], point[0]], 18, {animate: false});
        const p = T.map.latLngToContainerPoint([point[1], point[0] + 0.00003]), r = T.map.getContainer().getBoundingClientRect();
        return [p.x + r.left, p.y + r.top];
      });
      await page.mouse.click(via[0], via[1]);
      if (await page.evaluate(() => window.__trackstudy.st.editVia.via.length) !== 3) fails.push('地图点击未添加自定义途经点');
      await page.click('#vdone');
      const edited = await page.evaluate(() => {
        const T = window.__trackstudy;
        return ['斋区|汇文楼', '汇文楼|斋区'].every(pair => {
          const [o, d] = pair.split('|'), route = T.placeRoute(o, d, 'bike_off');
          return route && route.custom && route.custom.via.length === 3 && route.t > 0;
        });
      });
      if (!edited || !(await page.isVisible('#savebar'))) fails.push('自定义路线编辑后未更新双向预览或保存栏');
      if (await page.isVisible('#rdiscard')) await page.click('#rdiscard');
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
