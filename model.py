"""通行耗时模型：轨迹 → 路段×方向观测 → 稳健对数耗时回归。

响应 y = log(秒/米)，按“某条记录在某路段某方向”汇总（约每 40 m 一份权重、单条记录单路段单方向最多 5 份，
绕圈不会重复计权）。模型与 DirMap travel_model（foot-efficiency-corridor-v2）同构：

  y = α[记录×方式]            每次记录的配速（不惩罚：慢的人不能被解释成路慢）
    + θ·坡度特征(路段,方向)    方向性坡度，向先验收缩（步行 Tobler，骑行平地）
    + c[道路类别]              同类道路共享效应（台阶按上行/下行分开）
    + u[路段]                  局部路段效应（向 0 收缩）
    + v[路段,方向]             方向残差（更强收缩；坡度已解释的部分不重复）
    + β[高峰×路型]             上下课高峰，车行道与步行空间（步道/天桥/台阶，人流拥挤）分开（向 0 收缩）
    + ε                        稳健 IRLS（Huber 型）

跑步与步行共享坡度/道路效应（DirMap 同样合并为 foot），各自有配速项；骑行单独拟合。
"""
import collections
import math

import numpy as np
from scipy.sparse import coo_matrix, diags, vstack
from scipy.sparse.linalg import lsqr

from network import road_class

FAMILY = {'walk': 'foot', 'run': 'foot', 'bike': 'bike'}
# 先验：坡度系数（每单位坡度的 log 耗时变化），与 DirMap PRIOR 相同；骑行与 DirMap 一样以平地为先验。
GRADE_PRIOR = {'foot': np.array([3.5, -3.5, 3.5]), 'bike': np.zeros(3)}
# 台阶先验（相对平路的 log 耗时）：步行上 0.55 m/s、下 0.75 m/s；骑行推车上 0.5、下 0.6 m/s（相对 3.4 m/s）
# 骑行走步道/人行区需让行人、过路缘：先验 2.6 m/s；人行天桥多为坡道+台阶，推车先验 1.5 m/s（实测推车过召南天桥 ≈1.9 m/s）。
CLASS_PRIOR = {'foot': {'steps_up': math.log(1.25 / .55), 'steps_down': math.log(1.25 / .75), 'steps_flat': math.log(1.25 / .65)},
               'bike': {'steps_up': math.log(3.4 / .5), 'steps_down': math.log(3.4 / .6), 'steps_flat': math.log(3.4 / .55),
                        'footway': math.log(3.4 / 2.6), 'covered': math.log(3.4 / 2.6), 'footbridge': math.log(3.4 / 1.5)}}
BIKE_NO = math.log(3.4 / 1.2)   # 标了禁止骑行的车行道：推车（步道已有更慢的先验，不重复）
PEN = dict(grade=4., cls=4., edge=2., dir=6., peak=4.)
PED = ('footway', 'footbridge', 'covered', 'cycleway', 'steps_up', 'steps_down', 'steps_flat')   # 步行空间：高峰人流


def peak_key(cls):
    return 'ped' if cls in PED else 'road'
VMAX = {'walk': 3.0, 'run': 7.0, 'bike': 12.0}
UNIT_M, W_CAP = 40.0, 200.0   # 约每 40 m 一份权重（≈ DirMap 50 m 通道一份），单条记录单路段最多 5 份


# ---------- 逐点方式 ----------
def smooth_speed(x, y, t, win=20):
    """以 ±win/2 秒窗的位移/时间估计速度，抑制 GPS 抖动。"""
    v = np.zeros(len(t)); cs = np.r_[0, np.cumsum(np.hypot(np.diff(x), np.diff(y)))]
    j0 = j1 = 0
    for i in range(len(t)):
        while t[j0] < t[i] - win / 2: j0 += 1
        while j1 + 1 < len(t) and t[j1 + 1] <= t[i] + win / 2: j1 += 1
        v[i] = (cs[j1] - cs[j0]) / max(t[j1] - t[j0], 1)
    return v


def _runs(mask):
    i, n = 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j + 1 < n and mask[j + 1]: j += 1
            yield i, j
            i = j + 1
        else:
            i += 1


def point_modes(label, x, y, t):
    """bike / walk / run / stop / None。骑行记录全程为骑行（推车与等灯算骑行通行的一部分）；
    步行记录中持续快跑段记为跑步；跑步记录只取其中持续步行段（≥45 s）作为步行样本。"""
    v = smooth_speed(x, y, t)
    m = np.array([None] * len(t), dtype=object)
    if label == 'bike':
        m[:] = 'bike'
    elif label in ('walk', 'run'):
        walkish = (v >= 0.45) & (v < 2.0)
        if label == 'walk':
            m[walkish | ((v >= 2.0) & (v < 2.3))] = 'walk'; m[v >= 2.3] = 'run'
        else:
            m[v >= 2.0] = 'run'
            for i, j in _runs(walkish):
                if t[j] - t[i] >= 45: m[i:j + 1] = 'walk'
    # 长停留（>30 s 慢于 0.35 m/s）不计入通行；短停（路口、让行）保留在所在路段
    for i, j in _runs(v < 0.35):
        if t[j] - t[i] > 30: m[i:j + 1] = 'stop'
    return m, v


# ---------- 观测 ----------
def traversals(track, x, y, modes, keep, states, links, period):
    """每对相邻匹配点之间的路网路径按长度分配时间，汇总到 (路段, 方向, 方式, 时段)。"""
    t = track['t']; acc = collections.defaultdict(lambda: [0.0, 0.0, math.inf])
    cur = None; used = rejected = 0.0
    for k in range(1, len(keep)):
        i0, i1 = keep[k - 1], keep[k]; dt = t[i1] - t[i0]; link = links[k - 1]
        seg_m = modes[i0:i1 + 1]
        m = seg_m[0]
        if dt <= 0 or dt > 15 or m not in VMAX or any(q != m for q in seg_m) or link is None:
            cur = None; continue
        dr, pieces = link
        gc = math.hypot(x[i1] - x[i0], y[i1] - y[i0])
        if dr / dt > VMAX[m] or dr > 1.6 * gc + 12:      # 匹配绕路或跳点，不用
            rejected += dt; cur = None; continue
        per = period(t[i0])
        if dr < 0.3:                                     # 原地短停：时间计入当前路段
            if cur is not None and cur[2] == m:
                a = acc[cur + (per,)]; a[1] += dt; used += dt
            continue
        for e, s0, s1 in pieces:
            L = abs(s1 - s0); d = '+' if s1 > s0 else '-'
            a = acc[(e, d, m, per)]; a[0] += L; a[1] += dt * L / dr; a[2] = min(a[2], t[i0])
            cur = (e, d, m)
        used += dt
    return acc, used, rejected


def rows_from(track_id, acc, edges, commute):
    out = []
    for (e, d, m, per), (L, s, t0) in acc.items():
        if L < 10 or s < 4 or not (0.25 <= L / s <= VMAX[m]):
            continue
        E = edges[e]
        out.append(dict(track=track_id, edge=e, dir=d, mode=m, family=FAMILY[m], period=per, m=L, s=s, t0=t0,
                        cls=road_class(E, d), F=E['F'][d], commute=commute, bike_no=E['bike_no']))
    return out


# ---------- 拟合 ----------
class Fit:
    def __init__(self, rows, family, edges, exclude_track=None):
        self.family, self.edges = family, edges
        rows = [r for r in rows if r['family'] == family and r['track'] != exclude_track]
        self.rows = rows
        self.prior = GRADE_PRIOR[family]; self.cprior = CLASS_PRIOR[family]
        self.ok = len(rows) >= 12 and len({r['track'] for r in rows}) >= 2
        self.theta = self.prior.copy(); self.cls = {}; self.u = {}; self.v = {}; self.beta = {'road': 0.0, 'ped': 0.0}; self.alpha = {}
        self.support = collections.defaultdict(set); self.support_dir = collections.defaultdict(set)
        for r in rows:
            self.support[r['edge']].add(r['track']); self.support_dir[(r['edge'], r['dir'])].add(r['track'])
        if self.ok:
            self._solve()

    def _offset(self, cls, F, bike_no):
        o = self.cprior.get(cls, 0.0) + (0.0 if cls.startswith('steps') else float(F @ self.prior))
        return o + (BIKE_NO if self.family == 'bike' and bike_no and cls in ('service', 'residential', 'major', 'link') else 0.0)

    def _solve(self):
        rows = self.rows; n = len(rows)
        ep = {}; cl = {}; ed = {}; edd = {}
        for r in rows:
            ep.setdefault((r['track'], r['mode']), len(ep)); cl.setdefault(r['cls'], len(cl))
            ed.setdefault(r['edge'], len(ed)); edd.setdefault((r['edge'], r['dir']), len(edd))
        P, C, U, V = len(ep), len(cl), len(ed), len(edd)
        o_th, o_c, o_u, o_v, o_b = P, P + 3, P + 3 + C, P + 3 + C + U, P + 3 + C + U + V
        ncol = o_b + 2
        y = np.array([math.log(r['s'] / r['m']) - self._offset(r['cls'], r['F'], r['bike_no']) for r in rows])
        F = np.array([r['F'] if not r['cls'].startswith('steps') else np.zeros(3) for r in rows]) / .05
        ri, ci, vv = [], [], []
        for i, r in enumerate(rows):
            for col, val in ((ep[(r['track'], r['mode'])], 1.0), (o_c + cl[r['cls']], 1.0), (o_u + ed[r['edge']], 1.0),
                             (o_v + edd[(r['edge'], r['dir'])], 1.0),
                             (o_b + (peak_key(r['cls']) == 'ped'), 1.0 if r['period'] == 'peak' else 0.0),
                             (o_th, F[i, 0]), (o_th + 1, F[i, 1]), (o_th + 2, F[i, 2])):
                if val:
                    ri.append(i); ci.append(col); vv.append(val)
        X = coo_matrix((vv, (ri, ci)), shape=(n, ncol)).tocsr()
        w0 = np.array([min(r['m'], W_CAP) / UNIT_M for r in rows])
        pen = np.r_[np.zeros(P), np.full(3, PEN['grade']), np.full(C, PEN['cls']), np.full(U, PEN['edge']),
                    np.full(V, PEN['dir']), PEN['peak'], PEN['peak']]
        R = diags(np.sqrt(pen), format='csr')
        robust = np.ones(n)
        for _ in range(7):
            w = np.sqrt(w0 * robust)
            A = vstack((X.multiply(w[:, None]), R), format='csr')
            beta = lsqr(A, np.r_[y * w, np.zeros(ncol)], atol=1e-9, btol=1e-9, iter_lim=2000)[0]
            res = y - X @ beta
            scale = max(.1, 1.4826 * float(np.median(np.abs(res - np.median(res)))))
            robust = np.minimum(1., 1.5 * scale / np.maximum(np.abs(res), 1e-9))
        self.residual_mad = scale
        self.alpha = {k: float(beta[i]) for k, i in ep.items()}
        self.theta = self.prior + beta[o_th:o_th + 3] / .05
        self.cls = {k: float(beta[o_c + i]) for k, i in cl.items()}
        self.u = {k: float(beta[o_u + i]) for k, i in ed.items()}
        self.v = {k: float(beta[o_v + i]) for k, i in edd.items()}
        self.beta = {'road': float(beta[o_b]), 'ped': float(beta[o_b + 1])}
        self.n_peak = {k: int(sum(r['period'] == 'peak' and peak_key(r['cls']) == k for r in rows)) for k in ('road', 'ped')}
        self.pace_w = collections.Counter()
        for r, ww in zip(rows, w0):
            self.pace_w[(r['track'], r['mode'])] += ww

    def base(self, mode, tracks=None):
        """基准 log(秒/米)：指定记录（默认全部）该方式配速项的加权平均。"""
        ks = [k for k in self.alpha if k[1] == mode and (tracks is None or k[0] in tracks)] or [k for k in self.alpha if k[1] == mode]
        if not ks:
            return None
        w = np.array([self.pace_w[k] for k in ks]); a = np.array([self.alpha[k] for k in ks])
        return float((w * a).sum() / w.sum())

    def road_log(self, e, d, peak=False, parts=('grade', 'cls', 'edge', 'dir', 'peak')):
        """路段 e 方向 d 相对平地基准的 log 耗时（不含配速项）。"""
        E = self.edges[e]; cls = road_class(E, d)
        out = self.cprior.get(cls, 0.0)
        if 'grade' in parts and not cls.startswith('steps'):
            out += float(E['F'][d] @ self.theta)
        if 'cls' in parts:
            out += self.cls.get(cls, 0.0)
        if self.family == 'bike' and E['bike_no'] and cls in ('service', 'residential', 'major', 'link'):
            out += BIKE_NO
        if 'edge' in parts:
            out += self.u.get(e, 0.0)
        if 'dir' in parts:
            out += self.v.get((e, d), 0.0)
        if peak and 'peak' in parts:
            out += self.beta[peak_key(cls)]
        return out


def grade_curve(theta, prior, grades=np.linspace(-.08, .08, 33)):
    from network import grade_features
    f = grade_features(grades)
    return [dict(grade=round(float(g), 3), learned=round(float(np.exp(f[i] @ theta)), 4), prior=round(float(np.exp(f[i] @ prior)), 4))
            for i, g in enumerate(grades)]
