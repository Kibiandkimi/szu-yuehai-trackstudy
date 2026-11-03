"""只读访问 DirMap 共享轨迹库（服务器 /opt/dirmap/data/elevation，或本地镜像 data/dirmap/store）。

使用的接口（与 DirMap 会话约定，trackstudy 从不写入）：
  tracks.json            tracks[].{id, fileName, activity, enabled, kind, ownerName, sourceSha256}
  tracks/<id>.npz        points[:, :3] = lon, lat, h；times = Unix 秒（NaN = 无时间）；breaks = 分段起点
  models.json            regions[name=粤海校区].artifact（height 网格）；travelModels[regionId].artifact
"""
import hashlib
import json
from pathlib import Path

import numpy as np

import config

ACTIVITY = {'bicycle': 'bike', 'walk': 'walk', 'foot': 'walk', 'run': 'run', 'exclude': 'exclude', 'auto': None}


def root():
    return config.store()


def _json(name):
    p = root() / name
    return json.loads(p.read_text(encoding='utf-8')) if p.is_file() else None


def records():
    """可用于测速的 DirMap 轨迹记录（启用、非等高线、有时间、属于允许的账户）。"""
    src = _json('tracks.json')
    if not src:
        return []
    owners = set(config.load()['dirmap_owners'])
    return [t for t in src['tracks'] if t.get('enabled') and t.get('kind') != 'contours'
            and t.get('timedPointCount', t.get('pointCount', 0)) >= 3 and (not owners or t.get('ownerName') in owners)]


def load(record):
    """(t, lon, lat, h)，按时间排序并去掉无时间的点；文件缺失返回 None。"""
    p = root() / 'tracks' / f'{record["id"]}.npz'
    if not p.is_file():
        return None
    with np.load(p, allow_pickle=False) as z:
        pts = z['points']; times = z['times'] if 'times' in z.files else np.full(len(pts), np.nan)
    ok = np.isfinite(times) & np.isfinite(pts[:, :2]).all(1)
    if ok.sum() < 3:
        return None
    pts, times = pts[ok], times[ok]
    o = np.argsort(times, kind='stable')
    h = pts[o, 2] if pts.shape[1] > 2 else np.full(len(o), np.nan)
    return times[o], pts[o, 0], pts[o, 1], h


def region():
    """(元数据, artifact 路径) 或 (None, None)。"""
    m = _json('models.json')
    if not m:
        return None, None
    name = config.load()['dirmap_region']
    r = next((r for r in m.get('regions', []) if r['name'] == name), None)
    if r is None:
        return None, None
    meta = {k: v for k, v in r.items() if k not in ('trackIds', 'alignment')}
    meta.update(sourceVersion=m.get('sourceVersion'), builtAt=m.get('builtAt'))
    return meta, root() / r['artifact']


def travel_profiles():
    m = _json('models.json'); meta, _ = region()
    if not m or not meta:
        return None
    t = next((t for t in m.get('travelModels', []) if t['regionId'] == meta['id']), None)
    p = root() / t['artifact'] if t else None
    return json.loads(p.read_text(encoding='utf-8'))['profiles'] if p and p.is_file() else None


def fingerprint():
    """输入版本：tracks.json 与 models.json 内容哈希（变化即需重算）。"""
    h = hashlib.sha256()
    for name in ('tracks.json', 'models.json'):
        p = root() / name
        h.update(p.read_bytes() if p.is_file() else b'-')
    return h.hexdigest()[:16]
