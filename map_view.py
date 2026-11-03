"""生成可直接双击、离线查看的单文件地图。"""
import base64
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent


def load_basemap(reference_dir, bbox):
    """复用分析使用的 OSM 参照：建筑、水体、绿地，以及不参与规划的快速路（参与规划的路网由页面单独绘制）。"""
    from network import SKIP_HW
    features, seen = [], set()
    for name in ('yuehai-osm.json', 'yuehai-osm-extra.json'):
        elements = json.loads((Path(reference_dir) / name).read_text(encoding='utf-8'))['elements']
        for element in elements:
            tags = element.get('tags', {})
            if element['type'] != 'way' or element['id'] in seen:
                continue
            if not any(k in tags for k in ('highway', 'building', 'natural', 'landuse', 'leisure')):
                continue
            if 'highway' in tags and tags['highway'] not in SKIP_HW:
                continue
            points = [[p['lon'], p['lat']] for p in element.get('geometry', []) if p]
            if len(points) < 2 or not any(bbox[0] < lon < bbox[1] and bbox[2] < lat < bbox[3]
                                         for lon, lat in points):
                continue
            seen.add(element['id'])
            polygon = (len(points) >= 4 and points[0] == points[-1] and tags.get('area') != 'no'
                       and any(k in tags for k in ('building', 'natural', 'landuse', 'leisure')))
            kind = ('water' if tags.get('natural') == 'water' else
                    'building' if 'building' in tags else 'road' if 'highway' in tags else 'green')
            features.append(dict(
                type='Feature',
                properties=dict(kind=kind, highway=tags.get('highway', ''),
                                name=tags.get('name:zh', tags.get('name', ''))),
                geometry=dict(type='Polygon' if polygon else 'LineString',
                              coordinates=[[[round(a, 6), round(b, 6)] for a, b in points]] if polygon else [[round(a, 6), round(b, 6)] for a, b in points])))
    # 先画面，再画道路，避免建筑遮住道路。
    features.sort(key=lambda f: f['geometry']['type'] == 'LineString')
    return dict(type='FeatureCollection', features=features)


def render_map(data, bbox):
    """data 为 report.map_data 的结果；Leaflet、图标、底图与数据全部内嵌，离线可用。"""
    import config
    vendor = ROOT / 'vendor' / 'leaflet'

    def image_url(name):
        encoded = base64.b64encode((vendor / 'images' / name).read_bytes()).decode('ascii')
        return 'data:image/png;base64,' + encoded

    css = (vendor / 'leaflet.css').read_text(encoding='utf-8')
    css = re.sub(r'url\(images/([^)]+)\)', lambda match: 'url(' + image_url(match[1]) + ')', css)
    js = (vendor / 'leaflet.js').read_text(encoding='utf-8')
    js = re.sub(r'//# sourceMappingURL=.*', '', js)
    js = '/*\n' + (vendor / 'LICENSE').read_text(encoding='utf-8') + '\n*/\n' + js
    data = dict(data, basemap=load_basemap(Path(config.load()['osm_reference']).expanduser(), bbox))
    # 数据嵌入 script 标签；转义 < 以免地名中的 HTML 结束标签截断脚本。
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':'), default=float).replace('<', '\\u003c')
    html = (ROOT / 'map_template.html').read_text(encoding='utf-8')
    return (html.replace('/*__LEAFLET_CSS__*/', css)
                .replace('/*__LEAFLET_JS__*/', js)
                .replace('__MAP_DATA__', payload))
