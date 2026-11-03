"""有向路网规划：A→B 与 B→A 分别计算（坡度使两个方向耗时不同）。

弧代价 = 路段长度 × exp(基准 + 路段方向 log 耗时)。起终点投影到附近路段（可落在路段中间），
楼宇中心到投影点按步行计（门到路）。可达圈用一对多 Dijkstra；“到达这里”在反向图上计算。
"""
import heapq
import math

import numpy as np
from scipy.spatial import cKDTree

from network import densify, lonlat_at, project
from tracks import xy

SNAP_K, SNAP_R, SNAP_SLACK, ACCESS_DETOUR = 3, 120.0, 25.0, 1.3


class Planner:
    def __init__(self, edges, sec_per_m, walk_speed, allowed):
        """sec_per_m[(e,d)] 每米耗时；allowed(e,d) 是否可通行；walk_speed 门到路的步行速度。"""
        self.edges, self.walk = edges, walk_speed
        self.cost = {}
        self.out = {}; self.inc = {}
        for e in edges:
            for d, (u, v) in (('+', (e['a'], e['b'])), ('-', (e['b'], e['a']))):
                if not allowed(e, d):
                    continue
                c = e['len'] * sec_per_m[(e['id'], d)]
                self.cost[(e['id'], d)] = c
                self.out.setdefault(u, []).append((v, c, e['id'], d)); self.inc.setdefault(v, []).append((u, c, e['id'], d))
        P, PE, PS = densify(edges, 2.0)
        self.P, self.PE, self.PS, self.tree = P, PE, PS, cKDTree(P)

    def usable(self, e):
        return (e, '+') in self.cost or (e, '-') in self.cost

    def snap(self, lon, lat):
        """附近最多 3 条不同路段上的投影：[(edge, s, 门到路距离)]。只保留比最近路段远不超过 25 m 的候选，
        门到路按直线 × 1.3 计步行（防止把“离网直线步行”当捷径）。"""
        px, py = xy(lon, lat)
        best = {}
        for e in {int(self.PE[i]) for i in self.tree.query_ball_point([px, py], SNAP_R + 1.5)}:
            s, d = project(self.edges[e], px, py)
            if d <= SNAP_R and self.usable(e):
                best[e] = (s, d)
        if not best:
            raise ValueError(f'({lon},{lat}) 附近 {SNAP_R} m 内没有道路')
        c = sorted(best.items(), key=lambda kv: kv[1][1])[:SNAP_K]
        return [(e, s, d * ACCESS_DETOUR) for e, (s, d) in c if d <= c[0][1][1] + SNAP_SLACK]

    def _seeds(self, snaps, reverse=False):
        """虚拟起点 → 所在路段两端节点的初始代价（reverse=True 时为“节点 → 虚拟终点”）。"""
        out = []
        for e, s, acc in snaps:
            E = self.edges[e]; a0 = acc / self.walk
            # 正向搜索：从 s 走到 a 是 '-' 方向，走到 b 是 '+'；反向搜索（求到达此点）方向相反
            for node, part, d in ((E['a'], s, '-' if not reverse else '+'), (E['b'], E['len'] - s, '+' if not reverse else '-')):
                if (e, d) in self.cost:
                    out.append((a0 + self.cost[(e, d)] * part / E['len'], node, (e, s, d)))
        return out

    def tree_from(self, snaps, reverse=False, limit=math.inf):
        """一对多：返回 {node: (秒, 前驱节点, 弧)}；reverse=True 求各节点到该点的耗时。"""
        adj = self.inc if reverse else self.out
        dist = {}; pq = []
        for c, node, seed in self._seeds(snaps, reverse):
            if c < dist.get(node, (math.inf,))[0]:
                dist[node] = (c, None, seed); heapq.heappush(pq, (c, node))
        done = set()
        while pq:
            c, u = heapq.heappop(pq)
            if u in done or c > limit:
                continue
            done.add(u)
            for v, w, e, d in adj.get(u, ()):
                nc = c + w
                if nc < dist.get(v, (math.inf,))[0]:
                    dist[v] = (nc, u, (e, d)); heapq.heappush(pq, (nc, v))
        return dist

    def route(self, src, dst, S=None, dist=None, T=None):
        """src/dst = (lon, lat)。返回耗时、距离、爬升、几何与经过的 (路段, 方向)。
        同一起点多终点时传入 S/dist（起点投影与一对多结果）复用。"""
        S = S or self.snap(*src); T = T or self.snap(*dst)
        dist = dist if dist is not None else self.tree_from(S)
        best = None
        for e, s, acc in T:
            E = self.edges[e]
            for node, part, d in ((E['a'], s, '+'), (E['b'], E['len'] - s, '-')):
                if node in dist and (e, d) in self.cost:
                    c = dist[node][0] + self.cost[(e, d)] * part / E['len'] + acc / self.walk
                    if best is None or c < best[0]:
                        best = (c, node, (e, s, d), acc)
        # 起终点在同一路段上时直接沿路段走
        for e1, s1, a1 in S:
            for e2, s2, a2 in T:
                d = '+' if s2 >= s1 else '-'
                if e1 == e2 and (e1, d) in self.cost:
                    E = self.edges[e1]
                    c = (a1 + a2) / self.walk + self.cost[(e1, d)] * abs(s2 - s1) / E['len']
                    if best is None or c < best[0]:
                        best = (c, None, (e1, s1, s2, d), a1 + a2, 'direct')
        if best is None:
            return None
        if len(best) == 5:
            c, _, (e, s1, s2, d), acc, _ = best
            arcs = [(e, d, abs(s2 - s1))]; geo = lonlat_at(self.edges[e], s1, s2)
            return self._summary(c, arcs, geo, acc)
        c, node, (e_end, s_end, d_end), acc_end = best
        arcs, geo = [], []
        E = self.edges[e_end]
        part = s_end if d_end == '+' else E['len'] - s_end
        arcs.append((e_end, d_end, part)); geo = lonlat_at(E, 0.0 if d_end == '+' else E['len'], s_end)
        u = node
        while True:
            c_u, prev, via = dist[u]
            if prev is None:
                e, s, d = via; E = self.edges[e]
                arcs.append((e, d, E['len'] - s if d == '+' else s))
                geo = lonlat_at(E, s, E['len'] if d == '+' else 0.0) + geo
                acc0 = next(a for ee, ss, a in S if ee == e and ss == s)
                break
            e, d = via; E = self.edges[e]
            arcs.append((e, d, E['len']))
            geo = (E['ll'] if d == '+' else E['ll'][::-1]) + geo
            u = prev
        return self._summary(c, arcs[::-1], geo, acc0 + acc_end)

    def reach(self, dist, limit, reverse=False):
        """一对多结果 → 各路段在 limit 秒内可达的长度（两端分别推进，合计不超过路段长）。
        reverse=True（到达圈）时从端点向路段内部“倒推”，用的是走向该端点的方向。"""
        out = {}
        for e in self.edges:
            L = e['len']; r = 0.0
            for node, d in ((e['a'], '-' if reverse else '+'), (e['b'], '+' if reverse else '-')):
                if node in dist and (e['id'], d) in self.cost and dist[node][0] < limit:
                    r += min(L, (limit - dist[node][0]) / (self.cost[(e['id'], d)] / L))
            if r > 0:
                out[e['id']] = min(L, r)
        return out

    def _summary(self, c, arcs, geo, access_m):
        L = sum(p for _, _, p in arcs)
        up = down = 0.0
        for e, d, p in arcs:
            E = self.edges[e]; f = p / E['len']
            a, b = (E['ascent'], E['descent']) if d == '+' else (E['descent'], E['ascent'])
            up += a * f; down += b * f
        return dict(time_s=c, dist_m=L + access_m, net_m=L, ascent_m=up, descent_m=down, arcs=arcs, geo=geo)
