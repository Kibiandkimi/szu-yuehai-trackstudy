"""路径配置。个人路径（微信目录、服务器）放在不入库的 config.local.json（或 TRACKSTUDY_CONFIG 指向的文件），示例见 config.example.json。"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'            # 不入库：KML 副本、DirMap 镜像

DEFAULTS = {
    # DirMap 共享轨迹库（与服务器 /opt/dirmap/data/elevation 同结构）：tracks.json + tracks/*.npz + models.json …
    # 服务器上直接指向 DirMap 目录（只读）；本地由 sync_dirmap.py 镜像到 data/dirmap/store
    'dirmap_store': str(DATA / 'dirmap' / 'store'),
    # 只用这些 DirMap 账户的轨迹（结果公开；其他贡献者的轨迹未获同意不使用）。空列表 = 全部
    'dirmap_owners': ['kibi'],
    # 本地 KML 库（data/kml）作为补充来源；与 DirMap 重复的（同 sha256）以 DirMap 为准
    'use_kml_library': True,
    # 扫描这些目录（递归）里的 .kml/.gpx，新文件按内容去重后复制进 data/kml/
    'kml_sources': [],
    # DirMap 参照路网（OSM）目录
    'osm_reference': str(Path.home() / 'Work/DirMap/data/reference'),
    # DirMap 服务器（同步方式标签、共享地形、DirMap 学到的坡度响应）
    'dirmap_ssh': [],
    'dirmap_elevation_dir': '/opt/dirmap/data/elevation',
    'dirmap_region': '深圳大学粤海校区',
    # 输出目录：完整结果（不公开）与公开站点
    'out_dir': str(ROOT / 'out'),
    'site_dir': str(ROOT / 'site'),
    # 服务器（serve.py）：在 builds_dir 构建，成功后把 site_link 原子指向新构建；state_dir 存状态与日志
    'builds_dir': str(ROOT / 'builds'),
    'site_link': str(ROOT / 'site-live'),
    'state_dir': str(ROOT / 'state'),
    'site_url': 'https://85.211.195.104/trackstudy/',
    'dirmap_url': 'https://85.211.195.104/#elevation',
    'update_readme': True,      # 本地重算时刷新 README“主要结果”；服务器上关闭
}


def load():
    cfg = dict(DEFAULTS)
    local = ROOT / 'config.local.json'
    if local.is_file():
        cfg.update(json.loads(local.read_text(encoding='utf-8')))
    if os.environ.get('TRACKSTUDY_KML_SOURCES'):
        cfg['kml_sources'] = os.environ['TRACKSTUDY_KML_SOURCES'].split(os.pathsep)
    if os.environ.get('TRACKSTUDY_CONFIG'):
        cfg.update(json.loads(Path(os.environ['TRACKSTUDY_CONFIG']).read_text(encoding='utf-8')))
    cfg['kml_sources'] = [os.path.expanduser(p) for p in cfg['kml_sources']]
    for env, k in (('TRACKSTUDY_SITE', 'site_dir'), ('TRACKSTUDY_OUT', 'out_dir')):
        if os.environ.get(env):
            cfg[k] = os.environ[env]
    for k in ('dirmap_store', 'osm_reference', 'out_dir', 'site_dir', 'builds_dir', 'site_link', 'state_dir'):
        cfg[k] = os.path.expanduser(cfg[k])
    return cfg


def out_dir():
    p = Path(load()['out_dir']); p.mkdir(parents=True, exist_ok=True); return p


def site_dir():
    p = Path(load()['site_dir']); p.mkdir(parents=True, exist_ok=True); return p


def store():
    return Path(load()['dirmap_store'])
