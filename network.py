"""路网（OSM 参照）+ DirMap 共享地形 → 带方向坡度的路段。

每条路段 a→b 为“正向”（OSM 绘制方向），反向坡度取相反数。坡度按约 10 m 的小段积分，
上坡后下坡的路段不会被抵消成平路。桥/隧道/台阶不取中间地形（桥面不在地面上），只用端点高差。
"""
import collections
import json
import math
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

import config
from tracks import xy

BBOX = (113.9225, 113.9420, 22.5255, 22.5450)   # 校园及周边
SKIP_HW = {'construction', 'proposed', 'trunk', 'trunk_link', 'primary', 'primary_link', 'motorway', 'motorway_link'}


# ---------- 地形 ----------
class Terrain:
    """DirMap `shared-track-height` 网格（10 m，双线性；无支持处 NaN），坐标系与 DirMap Frame 一致。"""
    def __init__(self):
        import dirmap_store
        self.meta, path = dirmap_store.region()
        self.ok = bool(path and path.is_file())
        if not self.ok:
            return
        with np.load(path, allow_pickle=False) as z:
            self.height = z['height'].astype(float)
            self.support = z['support'].astype(float) if 'support' in z.files else None
        lng, lat = self.meta['frame']['origin']
        phi = math.radians(lat); e2 = 0.00669437999014
        n = 6378137 / math.sqrt(1 - e2 * math.sin(phi) ** 2); m = 6378137 * (1 - e2) / (1 - e2 * math.sin(phi) ** 2) ** 1.5
        self.scales = np.array([n * math.cos(phi), m]) * math.pi / 180
        self.o = np.array([lng, lat])

    def sample(self, lon, lat):
        lon, lat = np.broadcast_arrays(np.asarray(lon, float), np.asarray(lat, float))
        out = np.full(lon.shape, np.nan)
        if not self.ok:
            return out
        p = ((np.stack([lon, lat], -1) - self.o) * self.scales - self.meta['origin']) / self.meta['spacingM']
        c = np.floor(p).astype(int); H = self.height
        ok = (c[..., 0] >= 0) & (c[..., 1] >= 0) & (c[..., 0] < H.shape[1] - 1) & (c[..., 1] < H.shape[0] - 1)
        cx, cy = np.where(ok, c[..., 0], 0), np.where(ok, c[..., 1], 0)
        dx, dy = p[..., 0] - cx, p[..., 1] - cy
        v = (H[cy, cx] * (1 - dx) * (1 - dy) + H[cy, cx + 1] * dx * (1 - dy)
             + H[cy + 1, cx] * (1 - dx) * dy + H[cy + 1, cx + 1] * dx * dy)
        out[ok] = v[ok]
        return out


def grade_features(g):
    """与 DirMap travel_model 相同的方向性坡度特征：[上坡, 缓下坡(≤5%), 陡下坡(超出 5% 部分)]。"""
    g = np.clip(np.nan_to_num(np.asarray(g, float)), -1., 1.)
    return np.stack((np.maximum(g, 0), np.minimum(np.maximum(-g, 0), .05), np.maximum(-g - .05, 0)), -1)


# ---------- 路网 ----------
def build_graph(terrain=None):
    ref = Path(config.load()['osm_reference']).expanduser()
    ways, seen = [], set()
    for f in ['yuehai-osm.json', 'yuehai-osm-extra.json']:
        for e in json.loads((ref / f).read_text(encoding='utf-8'))['elements']:
            t = e.get('tags', {})
            if e['type'] != 'way' or 'highway' not in t or t['highway'] in SKIP_HW or 'geometry' not in e or e['id'] in seen:
                continue
            g = [(p['lon'], p['lat']) for p in e['geometry'] if p]
            if not any(BBOX[0] < a < BBOX[1] and BBOX[2] < b < BBOX[3] for a, b in g):
                continue
            seen.add(e['id']); ways.append((e['id'], t, g))
    key = lambda p: (round(p[0], 7), round(p[1], 7))
    deg = collections.Counter()
    for wid, t, g in ways:
        for i, p in enumerate(g):
            deg[key(p)] += 1 if 0 < i < len(g) - 1 else 2  # 端点也作为分段点
    edges = []

    def add(wid, t, pts, link=False):
        x, y = xy([q[0] for q in pts], [q[1] for q in pts])
        L = float(np.hypot(np.diff(x), np.diff(y)).sum())
        if L <= 0.5 and not link:
            return
        hw = 'link' if link else t['highway']
        incline = t.get('incline', '')
        if t.get('oneway') == '-1':  # 统一成正向单行
            pts = pts[::-1]; x, y = x[::-1], y[::-1]
            incline = {'up': 'down', 'down': 'up'}.get(incline, incline)
        edges.append(dict(id=len(edges), key=edge_key(wid, pts), way=wid, hw=hw, name=t.get('name:zh', t.get('name', '')) if not link else '（拓扑补连）',
                          oneway=t.get('oneway') in ('yes', '-1', '1'), ll=[tuple(map(float, q)) for q in pts], x=x, y=y, len=max(L, 0.6),
                          a=key(pts[0]), b=key(pts[-1]), bridge=t.get('bridge', 'no') != 'no' or t.get('layer', '0') not in ('0', ''),
                          tunnel=t.get('tunnel', 'no') != 'no', incline=incline, covered=t.get('covered') == 'yes',
                          foot_no=t.get('foot') == 'no' or t.get('access') in ('no', 'private'),
                          bike_no=t.get('bicycle') == 'no'))

    for wid, t, g in ways:
        cur = [g[0]]
        for p in g[1:]:
            cur.append(p)
            if deg[key(p)] >= 2:
                if len(cur) >= 2:
                    add(wid, t, cur)
                cur = [p]
    # OSM 拓扑缺口：悬空端点与 5 m 内其他节点相连（不跨越立交时 5 m 足够保守）
    nd = collections.defaultdict(list)
    for e in edges:
        nd[e['a']].append(e); nd[e['b']].append(e)
    keys = list(nd); kx, ky = xy([k[0] for k in keys], [k[1] for k in keys]); kt = cKDTree(np.c_[kx, ky])
    for i, k in enumerate(keys):
        if len(nd[k]) != 1:
            continue
        for j in kt.query_ball_point([kx[i], ky[i]], 5.0):
            if j == i or keys[j] in (nd[k][0]['a'], nd[k][0]['b']):
                continue
            add(0, {}, [k, keys[j]], link=True)
    attach_terrain(edges, terrain or Terrain())
    for e in edges:
        e.setdefault('rules', {})
    return edges


def edge_key(wid, pts):
    """跨重算稳定的路段标识：OSM way + 两端坐标（正向 = 绘制方向，oneway=-1 已翻转）。规则文件用它定位路段。"""
    a, b = pts[0], pts[-1]
    return f'{wid}:{a[0]:.7f},{a[1]:.7f}:{b[0]:.7f},{b[1]:.7f}'


RULE_ACTIONS = {'bike': ('block', 'push', 'ride'), 'walk': ('block',)}


def load_rules(path=None):
    path = Path(path or config.load()['rules_file'])
    if not path.is_file():
        return dict(version=1, edges=[], routes=[])
    r = json.loads(path.read_text(encoding='utf-8'))
    r.setdefault('edges', []); r.setdefault('routes', [])
    return r


def apply_rules(edges, rules):
    """e['rules'][(mode, dir)] = block / push / ride。找不到的规则（OSM 参照变了）返回列表供报告。"""
    by = {e['key']: e for e in edges}
    missing = []
    for e in edges:
        e['rules'] = {}
    for r in rules.get('edges', []):
        e = by.get(r['key'])
        if e is None:
            missing.append(r); continue
        for d in ('+', '-') if r.get('dir', 'both') == 'both' else (r['dir'],):
            e['rules'][(r['mode'], d)] = r['action']
    return missing


def attach_terrain(edges, terrain):
    """每条路段：正/反向坡度特征（长度加权）、累计爬升/下降、台阶方向。"""
    for e in edges:
        x, y = e['x'], e['y']; seg = np.hypot(np.diff(x), np.diff(y)); cs = np.r_[0, np.cumsum(seg)]
        lon = np.array([p[0] for p in e['ll']]); lat = np.array([p[1] for p in e['ll']])
        n = max(2, int(math.ceil(e['len'] / 10)) + 1); s = np.linspace(0, cs[-1], n)
        h = terrain.sample(np.interp(s, cs, lon), np.interp(s, cs, lat))
        if e['bridge'] or e['tunnel'] or e['hw'] == 'steps':
            h = np.r_[h[0], np.full(n - 2, np.nan), h[-1]]
        known = np.isfinite(h)
        e['h_known'] = bool((known[0] and known[-1]) or known.mean() >= 0.6)
        if known.sum() >= 2:
            h = np.interp(s, s[known], h[known])  # 中间缺失线性补；两端外推为常值
        else:
            h = np.zeros(n)
            e['h_known'] = False
        ds = np.diff(s); dh = np.diff(h); g = dh / np.maximum(ds, 1e-6)
        L = max(ds.sum(), 1e-6)
        e['rise'] = float(h[-1] - h[0]); e['ascent'] = float(np.maximum(dh, 0).sum()); e['descent'] = float(-np.minimum(dh, 0).sum())
        e['h'] = (float(h[0]), float(h[-1]))
        if e['hw'] == 'steps':
            e['F'] = {'+': np.zeros(3), '-': np.zeros(3)}
            up = e['incline'] if e['incline'] in ('up', 'down') else ('up' if e['rise'] > 1 else 'down' if e['rise'] < -1 else '')
            e['steps_dir'] = {'+': up, '-': {'up': 'down', 'down': 'up'}.get(up, '')}
        else:
            e['F'] = {'+': (grade_features(g) * ds[:, None]).sum(0) / L, '-': (grade_features(-g) * ds[:, None]).sum(0) / L}
            e['steps_dir'] = {'+': '', '-': ''}
        e['grade'] = e['rise'] / e['len']


def road_class(e, d, family=None):
    """回归中的道路类别（同类共享先验效应）。台阶按上/下行分开；骑行可被用户规则改成 ramp（可骑）/ push（推车）。"""
    if family == 'bike':
        act = e.get('rules', {}).get(('bike', d))
        if act in ('ride', 'push'):
            return 'ramp' if act == 'ride' else 'push'
    hw = e['hw']
    if hw == 'steps':
        return 'steps_' + (e['steps_dir'][d] or 'flat')
    if hw in ('footway', 'pedestrian', 'path', 'living_street'):
        return 'footbridge' if e['bridge'] else 'covered' if e['covered'] else 'footway'
    if hw == 'cycleway':
        return 'cycleway'
    if hw in ('service', 'residential', 'unclassified', 'link'):
        return hw if hw != 'unclassified' else 'service'
    return 'major'   # secondary/tertiary 及其连接线


def densify(edges, step=2.0):
    P, E, S = [], [], []
    for e in edges:
        x, y = e['x'], e['y']; seg = np.hypot(np.diff(x), np.diff(y)); cs = np.r_[0, np.cumsum(seg)]
        n = max(2, int(cs[-1] / step) + 1); s = np.linspace(0, cs[-1], n)
        P.append(np.c_[np.interp(s, cs, x), np.interp(s, cs, y)]); E.append(np.full(n, e['id'])); S.append(s * e['len'] / max(cs[-1], 1e-9))
    return np.vstack(P), np.concatenate(E), np.concatenate(S)


def tangent(e, s):
    """路段在弧长 s 处的单位方向（a→b）。"""
    x, y = e['x'], e['y']; seg = np.hypot(np.diff(x), np.diff(y)); cs = np.r_[0, np.cumsum(seg)]
    i = int(np.clip(np.searchsorted(cs, s * cs[-1] / e['len']) - 1, 0, len(seg) - 1))
    return np.array([x[i + 1] - x[i], y[i + 1] - y[i]]) / max(seg[i], 1e-9)


def point_at(e, s):
    x, y = e['x'], e['y']; seg = np.hypot(np.diff(x), np.diff(y)); cs = np.r_[0, np.cumsum(seg)]
    q = s * cs[-1] / e['len']
    return float(np.interp(q, cs, x)), float(np.interp(q, cs, y))


def lonlat_at(e, s0, s1):
    """路段上 s0→s1 的子线（经纬度），方向随 s0/s1 顺序。"""
    x, y = e['x'], e['y']; seg = np.hypot(np.diff(x), np.diff(y)); cs = np.r_[0, np.cumsum(seg)] * e['len'] / max(seg.sum(), 1e-9)
    lon = np.array([p[0] for p in e['ll']]); lat = np.array([p[1] for p in e['ll']])
    lo, hi = sorted((s0, s1)); inner = [i for i in range(len(cs)) if lo < cs[i] < hi]
    ss = [lo] + [cs[i] for i in inner] + [hi]
    pts = [(float(np.interp(q, cs, lon)), float(np.interp(q, cs, lat))) for q in ss]
    return pts if s0 <= s1 else pts[::-1]


def project(e, px, py):
    """点到路段折线的精确投影：(弧长 s, 距离)。与网页端 JS 同一算法，保证两边规划一致。"""
    x, y = e['x'], e['y']; best = (0.0, math.inf); acc = 0.0
    scale = e['len'] / max(float(np.hypot(np.diff(x), np.diff(y)).sum()), 1e-9)
    for i in range(len(x) - 1):
        dx, dy = x[i + 1] - x[i], y[i + 1] - y[i]; L2 = dx * dx + dy * dy; L = math.sqrt(L2)
        u = 0.0 if L2 == 0 else min(1.0, max(0.0, ((px - x[i]) * dx + (py - y[i]) * dy) / L2))
        d = math.hypot(px - x[i] - u * dx, py - y[i] - u * dy)
        if d < best[1]:
            best = ((acc + u * L) * scale, d)
        acc += L
    return best
