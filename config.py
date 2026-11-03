"""路径配置。个人路径（微信目录、服务器）放在不入库的 config.local.json，示例见 config.example.json。"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'            # 不入库：KML 副本、DirMap 同步数据
OUT = ROOT / 'out'              # 不入库：完整分析结果（含逐轨迹明细）
SITE = ROOT / 'docs'            # 入库：GitHub Pages，仅聚合结果

DEFAULTS = {
    # 扫描这些目录（递归）里的 .kml/.gpx，新文件按内容去重后复制进 data/kml/
    'kml_sources': [],
    # DirMap 参照路网（OSM）目录
    'osm_reference': str(Path.home() / 'Work/DirMap/data/reference'),
    # DirMap 服务器（同步方式标签、共享地形、DirMap 学到的坡度响应）
    'dirmap_ssh': [],
    'dirmap_elevation_dir': '/opt/dirmap/data/elevation',
    'dirmap_region': '深圳大学粤海校区',
}


def load():
    cfg = dict(DEFAULTS)
    local = ROOT / 'config.local.json'
    if local.is_file():
        cfg.update(json.loads(local.read_text(encoding='utf-8')))
    if os.environ.get('TRACKSTUDY_KML_SOURCES'):
        cfg['kml_sources'] = os.environ['TRACKSTUDY_KML_SOURCES'].split(os.pathsep)
    cfg['kml_sources'] = [os.path.expanduser(p) for p in cfg['kml_sources']]
    return cfg
