"""地点：由 OSM 建筑名求质心（多栋取平均），分组用于 OD 与可达圈分析。"""
import json
from pathlib import Path

import config

# 名称: (分组, [OSM 建筑名])
GROUPS = {
    '宿舍区': {
        '斋区': ['红榴斋', '山茶斋', '海桐斋', '米兰斋', '桃李斋', '雨鹃斋', '风槐斋', '银桦斋', '红豆斋', '紫薇斋', '聚翰斋'],
        '乔区': ['乔林阁', '乔木阁', '乔相阁', '乔梧阁'],
        '南区': ['春笛', '夏筝', '冬筑', '高音', '低音'],
        '海山楼一带': ['海山楼', '海志楼', '海星楼', '海月楼', '海望楼', '海涛楼', '海青楼'],
    },
    '教学区': {
        '汇文楼': ['汇文楼H1号楼', '汇文楼H2号楼', '汇文楼H3号楼', '汇文楼H4号楼'],
        '科技楼': ['科技楼'],
        '致理楼': ['致理楼L1', '致理楼L2', '致理楼L3'],
        '致真楼': ['致真楼'],
        '致信楼': ['致信楼'],
        '计软楼': ['计算机与软件学院、电子科学与技术学院楼'],
    },
    '图书馆': {'图书馆南馆': ['图书馆南馆'], '图书馆北馆': ['图书馆北馆']},
    '餐厅': {'荔山餐厅': ['荔山餐厅', '荔天餐厅'], '文山湖餐厅': ['文山湖餐厅'], '南校区餐厅': ['南校区餐厅'], '听山餐厅': ['听山餐厅']},
    '运动': {'体育馆': ['元平体育馆'], '田径场': ['__stadium__']},
    '其他': {'学生活动中心': ['学生活动中心'], '校医院': ['校医院']},
}
TEACHING = list(GROUPS['教学区'])
DORMS = list(GROUPS['宿舍区'])


def _centroids():
    ref = Path(config.load()['osm_reference']).expanduser()
    out = {}
    for f in ('yuehai-osm.json', 'yuehai-osm-extra.json', 'yuehai-sports.json'):
        p = ref / f
        if not p.is_file():
            continue
        for e in json.loads(p.read_text(encoding='utf-8'))['elements']:
            t = e.get('tags', {}); g = [q for q in e.get('geometry') or [] if q]
            if not g:
                continue
            name = '__stadium__' if t.get('leisure') == 'stadium' else t.get('name:zh', t.get('name'))
            if name and name not in out:
                out[name] = (sum(q['lon'] for q in g) / len(g), sum(q['lat'] for q in g) / len(g))
    return out


def load():
    """{名称: dict(group, lon, lat)}"""
    c = _centroids(); out = {}
    for group, items in GROUPS.items():
        for name, members in items.items():
            pts = [c[m] for m in members if m in c]
            if not pts:
                print('地点缺少 OSM 建筑：', name); continue
            out[name] = dict(group=group, lon=round(sum(p[0] for p in pts) / len(pts), 6), lat=round(sum(p[1] for p in pts) / len(pts), 6))
    return out
