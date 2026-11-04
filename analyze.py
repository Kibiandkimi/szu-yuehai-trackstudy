"""粤海校区道路通行效率（轨迹重建）主流程。

    python3 analyze.py            # 先扫描新 KML（tracks.py scan），再完整重算，写 out/ 与 site/

步骤：轨迹库 → 逐点方式 → HMM 地图匹配 → 路段×方向观测 → 稳健回归（方向性坡度/道路类别/路段/高峰）
→ 留一记录验证 → 有向路网规划：宿舍→教学楼、教学区之间（双向）、全地点 OD、5/10 分钟可达圈 → 地图。
"""
import argparse
import collections
import csv
import json
import math
import time
from datetime import datetime

import numpy as np
from scipy.spatial import cKDTree

import config
import matching
import model
import network
import places as places_mod
import tracks as tracks_mod
from routing import Planner
from tracks import TZ, xy


# ---------- 上下课高峰（来自课表；仅工作日） ----------
STARTS = ['8:30', '10:15', '13:30', '16:00', '19:00', '20:30']   # 主要开课时刻（含大课间后）
ENDS = ['9:55', '11:40', '12:25', '15:40', '17:25', '20:20', '21:45']
BOUNDS = ['9:10', '9:15', '11:00', '11:45', '14:10', '14:15', '14:55', '15:00', '16:40', '16:45']  # 节间换教室
# 课间（下课 → 下一节开始）：同一大节内 5 分钟；大课间 20 分钟；晚上两段之间 10 分钟
BREAKS = {5: '节间 5 分钟', 10: '晚间 10 分钟', 20: '大课间 20 分钟'}


def _m(s):
    h, m = s.split(':'); return int(h) * 60 + int(m)


PEAK = [(_m(s) - 15, _m(s) + 3) for s in STARTS] + [(_m(e) - 2, _m(e) + 15) for e in ENDS] \
     + [(_m(b) - 3, _m(b) + 3) for b in BOUNDS]


def period(ts):
    d = datetime.fromtimestamp(ts, TZ)
    if d.weekday() >= 5:
        return 'off'
    m = d.hour * 60 + d.minute + d.second / 60
    return 'peak' if any(a <= m <= b for a, b in PEAK) else 'off'


def in_campus(tr):
    B = network.BBOX
    return B[0] < np.median(tr['lon']) < B[1] and B[2] < np.median(tr['lat']) < B[3]


# ---------- 1. 轨迹 → 观测 ----------
def observe(trs, edges):
    P, PE, PS = network.densify(edges); tree = cKDTree(P); router = matching.Router(edges)
    rows, used, matched_geo = [], [], {}
    for tr in trs:
        x, y = xy(tr['lon'], tr['lat']); t = tr['t']
        modes, v = model.point_modes(tr['label'], x, y, t)
        keep = matching.thin(x, y, t)
        t0 = time.time()
        states, links = matching.match(x[keep], y[keep], t[keep], tree, PE, PS, edges, router)
        on = np.array([s is not None for s in states])
        dist = [math.hypot(*(np.array(network.point_at(edges[s[0]], s[1])) - [x[keep[i]], y[keep[i]]])) for i, s in enumerate(states) if s is not None]
        disp = math.hypot(x[-1] - x[0], y[-1] - y[0]); dur = t[-1] - t[0]
        commute = tr['label'] == 'bike' or (tr['label'] == 'walk' and dur <= 25 * 60 and disp > 300)
        acc, sec_used, sec_rej = model.traversals(tr, x, y, modes, keep, states, links, period)
        r = model.rows_from(tr['file'], acc, edges, commute)
        rows += r
        used.append(dict(file=tr['file'], label=tr['label'], label_source=tr['label_source'], points=len(t),
                         matched=round(float(on.mean()), 3), match_dist_p50=round(float(np.median(dist)), 1) if dist else None,
                         match_dist_p90=round(float(np.percentile(dist, 90)), 1) if dist else None,
                         moving_s_used=round(sec_used), moving_s_rejected=round(sec_rej), observations=len(r),
                         commute=commute, minutes=round(dur / 60, 1), match_s=round(time.time() - t0, 1),
                         modes={k: int((modes == k).sum()) for k in ('walk', 'run', 'bike', 'stop')}))
        matched_geo[tr['file']] = [network.lonlat_at(edges[e], s0, s1) for link in links if link for e, s0, s1 in link[1]]
        arcs = collections.Counter()
        for link in links:
            for e, s0, s1 in (link[1] if link else ()):
                arcs[(e, '+' if s1 > s0 else '-')] += abs(s1 - s0)
        on_i = [keep[i] for i, s in enumerate(states) if s is not None]
        used[-1]['_arcs'] = arcs
        used[-1]['_ends'] = ((float(tr['lon'][on_i[0]]), float(tr['lat'][on_i[0]])), (float(tr['lon'][on_i[-1]]), float(tr['lat'][on_i[-1]]))) if on_i else None
        used[-1]['_period'] = period(t[0])
        used[-1]['gps_m'] = round(float(np.hypot(np.diff(x), np.diff(y)).sum()))
        print(f'  {tr["file"]} {tr["label"]:<5} 匹配 {on.mean():.0%}，偏差中位 {used[-1]["match_dist_p50"]} m，观测 {len(r)}，{used[-1]["match_s"]} s')
    return rows, used, matched_geo


# ---------- 2. 拟合与验证 ----------
MODES = (('walk', 'foot'), ('bike', 'bike'))
VARIANTS = {'常速（仅台阶先验）': (), '坡度+道路类别': ('grade', 'cls'), '完整模型': ('grade', 'cls', 'edge', 'dir', 'peak')}


def validate(rows, edges, commute):
    """留一记录：去掉整条记录重拟合，预测它在实际匹配路径上的移动耗时。
    “未校准”用其他通勤记录的平均配速；“前 40% 校准”用该记录前 40% 路程估计当天配速，只评估后 60%。"""
    out = []
    for mode, fam in MODES:
        test = sorted({r['track'] for r in rows if r['mode'] == mode and r['track'] in commute})
        for k in test:
            fit = model.Fit(rows, fam, edges, exclude_track=k)
            if not fit.ok:
                continue
            mu = fit.base(mode, commute - {k})
            rk = sorted((r for r in rows if r['track'] == k and r['mode'] == mode), key=lambda r: r['t0'])
            cm = np.cumsum([r['m'] for r in rk]); cut = cm <= 0.4 * cm[-1]
            actual = sum(r['s'] for r in rk); rec = dict(track=k, mode=mode, meters=round(cm[-1]), actual_s=round(actual, 1))
            for name, parts in VARIANTS.items():
                pred = np.array([r['m'] * math.exp(mu + fit.road_log(r['edge'], r['dir'], r['period'] == 'peak', parts)) for r in rk])
                act = np.array([r['s'] for r in rk])
                rec[name] = round(float(pred.sum() / actual - 1), 4)
                if cut.any() and (~cut).any():
                    delta = act[cut].sum() / pred[cut].sum()
                    rec[name + '·前40%校准'] = round(float(pred[~cut].sum() * delta / act[~cut].sum() - 1), 4)
            out.append(rec)
    return out


def edge_speeds(fits, bases, edges, variant=None):
    """sec_per_m[(mode, period)][(e, d)]。variant=None 用路段当前规则；'osm' 忽略规则；'push'/'ride' 假定骑行为该规则
    （供网页端即时预览规则修改，与这里的计算一致）。"""
    sp = {}
    for mode, fam in MODES:
        f = fits[fam]
        for per in ('off', 'peak'):
            d_ = {}
            for e in edges:
                saved = e['rules']
                if variant is not None:
                    e['rules'] = {} if variant == 'osm' else {('bike', '+'): variant, ('bike', '-'): variant}
                for d in '+-':
                    d_[(e['id'], d)] = math.exp(bases[mode] + f.road_log(e['id'], d, per == 'peak'))
                e['rules'] = saved
            sp[(mode, per)] = d_
    return sp


def allowed_for(mode):
    """通行限制：OSM（禁止步行 / 骑行单行）+ 用户封禁规则。"""
    if mode == 'walk':
        return lambda e, d: not e['foot_no'] and e['rules'].get(('walk', d)) != 'block'
    return lambda e, d: not (e['oneway'] and d == '-') and e['rules'].get(('bike', d)) != 'block'


# ---------- 3. 规划分析 ----------
def custom_routes(rules, P):
    """{(起点, 终点, 方式): [途经点]}；both=True 时反向用倒序途经点。"""
    out = {}
    for r in rules.get('routes', []):
        if r['origin'] in P and r['dest'] in P:
            out[(r['origin'], r['dest'], r['mode'])] = [tuple(v) for v in r['via']]
            if r.get('both'):
                out.setdefault((r['dest'], r['origin'], r['mode']), [tuple(v) for v in r['via']][::-1])
    return out


def od_analysis(planners, P, edges, rules):
    """全地点 OD（双向、两种方式、平/高峰）。有用户自定义路线的 OD 按途经点逐段规划（custom=True）。"""
    names = list(P); od = []; geo = {}; CR = custom_routes(rules, P)
    for (mode, per), pl in planners.items():
        snaps = {n: pl.snap(P[n]['lon'], P[n]['lat']) for n in names}
        for o in names:
            dist = pl.tree_from(snaps[o])
            for d in names:
                if d == o:
                    continue
                via = CR.get((o, d, mode))
                if via:
                    r = pl.route_via([(P[o]['lon'], P[o]['lat']), *via, (P[d]['lon'], P[d]['lat'])])
                else:
                    r = pl.route(None, None, S=snaps[o], dist=dist, T=snaps[d])
                if r is None:
                    continue
                sup = sum(p for e, dd, p in r['arcs'] if (e, dd) in SUPPORT[mode]) / max(r['net_m'], 1)
                od.append(dict(origin=o, dest=d, origin_group=P[o]['group'], dest_group=P[d]['group'], mode=mode, period=per,
                               time_min=round(r['time_s'] / 60, 2), dist_m=round(r['dist_m']), ascent_m=round(r['ascent_m'], 1),
                               descent_m=round(r['descent_m'], 1), avg_kmh=round(r['dist_m'] / r['time_s'] * 3.6, 1),
                               data_coverage=round(sup, 2), custom=bool(via)))
                if per == 'off':
                    geo[(o, d, mode)] = [[round(a, 6), round(b, 6)] for a, b in r['geo']]
    return od, geo


ISO_LIMITS = {'walk': (300, 600, 900), 'bike': (180, 300, 600)}


def isochrones(planners, P, od):
    """各地点出发圈（步行 5/10/15、骑行 3/5/10 分钟）（从这里出发能到）与到达圈（从哪里能按时到这里）：路网长度与地点。"""
    T = {(r['origin'], r['dest'], r['mode']): r['time_min'] * 60 for r in od if r['period'] == 'off'}
    out = []
    for (mode, per), pl in planners.items():
        if per != 'off':
            continue
        limits = ISO_LIMITS[mode]
        for o in P:
            S = pl.snap(P[o]['lon'], P[o]['lat'])
            for direction, rev in (('出发', False), ('到达', True)):
                dist = pl.tree_from(S, reverse=rev, limit=max(limits) + 60)
                rec = dict(place=o, group=P[o]['group'], mode=mode, direction=direction)
                for lim in limits:
                    rec[f'net_km_{lim // 60}min'] = round(sum(pl.reach(dist, lim, reverse=rev).values()) / 1000, 2)
                    rec[f'places_{lim // 60}min'] = [d for d in P if d != o and T.get((d, o, mode) if rev else (o, d, mode), 1e9) <= lim]
                out.append(rec)
    return out


def route_choice(used, planners, speeds):
    """实测通勤的起终点重新规划：规划路线与实际路线的重合度，以及模型对两条路线的耗时估计。"""
    out = []
    for u in used:
        if not u['commute'] or not u['_ends'] or u['label'] not in ('walk', 'bike'):
            continue
        key = (u['label'], u['_period']); pl = planners[key]; sp = speeds[key]
        r = pl.route(*u['_ends'])
        if r is None:
            continue
        actual_m = sum(u['_arcs'].values())
        actual_pred = sum(L * sp[k] for k, L in u['_arcs'].items() if k in pl.cost)
        overlap = sum(p for e, d, p in r['arcs'] if u['_arcs'].get((e, d), 0) > 0) / max(r['net_m'], 1)
        out.append(dict(track=u['file'], mode=u['label'], period=u['_period'], planned_m=round(r['net_m']), actual_m=round(actual_m), gps_m=u['gps_m'],
                        overlap=round(overlap, 2), planned_pred_min=round(r['time_s'] / 60, 2),
                        actual_route_pred_min=round(actual_pred / 60, 2), actual_min=u['minutes']))
    return out


SUPPORT = {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-scan', action='store_true')
    args = ap.parse_args()
    if not args.no_scan:
        new = tracks_mod.scan(quiet=True)
        if new:
            print('新入库轨迹：', ', '.join(new))
    terrain = network.Terrain()
    print('地形：', f'DirMap {terrain.meta["name"]} {terrain.meta["schema"]}' if terrain.ok else '无 DirMap 地形（按平地；本地请运行 sync_dirmap.py）')
    edges = network.build_graph(terrain)
    rules = network.load_rules()
    missing = network.apply_rules(edges, rules)
    print(f'用户规则：{len(rules["edges"])} 条路段规则，{len(rules["routes"])} 条自定义路线' + (f'；{len(missing)} 条找不到路段（OSM 参照已变）' if missing else ''))
    print(f'路网：{len(edges)} 段，{sum(e["len"] for e in edges)/1000:.1f} km；有地形高程 {sum(e["len"] for e in edges if e["h_known"])/1000:.1f} km')
    trs = []
    for tr in tracks_mod.load_tracks():
        if tr['label'] in ('exclude', 'unknown'):
            print('不参与（方式', tr['label'], '）', tr['file'], tr['label_source']); continue
        if not in_campus(tr):
            print('不参与（不在校园）', tr['file']); continue
        trs.append(tr)
    rows, used, matched_geo = observe(trs, edges)
    commute = {u['file'] for u in used if u['commute']}

    fits = {fam: model.Fit(rows, fam, edges) for _, fam in MODES}
    bases = {'walk': fits['foot'].base('walk', commute), 'bike': fits['bike'].base('bike', commute)}
    for mode, fam in MODES:
        SUPPORT[mode] = {(r['edge'], r['dir']) for r in rows if r['family'] == fam}
    print('通勤基准：', {m: f'{math.exp(-b):.2f} m/s' for m, b in bases.items()})
    for fam, f in fits.items():
        print(f'  {fam}: 坡度系数 {np.round(f.theta, 2).tolist()}（先验 {f.prior.tolist()}），高峰步行空间 ×{math.exp(f.beta["ped"]):.2f}（{f.n_peak["ped"]} 观测），'
              f'类别 { {k: round(v, 3) for k, v in f.cls.items()} }，残差 MAD {f.residual_mad:.3f}')
    val = validate(rows, edges, commute)
    speeds = edge_speeds(fits, bases, edges)
    variants = {v: edge_speeds(fits, bases, edges, v) for v in ('osm', 'push', 'ride')}
    walk_speed = 1 / math.exp(bases['walk'])
    planners = {k: Planner(edges, v, walk_speed, allowed_for(k[0])) for k, v in speeds.items()}
    P = places_mod.load()
    choice = route_choice(used, planners, speeds)
    for c in choice:
        print('  路线校验', c)
    od, od_geo = od_analysis(planners, P, edges, rules)
    iso = isochrones(planners, P, od)
    from report import write_outputs
    write_outputs(dict(locals(), PEAK=PEAK, BREAKS=BREAKS))


if __name__ == '__main__':
    main()
