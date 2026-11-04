"""核心逻辑的小测试：解析、自动识别、地图匹配方向、坡度方向性、规划一致性。python3 -m pytest tests -q"""
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import matching  # noqa: E402
import model  # noqa: E402
import network  # noqa: E402
import tracks  # noqa: E402
from analyze import allowed_for  # noqa: E402
from routing import Planner  # noqa: E402


def line_edges(n=5, seg=50.0, rise=0.0):
    """东西向直路，n 段，每段 seg 米，向东每段上升 rise 米。"""
    edges = []
    kx = 111320 * math.cos(math.radians(tracks.LAT0))
    for i in range(n):
        lon0, lon1 = tracks.LON0 + i * seg / kx, tracks.LON0 + (i + 1) * seg / kx
        ll = [(lon0, tracks.LAT0), (lon1, tracks.LAT0)]
        x, y = tracks.xy([p[0] for p in ll], [p[1] for p in ll])
        g = rise / seg
        edges.append(dict(id=i, way=i, hw='service', name='', oneway=False, ll=ll, x=x, y=y, len=seg, a=('n', i), b=('n', i + 1),
                          bridge=False, tunnel=False, incline='', covered=False, foot_no=False, bike_no=False,
                          rise=rise, ascent=max(rise, 0), descent=max(-rise, 0), h=(0, rise), h_known=True, grade=g,
                          F={'+': network.grade_features(g), '-': network.grade_features(-g)}, steps_dir={'+': '', '-': ''}))
    return edges


def test_parse_kml(tmp_path):
    p = tmp_path / 'a.kml'
    p.write_text('<kml><gx:Track><when>2026-11-02T06:00:01Z</when><when>2026-11-02T06:00:00Z</when><when>2026-11-02T06:00:02Z</when>'
                 '<gx:coord>113.9 22.5 10</gx:coord><gx:coord>113.91 22.5 11</gx:coord><gx:coord>113.92 22.5 12</gx:coord></gx:Track></kml>')
    t, lon, lat, h = tracks.parse(p)
    assert list(np.diff(t)) == [1, 1] and lon[0] == 113.91   # 按时间排序


def test_classify_walk_and_run():
    t = np.arange(0, 600, 1.0); kx = 111320 * math.cos(math.radians(tracks.LAT0))
    walk = tracks.classify(t, tracks.LON0 + 1.2 * t / kx, np.full_like(t, tracks.LAT0))
    run = tracks.classify(t, tracks.LON0 + 3.2 * t / kx, np.full_like(t, tracks.LAT0))
    assert walk[0] == 'walk' and run[0] == 'run'


def test_match_direction_and_pieces():
    edges = line_edges()
    P, PE, PS = network.densify(edges); from scipy.spatial import cKDTree
    t = np.arange(0, 200, 1.0); x = 1.2 * t + 5; y = np.random.default_rng(0).normal(0, 3, len(t))
    keep = matching.thin(x, y, t)
    states, links = matching.match(x[keep], y[keep], t[keep], cKDTree(P), PE, PS, edges, matching.Router(edges))
    assert all(s is not None for s in states)
    pieces = [p for l in links if l for p in l[1]]
    assert all(p[2] >= p[1] for p in pieces)               # 全部为正向（向东）
    assert abs(sum(p[2] - p[1] for p in pieces) - (x[keep][-1] - x[keep][0])) < 10


def test_grade_direction_asymmetry_and_planner_consistency():
    edges = line_edges(rise=2.5)                            # 5% 上坡向东
    rows = []
    for k in range(4):                                      # 四条记录：上坡慢、下坡快
        for e in edges:
            for d, v in (('+', 1.0), ('-', 1.4)):
                rows.append(dict(track=f't{k}', edge=e['id'], dir=d, mode='walk', family='foot', period='off', m=50.0, s=50.0 / v,
                                 t0=0.0, cls='service', F=e['F'][d], commute=True, bike_no=False))
    f = model.Fit(rows, 'foot', edges)
    assert f.road_log(0, '+') > f.road_log(0, '-')          # 上坡方向耗时更长
    base = f.base('walk')
    sp = {(e['id'], d): math.exp(base + f.road_log(e['id'], d)) for e in edges for d in '+-'}
    pl = Planner(edges, sp, 1.2, lambda e, d: True)
    a, b = edges[0]['ll'][0], edges[-1]['ll'][-1]
    up, down = pl.route(a, b), pl.route(b, a)
    assert up['time_s'] > down['time_s'] and abs(up['net_m'] - 250) < 1 and up['ascent_m'] > 12


def test_commute_base_weights_trips_equally():
    """同样是一次出行，长记录的观测量不能让它主导通勤配速。"""
    edges = line_edges(6)
    rows = []
    for track, length, speed in [('short', 50, 2), ('long', 500, 8)]:
        for e in edges:
            rows.append(dict(track=track, edge=e['id'], dir='+', mode='bike', family='bike', period='off',
                             m=length, s=length / speed, t0=0, cls='service', F=e['F']['+'], commute=True, bike_no=False))
    fit = model.Fit(rows, 'bike', edges)
    assert fit.ok
    assert fit.pace_w[('long', 'bike')] > fit.pace_w[('short', 'bike')]
    assert math.exp(-fit.base('bike')) == pytest.approx(4)  # 两次对数配速等权，速度为几何平均
    assert math.exp(-fit.base('bike', {'short'})) == pytest.approx(2)
    assert fit.base('bike', {'missing'}) == pytest.approx(fit.base('bike'))
    assert fit.base('walk') is None


@pytest.mark.parametrize('peak_ped_speed', [3, 8])
def test_peak_cannot_make_roads_faster(peak_ped_speed):
    """高峰记录整体骑得快由记录配速解释；步行空间高峰效应只能增加耗时。"""
    edges = line_edges(6)
    for e in edges[3:]:
        e['hw'] = 'footway'
    rows = []
    for track, per, road_speed, ped_speed in [('off', 'off', 4, 2), ('peak', 'peak', 8, peak_ped_speed)]:
        for e in edges:
            speed = ped_speed if e['hw'] == 'footway' else road_speed
            for d in '+-':
                rows.append(dict(track=track, edge=e['id'], dir=d, mode='bike', family='bike', period=per,
                                 m=100, s=100 / speed, t0=0, cls=e['hw'], F=e['F'][d], commute=True, bike_no=False))
    fit = model.Fit(rows, 'bike', edges)
    assert fit.ok and fit.beta['road'] == 0
    if peak_ped_speed == 3:
        assert fit.beta['ped'] > 0
    else:
        assert fit.beta['ped'] == pytest.approx(0, abs=1e-7)
    for e in edges:
        for d in '+-':
            assert fit.road_log(e['id'], d, peak=True) >= fit.road_log(e['id'], d)


def test_block_rule_preserves_other_direction_and_mode():
    edges = line_edges(8)
    for e in edges:
        e['rules'] = {}
    edges[3]['rules'][('bike', '+')] = 'block'
    costs = {(e['id'], d): 1 for e in edges for d in '+-'}
    a, b = edges[0]['ll'][0], edges[-1]['ll'][-1]
    bike = Planner(edges, costs, 1, allowed_for('bike'))
    walk = Planner(edges, costs, 1, allowed_for('walk'))
    assert bike.route(a, b) is None
    assert bike.route(b, a) is not None
    assert walk.route(a, b) is not None
    assert walk.route(b, a) is not None


@pytest.mark.parametrize('reverse, expected', [(False, 50), (True, 100)])
def test_isochrone_starts_inside_oneway_edge(reverse, expected):
    edges = line_edges(1, seg=1000)
    p = Planner(edges, {(0, '+'): 1, (0, '-'): 2}, 1, lambda e, d: d == '+')
    snaps = [(0, 950, 0)]
    dist = p.tree_from(snaps, reverse=reverse, limit=100)
    assert p.reach(dist, 100, reverse=reverse, snaps=snaps) == {0: pytest.approx(expected)}


def test_isochrone_merges_overlaps_and_charges_access_walk():
    edges = line_edges(1, seg=1000)
    p = Planner(edges, {(0, '+'): 1, (0, '-'): 2}, 1, lambda e, d: True)
    # 同一路段的重叠来源不能重复计长度；门到路需先步行 20 秒。
    snaps = [(0, 400, 20), (0, 410, 20)]
    dist = p.tree_from(snaps, limit=100)
    assert p.reach(dist, 100, snaps=snaps) == {0: pytest.approx(130)}
    assert p.reach(dist, 20, snaps=snaps) == {}
    assert p.reach(p.tree_from(snaps), 3000, snaps=snaps) == {0: pytest.approx(1000)}
