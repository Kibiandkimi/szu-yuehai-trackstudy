"""从 DirMap 服务器同步：轨迹方式标签、粤海校区共享地形、DirMap 学到的通行/坡度模型。

    python3 sync_dirmap.py

结果写到 data/dirmap/（不入库）。没有网络时分析仍可用上次同步的数据；从未同步则按平地计算并在结果中注明。
"""
import json
import os
import subprocess
import sys

import config

DST = config.DATA / 'dirmap'


def fetch(ssh, path):
    return subprocess.run(ssh + ['cat', path], check=True, capture_output=True, timeout=120).stdout


def main():
    cfg = config.load()
    ssh = [os.path.expanduser(a) for a in cfg['dirmap_ssh']]
    if not ssh:
        sys.exit('config.local.json 未配置 dirmap_ssh')
    root = cfg['dirmap_elevation_dir'].rstrip('/')
    DST.mkdir(parents=True, exist_ok=True)
    tracks = fetch(ssh, f'{root}/tracks.json')
    (DST / 'tracks.json').write_bytes(tracks)
    models = json.loads(fetch(ssh, f'{root}/models.json'))
    region = next(r for r in models['regions'] if r['name'] == cfg['dirmap_region'])
    (DST / 'terrain.npz').write_bytes(fetch(ssh, f'{root}/{region["artifact"]}'))
    meta = {k: v for k, v in region.items() if k not in ('trackIds', 'alignment')}
    meta['sourceVersion'] = models['sourceVersion']; meta['builtAt'] = models['builtAt']
    (DST / 'terrain.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
    travel = next((t for t in models.get('travelModels', []) if t['regionId'] == region['id']), None)
    if travel:
        (DST / 'travel.json').write_bytes(fetch(ssh, f'{root}/{travel["artifact"]}'))
    n = len(json.loads(tracks)['tracks'])
    print(f'同步完成：{n} 条轨迹标签；地形 {region["columns"]}×{region["rows"]} @ {region["spacingM"]} m'
          f'（{region["minHeightM"]}–{region["maxHeightM"]} m）；通行模型 {travel["schema"] if travel else "无"}')


if __name__ == '__main__':
    main()
