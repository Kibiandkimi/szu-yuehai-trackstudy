"""HMM 地图匹配（Newson & Krumm 2009 思路）+ 路段通行分解。

- 发射：到候选路段的垂距（σ≈7 m）+ 行进方向与路段切线的夹角（移动时才计，双向道路用 |cos|）。
- 转移：|路网距离 − GPS 直线距离| / β，路网距离用有截断的 Dijkstra（节点间距离缓存）；
  比旧版“相邻即可”更能区分紧邻的平行路，也不会在路口误跳到垂直道路。
- 候选位置的位移与 GPS 位移方向相反（>2 m）时加罚，避免路口处“先跳到下一段再退回”的假倒退。
- None 状态 = 不在路网（操场、楼内、OSM 缺路），进出各有代价。
- 输出逐对匹配点之间的路网路径，按长度把时间分到经过的每条路段（含方向）。
"""
import heapq
import math

import numpy as np

from network import point_at, tangent

SIGMA, RADIUS, BETA, KAPPA = 7.0, 30.0, 8.0, 3.0
K_CAND = 6
OFF_EMIT = 0.5 * (RADIUS / SIGMA) ** 2 + 1.5
OFF_TRANS, JUMP, BACK = 3.0, 14.0, 4.0


class Router:
    """无向距离图（地图匹配只关心几何距离；单行限制在规划阶段处理）。"""
    def __init__(self, edges):
        self.edges = edges
        self.adj = {}
        for e in edges:
            self.adj.setdefault(e['a'], []).append((e['b'], e['len'], e['id']))
            self.adj.setdefault(e['b'], []).append((e['a'], e['len'], e['id']))
        self.cache = {}

    def tree(self, src, cutoff=500.0):
        t = self.cache.get(src)
        if t is not None:
            return t
        dist, pred = {src: 0.0}, {}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, 1e18) or d > cutoff:
                continue
            for v, w, eid in self.adj[u]:
                nd = d + w
                if nd < dist.get(v, 1e18):
                    dist[v] = nd; pred[v] = (u, eid); heapq.heappush(pq, (nd, v))
        self.cache[src] = (dist, pred)
        return dist, pred

    def node_path(self, src, dst):
        """src→dst 的 [(edge_id, from_node)]。"""
        dist, pred = self.tree(src)
        out, v = [], dst
        while v != src:
            u, eid = pred[v]; out.append((eid, u)); v = u
        return out[::-1]

    def between(self, c1, c2):
        """候选 (e,s) → (e,s) 的路网距离；返回 (dist, via)，via 供 pieces() 还原路径。不可达 (inf, None)。"""
        (e1, s1), (e2, s2) = c1, c2
        if e1 == e2:
            return abs(s2 - s1), 'same'
        E1, E2 = self.edges[e1], self.edges[e2]
        best = (math.inf, None)
        for n1, d1, end1 in ((E1['a'], s1, 0.0), (E1['b'], E1['len'] - s1, E1['len'])):
            dist = self.tree(n1)[0]
            for n2, d2, start2 in ((E2['a'], s2, 0.0), (E2['b'], E2['len'] - s2, E2['len'])):
                if n2 in dist and d1 + dist[n2] + d2 < best[0]:
                    best = (d1 + dist[n2] + d2, (n1, end1, n2, start2))
        return best

    def pieces(self, c1, c2, via):
        """路径片段 [(edge, s_from, s_to)]，s_from > s_to 表示逆向（b→a）通行。"""
        (e1, s1), (e2, s2) = c1, c2
        if via == 'same':
            return [(e1, s1, s2)]
        n1, end1, n2, start2 = via
        out = [(e1, s1, end1)]
        for eid, u in self.node_path(n1, n2):
            E = self.edges[eid]
            out.append((eid, 0.0, E['len']) if E['a'] == u else (eid, E['len'], 0.0))
        out.append((e2, start2, s2))
        return [p for p in out if abs(p[2] - p[1]) > 1e-6]


def candidates(x, y, tree, PE, PS, edges):
    idx = tree.query_ball_point([x, y], RADIUS)
    best = {}
    for k in idx:
        e = int(PE[k]); d = math.hypot(tree.data[k][0] - x, tree.data[k][1] - y)
        if e not in best or d < best[e][0]:
            best[e] = (d, float(PS[k]))
    return sorted(best.items(), key=lambda kv: kv[1][0])[:K_CAND]


def headings(x, y, t, win=6.0):
    """±win/2 s 的位移方向；位移 <4 m 视为静止（不用方向）。"""
    h = np.full((len(t), 2), np.nan)
    j0 = j1 = 0
    for i in range(len(t)):
        while t[j0] < t[i] - win / 2: j0 += 1
        while j1 + 1 < len(t) and t[j1 + 1] <= t[i] + win / 2: j1 += 1
        d = np.array([x[j1] - x[j0], y[j1] - y[j0]]); n = np.hypot(*d)
        if n >= 4:
            h[i] = d / n
    return h


def match(x, y, t, tree, PE, PS, edges, router):
    """返回 states[i] = (edge, s) 或 None，以及每对相邻匹配点的 (dist_route, pieces)。"""
    n = len(x)
    hd = headings(x, y, t)
    cand = []
    for i in range(n):
        cs = []
        for e, (d, s) in candidates(x[i], y[i], tree, PE, PS, edges):
            c = 0.5 * (d / SIGMA) ** 2
            if np.isfinite(hd[i, 0]):
                c += KAPPA * (1 - abs(float(hd[i] @ tangent(edges[e], s))))
            cs.append(((e, s), c, point_at(edges[e], s)))
        cs.append((None, OFF_EMIT, None))
        cand.append(cs)
    # Viterbi
    cost = [c[1] for c in cand[0]]; back = []; trans_cache = []
    for i in range(1, n):
        gx, gy = x[i] - x[i - 1], y[i] - y[i - 1]; gc = math.hypot(gx, gy)
        dt = max(t[i] - t[i - 1], 1e-3)
        cur, bk, tc = [], [], {}
        for j, (sj, ej, pj) in enumerate(cand[i]):
            best, arg = math.inf, 0
            for k, (sk, _, pk) in enumerate(cand[i - 1]):
                if sj is None and sk is None: tr = 0.0
                elif sj is None or sk is None: tr = OFF_TRANS
                else:
                    dr, via = router.between(sk, sj)
                    tc[(k, j)] = (dr, via)
                    tr = JUMP if not math.isfinite(dr) or dr / dt > 15 else min(abs(dr - gc) / BETA, JUMP)
                    mx, my = pj[0] - pk[0], pj[1] - pk[1]
                    if mx * gx + my * gy < 0 and math.hypot(mx, my) > 2:
                        tr += BACK
                c = cost[k] + tr
                if c < best: best, arg = c, k
            cur.append(best + ej); bk.append(arg)
        cost = cur; back.append(bk); trans_cache.append(tc)
    j = int(np.argmin(cost)); path = [0] * n; path[-1] = j
    for i in range(n - 1, 0, -1):
        j = back[i - 1][j]; path[i - 1] = j
    states = [cand[i][path[i]][0] for i in range(n)]
    links = []
    for i in range(1, n):
        a, b = states[i - 1], states[i]
        link = None
        if a is not None and b is not None:
            dr, via = trans_cache[i - 1][(path[i - 1], path[i])]
            if via is not None and dr / max(t[i] - t[i - 1], 1e-3) <= 15:
                link = (dr, router.pieces(a, b, via))
        links.append(link)
    return states, links


def thin(x, y, t, min_dt=2.5, min_d=6.0):
    """地图匹配用的点：间隔 ≥2.5 s 或 ≥6 m（1 Hz 原始点中相邻点几乎重复）。"""
    keep = [0]
    for i in range(1, len(t)):
        if t[i] - t[keep[-1]] >= min_dt or math.hypot(x[i] - x[keep[-1]], y[i] - y[keep[-1]]) >= min_d:
            keep.append(i)
    if keep[-1] != len(t) - 1:
        keep.append(len(t) - 1)
    return np.array(keep)
