"""本地开发用：把 DirMap 服务器上的共享轨迹库镜像到 data/dirmap/store（与服务器同结构，只读使用）。

    python3 sync_dirmap.py

镜像 tracks.json、tracks/*.npz（允许账户的启用轨迹）、models.json、粤海校区地形 artifact 与对应通行模型。
服务器上的 trackstudy 直接读 /opt/dirmap/data/elevation，不需要这一步。
"""
import json
import os
import subprocess
import sys

from pathlib import Path

import config


def fetch(ssh, path):
    return subprocess.run(ssh + ['cat', path], check=True, capture_output=True, timeout=120).stdout


def main():
    cfg = config.load()
    ssh = [os.path.expanduser(a) for a in cfg['dirmap_ssh']]
    if not ssh:
        sys.exit('config.local.json 未配置 dirmap_ssh')
    remote = cfg['dirmap_elevation_dir'].rstrip('/')
    dst = config.store(); (dst / 'tracks').mkdir(parents=True, exist_ok=True)
    tracks = json.loads(fetch(ssh, f'{remote}/tracks.json'))
    models = json.loads(fetch(ssh, f'{remote}/models.json'))
    owners = set(cfg['dirmap_owners'])
    want = [t['id'] for t in tracks['tracks'] if t.get('enabled') and t.get('kind') != 'contours' and (not owners or t.get('ownerName') in owners)]
    have = {p.stem for p in (dst / 'tracks').glob('*.npz')}
    files = [f'tracks/{i}.npz' for i in want if i not in have]
    region = next((r for r in models['regions'] if r['name'] == cfg['dirmap_region']), None)
    if region:
        files.append(region['artifact'])
        tm = next((t for t in models.get('travelModels', []) if t['regionId'] == region['id']), None)
        if tm:
            files.append(tm['artifact'])
    if files:   # 一次 tar 传输
        tar = subprocess.run(ssh + ['tar', 'c', '-C', remote] + files, check=True, capture_output=True, timeout=300).stdout
        subprocess.run(['tar', 'x', '-C', str(dst)], input=tar, check=True)
    for name, obj in (('tracks.json', tracks), ('models.json', models)):   # 最后写索引，镜像始终自洽
        (dst / name).write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
    rules = subprocess.run(ssh + ['cat', '/opt/trackstudy/rules.json'], capture_output=True, timeout=60)
    if rules.returncode == 0 and rules.stdout:   # 服务器上的规则为准（网页编辑）
        p = Path(cfg['rules_file']); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(rules.stdout)
        print('已同步服务器规则 →', p)
    print(f'镜像完成：{len(want)} 条轨迹（新增 {len([f for f in files if f.startswith("tracks/")])}），'
          f'地形 {region["name"] if region else "无"}（sourceVersion {models.get("sourceVersion")}）→ {dst}')


if __name__ == '__main__':
    main()
