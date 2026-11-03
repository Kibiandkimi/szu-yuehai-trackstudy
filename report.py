"""输出：out/（完整，含逐轨迹明细，不公开）与 site/（公开站点，仅聚合结果、记录匿名）。"""
import csv
import json
import math
import time
from pathlib import Path

import numpy as np

import config
import network
import routing
from map_view import render_map
from tracks import LAT0, LON0


def _csv(path, rows):
    if not rows:
        return
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        cols = list(dict.fromkeys(k for r in rows for k in r))
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)


def _r(v, n=3):
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(float(v), n)


def edge_table(L):
    edges, fits, bases, speeds, rows = L['edges'], L['fits'], L['bases'], L['speeds'], L['rows']
    obs = {}
    for r in rows:
        a = obs.setdefault((r['edge'], r['dir'], r['mode']), [0.0, 0.0, set()])
        a[0] += r['m']; a[1] += r['s']; a[2].add(r['track'])
    out = []
    for mode, fam in (('walk', 'foot'), ('bike', 'bike')):
        f = fits[fam]; flat = math.exp(-bases[mode])
        for e in edges:
            for d in '+-':
                o = obs.get((e['id'], d, mode)); sup = f.support_dir.get((e['id'], d), set())
                if not o and not sup and e['hw'] in network.SKIP_HW:
                    continue
                v_off = 1 / speeds[(mode, 'off')][(e['id'], d)]; v_pk = 1 / speeds[(mode, 'peak')][(e['id'], d)]
                rise = e['rise'] if d == '+' else -e['rise']
                out.append(dict(edge=e['id'], dir=d, mode=mode, name=e['name'] or e['hw'], highway=e['hw'],
                                road_class=network.road_class(e, d), length_m=round(e['len'], 1), rise_m=round(rise, 2),
                                grade_pct=round(100 * rise / e['len'], 2), terrain=e['h_known'],
                                speed_off_mps=round(v_off, 3), speed_peak_mps=round(v_pk, 3), s_per_100m=round(100 / v_off, 1),
                                efficiency=round(v_off / flat, 3), grade_effect=round(math.exp(-float(e['F'][d] @ f.theta)) if not network.road_class(e, d).startswith('steps') else 1.0, 3),
                                edge_effect=round(math.exp(-f.u.get(e['id'], 0) - f.v.get((e['id'], d), 0)), 3),
                                support_tracks=len(sup), observed_m=round(o[0], 1) if o else 0, observed_s=round(o[1], 1) if o else 0,
                                observed_mps=round(o[0] / o[1], 2) if o else None, observed_tracks=len(o[2]) if o else 0))
    return out


def teaching_commute(od, P):
    """教学区之间：双向耗时、方向差、能否在课间赶到（路上时间 + 出入楼/取停车）。"""
    import places
    T = {(r['origin'], r['dest'], r['mode'], r['period']): r for r in od}
    out = []
    names = places.TEACHING
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            for mode in ('walk', 'bike'):
                for per in ('off', 'peak'):
                    ab, ba = T.get((a, b, mode, per)), T.get((b, a, mode, per))
                    if not ab or not ba:
                        continue
                    out.append(dict(a=a, b=b, mode=mode, period=per, dist_m=ab['dist_m'],
                                    a_to_b_min=ab['time_min'], b_to_a_min=ba['time_min'],
                                    diff_min=round(ab['time_min'] - ba['time_min'], 2),
                                    asym_pct=round(100 * (ab['time_min'] / ba['time_min'] - 1), 1),
                                    a_to_b_ascent_m=ab['ascent_m'], b_to_a_ascent_m=ba['ascent_m'],
                                    coverage=min(ab['data_coverage'], ba['data_coverage'])))
    return out


def observed_trips(L):
    """实测直达行程：起终点各距地点中心 ≤150 m。"""
    from tracks import xy
    P = L['P']; out = []
    PXY = {k: xy(v['lon'], v['lat']) for k, v in P.items()}
    by = {u['file']: u for u in L['used']}
    for tr in L['trs']:
        if tr['label'] not in ('walk', 'bike'):
            continue
        x, y = xy(tr['lon'], tr['lat'])

        def near(i):
            k = min(PXY, key=lambda k: math.hypot(x[i] - PXY[k][0], y[i] - PXY[k][1]))
            return k if math.hypot(x[i] - PXY[k][0], y[i] - PXY[k][1]) <= 150 else None
        a, b = near(0), near(-1)
        if a and b and a != b:
            model_t = next((r['time_min'] for r in L['od'] if r['origin'] == a and r['dest'] == b and r['mode'] == tr['label']
                            and r['period'] == by[tr['file']]['_period']), None)
            out.append(dict(file=tr['file'], mode=tr['label'], period=by[tr['file']]['_period'], origin=a, dest=b,
                            dist_m=round(float(np.hypot(np.diff(x), np.diff(y)).sum())), time_min=round((tr['t'][-1] - tr['t'][0]) / 60, 1),
                            model_min=model_t))
    return out


def map_data(L, public):
    edges, P = L['edges'], L['P']
    node_ix = {}
    for e in edges:
        for k in (e['a'], e['b']):
            node_ix.setdefault(k, len(node_ix))
    fits, bases = L['fits'], L['bases']
    E = []
    for e in edges:
        E.append([node_ix[e['a']], node_ix[e['b']], round(e['len'], 2),
                  [c for p in e['ll'] for c in (round(p[0], 7), round(p[1], 7))],
                  e['name'], e['hw'], round(e['rise'], 2), round(e['ascent'], 2), round(e['descent'], 2), int(e['h_known'])])
    cost = {}
    for (mode, per), pl in L['planners'].items():
        cost[f'{mode}_{per}'] = [[_r(pl.cost.get((e['id'], d)), 2) for d in '+-'] for e in edges]
    eff, support, observed = {}, {}, {}
    for mode, fam in (('walk', 'foot'), ('bike', 'bike')):
        f = fits[fam]
        eff[mode] = [[round(math.exp(-f.road_log(e['id'], d)), 3) for d in '+-'] for e in edges]
        support[mode] = [[len(f.support_dir.get((e['id'], d), ())) for d in '+-'] for e in edges]
        o = {}
        for r in L['rows']:
            if r['family'] == fam:
                a = o.setdefault((r['edge'], r['dir']), [0.0, 0.0]); a[0] += r['m']; a[1] += r['s']
        observed[mode] = {f'{k[0]}{k[1]}': [round(v[0]), round(v[1], 1)] for k, v in o.items()}
    anon = {}
    def tid(f, mode):
        if not public:
            return f
        anon.setdefault(mode, {})
        return anon[mode].setdefault(f, f'{"骑行" if mode == "bike" else "步行"}#{len(anon[mode]) + 1}')
    val = [dict(v, track=tid(v['track'], v['mode'])) for v in L['val']]
    choice = [dict(c, track=tid(c['track'], c['mode'])) for c in L['choice']]
    import dirmap_store
    dm = {}
    prof = dirmap_store.travel_profiles()
    if prof:
        dm = {k: dict(coefficients=v['coefficients'], tracks=v['tracks'], observations=v['observations'], gradeLearned=v['gradeLearned'],
                      gradeRange=v['gradeRange']) for k, v in prof.items()}
    import model
    models = {}
    for mode, fam in (('walk', 'foot'), ('bike', 'bike')):
        f = fits[fam]
        models[mode] = dict(family=fam, theta=[round(float(t), 3) for t in f.theta], prior=f.prior.tolist(),
                            dirmap=dm.get('foot' if fam == 'foot' else 'bicycle'),
                            curve=model.grade_curve(f.theta, f.prior), dirmap_curve=model.grade_curve(np.array(dm['foot' if fam == 'foot' else 'bicycle']['coefficients']), f.prior) if dm else None,
                            classes={k: round(math.exp(-(v + f.cprior.get(k, 0.0))), 3) for k, v in f.cls.items()},
                            peak={k: round(math.exp(-v), 3) for k, v in f.beta.items()}, n_peak=f.n_peak,
                            base_mps=round(math.exp(-bases[mode]), 3), residual_mad=round(f.residual_mad, 3),
                            tracks=len({r['track'] for r in f.rows}), episodes=len(f.alpha), observations=len(f.rows),
                            observed_m=round(sum(r['m'] for r in f.rows)),
                            grade_range=[_r(np.percentile([100 * (e['rise'] / e['len']) for e in edges if e['h_known'] and e['len'] > 20], q), 1) for q in (5, 95)])
    used = [dict(track=tid(u['file'], u['label']), label=u['label'], label_source=u['label_source'].split('：')[0], matched=u['matched'],
                 match_dist_p50=u['match_dist_p50'], observations=u['observations'], commute=u['commute'],
                 **({} if public else dict(minutes=u['minutes'])))
            for u in L['used']]
    iso_rows = [dict(r) for r in L['iso']]
    return dict(
        meta=dict(generated=time.strftime('%Y-%m-%d %H:%M'), public=public, lat0=LAT0, lon0=LON0,
                  terrain=dict(name=L['terrain'].meta['name'], schema=L['terrain'].meta['schema'], version=L['terrain'].meta.get('sourceVersion'),
                               spacing=L['terrain'].meta['spacingM'], range=[L['terrain'].meta['minHeightM'], L['terrain'].meta['maxHeightM']]) if L['terrain'].ok else None,
                  tracks=dict(walk=sum(u['label'] == 'walk' for u in L['used']), run=sum(u['label'] == 'run' for u in L['used']),
                              bike=sum(u['label'] == 'bike' for u in L['used'])),
                  peak=L['PEAK'], breaks=L['BREAKS'], dirmap_url=config.load()['dirmap_url'], site_url=config.load()['site_url'], walk_speed=round(L['walk_speed'], 4), snap=dict(k=routing.SNAP_K, r=routing.SNAP_R, slack=routing.SNAP_SLACK, detour=routing.ACCESS_DETOUR)),
        nodes=[[k[0], k[1]] for k in node_ix], edges=E, cost=cost, eff=eff, support=support, observed=observed,
        places=P, od=L['od'], teaching=L['teaching'], iso=iso_rows, models=models, validation=val, choice=choice, tracks=used,
        observed_trips=[dict(o, file=tid(o['file'], o['mode'])) for o in L['obs_trips']],
        matched=None if public else {k: v for k, v in L['matched_geo'].items()})


def write_outputs(L):
    out, site = config.out_dir(), config.site_dir()
    out.mkdir(exist_ok=True); (site / 'data').mkdir(parents=True, exist_ok=True)
    L['teaching'] = teaching_commute(L['od'], L['P'])
    L['obs_trips'] = observed_trips(L)
    et = edge_table(L)
    _csv(out / 'edges.csv', et)
    feats = [dict(type='Feature', properties=r, geometry=dict(type='LineString', coordinates=L['edges'][r['edge']]['ll'] if r['dir'] == '+' else L['edges'][r['edge']]['ll'][::-1]))
             for r in et if r['support_tracks'] or r['observed_tracks']]
    gj = dict(type='FeatureCollection', features=feats)
    (out / 'edges.geojson').write_text(json.dumps(gj, ensure_ascii=False), encoding='utf-8')
    _csv(out / 'od.csv', L['od'])
    _csv(out / 'od_dorm_teaching.csv', [r for r in L['od'] if r['origin_group'] == '宿舍区' and r['dest_group'] == '教学区'])
    _csv(out / 'teaching_commute.csv', L['teaching'])
    _csv(out / 'isochrones.csv', [dict(r, **{k: '、'.join(v) for k, v in r.items() if isinstance(v, list)}) for r in L['iso']])
    _csv(out / 'validation.csv', L['val'])
    _csv(out / 'route_choice.csv', L['choice'])
    _csv(out / 'observed_trips.csv', L['obs_trips'])
    _csv(out / 'tracks.csv', [{k: v for k, v in u.items() if not k.startswith('_') and k != 'modes'} for u in L['used']])
    full = map_data(L, public=False)
    summary = {k: full[k] for k in ('meta', 'models', 'validation', 'choice', 'tracks')}
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float), encoding='utf-8')
    (out / 'map.html').write_text(render_map(full, network.BBOX), encoding='utf-8')
    pub = map_data(L, public=True)
    (site / 'index.html').write_text(render_map(pub, network.BBOX), encoding='utf-8')
    # 公开下载：聚合 CSV（无逐轨迹时间）
    _csv(site / 'data' / 'od.csv', L['od'])
    _csv(site / 'data' / 'teaching_commute.csv', L['teaching'])
    _csv(site / 'data' / 'edges.csv', et)
    _csv(site / 'data' / 'isochrones.csv', [dict(r, **{k: '、'.join(v) for k, v in r.items() if isinstance(v, list)}) for r in L['iso']])
    if config.load()['update_readme']:
        readme_results(L)
    print(f'输出：{out}/map.html（{(out / "map.html").stat().st_size / 1e6:.1f} MB），{site}/index.html')


def readme_results(L):
    """README“主要结果”一节（每次重算自动更新）。"""
    M = {md: L['fits'][fam] for md, fam in (('walk', 'foot'), ('bike', 'bike'))}
    T = {(r['origin'], r['dest'], r['mode'], r['period']): r for r in L['od']}
    t = lambda a, b, md: T[(a, b, md, 'off')]['time_min']
    import dirmap_store
    dm = {k: v['coefficients'] for k, v in (dirmap_store.travel_profiles() or {}).items()}
    pct = lambda th, g: 100 * (math.exp(float(network.grade_features(g) @ th)) - 1)
    val = {}
    for v in L['val']:
        for k in ('常速（仅台阶先验）', '完整模型', '完整模型·前40%校准'):
            if k in v:
                val.setdefault((v['mode'], k), []).append(abs(v[k]))
    med = lambda md, k: 100 * float(np.median(val[(md, k)])) if (md, k) in val else float('nan')
    tc = [r for r in L['teaching'] if r['period'] == 'off']
    big = sorted((r for r in tc if r['mode'] == 'bike'), key=lambda r: -abs(r['asym_pct']))[:3]
    used = L['used']; mt = [u for u in used if u['matched'] > 0]
    lines = [f"## 主要结果（{time.strftime('%Y-%m-%d')}，{sum(u['label'] in ('walk', 'run') for u in used)} 条步行/跑步 + "
             f"{sum(u['label'] == 'bike' for u in used)} 条骑行校内轨迹）", '']
    for md, zh in (('bike', '骑行'), ('walk', '步行')):
        th = M[md].theta; d = dm.get('bicycle' if md == 'bike' else 'foot')
        lines.append(f"- **坡度（{zh}）**：θ = [{', '.join(f'{v:.2f}' for v in th)}]，5% 上坡慢 {pct(th, .05):.0f}%、5% 下坡快 {-pct(th, -.05):.0f}%"
                     + (f"；DirMap 独立学到 [{', '.join(f'{v:.2f}' for v in d)}]（5% 上坡慢 {pct(np.array(d), .05):.0f}%）" if d else ''))
    lines.append('- **教学区之间方向差最大（骑行、平峰，路上时间）**：' + '；'.join(
        f"{r['a']}→{r['b']} {r['a_to_b_min']:.1f} 分 / 回程 {r['b_to_a_min']:.1f} 分（爬升 {r['a_to_b_ascent_m']:.0f} / {r['b_to_a_ascent_m']:.0f} m）" for r in big))
    hz = T[('汇文楼', '致理楼', 'walk', 'off')]['time_min']
    lines.append(f"- **步行换楼**：汇文楼→致理楼 {hz:.1f} 分，加出入楼 1 分{'超过' if hz + 1 > 20 else '在'}大课间 20 分钟{'，需骑行' if hz + 1 > 20 else '内'}；"
                 f"5 分钟节间只够近邻楼（致理↔致真 {t('致理楼', '致真楼', 'walk'):.1f} 分、致信↔计软 {t('致信楼', '计软楼', 'walk'):.1f} 分）。")
    lines.append('- **宿舍→教学楼（平峰，步行 / 骑行）**：' + '；'.join(
        f"{a}→{b} {t(a, b, 'walk'):.1f} / {t(a, b, 'bike'):.1f} 分" for a in ('斋区', '乔区', '南区') for b in ('汇文楼', '致理楼')))
    pk = M['bike'].beta
    lines.append(f"- **高峰（骑行）**：车行道耗时 ×{math.exp(pk['road']):.2f}、步道/天桥 ×{math.exp(pk['ped']):.2f}"
                 f"（{M['bike'].n_peak['road'] + M['bike'].n_peak['ped']} 条高峰观测，支持弱）；步行 ×{math.exp(M['walk'].beta['road']):.2f} / ×{math.exp(M['walk'].beta['ped']):.2f}。")
    lines.append(f"- **验证（留一记录，中位绝对误差）**：步行 常速 {med('walk', '常速（仅台阶先验）'):.1f}% / 完整模型 {med('walk', '完整模型'):.1f}%；"
                 f"骑行 常速 {med('bike', '常速（仅台阶先验）'):.1f}% / 完整模型 {med('bike', '完整模型'):.1f}% / 前 40% 校准 {med('bike', '完整模型·前40%校准'):.1f}%。"
                 "误差主要来自每次出行本身的快慢；样本少，完整模型未显著优于常速。")
    ch = [c for c in L['choice'] if c['mode'] == 'bike']
    if ch:
        lines.append(f"- **路线选择校验（骑行）**：规划与实际路线重合中位 {100 * float(np.median([c['overlap'] for c in ch])):.0f}%（{len(ch)} 条）。")
    lines.append(f"- **地图匹配**：{len(mt)} 条在路网上的轨迹匹配率 {100 * min(u['matched'] for u in mt):.0f}–{100 * max(u['matched'] for u in mt):.0f}%，"
                 f"偏差中位 {min(u['match_dist_p50'] for u in mt):.1f}–{max(u['match_dist_p50'] for u in mt):.1f} m。")
    path = config.ROOT / 'README.md'
    s = path.read_text(encoding='utf-8')
    a, b = s.index('<!-- RESULTS:BEGIN'), s.index('<!-- RESULTS:END -->')
    head = s[a:s.index('\n', a) + 1]
    path.write_text(s[:a] + head + '\n'.join(lines) + '\n' + s[b:], encoding='utf-8')
