"""轨迹库：导入、去重、方式标签与自动识别。

    python3 tracks.py scan                  # 扫描 config 中的目录（微信文件、下载），新 KML/GPX 入库
    python3 tracks.py add a.kml dir/ --mode bike   # 手动加入文件或目录，可同时指定方式
    python3 tracks.py label 20261102_191119 bike   # 修改方式（walk/run/bike/exclude/auto）
    python3 tracks.py list                  # 列出全部轨迹、方式来源与是否参与分析

入库 = 按内容 sha256 去重后复制到 data/kml/（微信清理缓存后仍可重算）。
方式优先级：data/labels.csv 手动标签 > DirMap 服务器标签（sync_dirmap.py 同步，按 sha256 对应）> 自动识别。
自动识别只把明确的步行/骑行用于分析；跑步与骑行速度重叠时标为 unknown，需要手动标注。
"""
import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

import config

TZ = timezone(timedelta(hours=8))
KML_DIR = config.DATA / 'kml'
INDEX = config.DATA / 'library.json'
LABELS = config.DATA / 'labels.csv'
DIRMAP_TRACKS = config.DATA / 'dirmap' / 'tracks.json'
MODES = ('walk', 'run', 'bike', 'exclude')
ALIASES = {'bicycle': 'bike', 'cycling': 'bike', 'ride': 'bike', 'foot': 'walk', 'hike': 'walk', 'running': 'run'}

LAT0, LON0 = 22.535, 113.932


def xy(lon, lat):
    return (np.asarray(lon) - LON0) * 111320 * math.cos(math.radians(LAT0)), (np.asarray(lat) - LAT0) * 110574


# ---------- 解析 ----------
def _iso(s):
    return datetime.fromisoformat(s.strip().replace('Z', '+00:00')).timestamp()


def parse(path):
    """返回 (t, lon, lat, h)；支持 KML gx:Track（可多段）与 GPX trkpt。无时间的线不可用于测速，返回 None。"""
    s = Path(path).read_text(encoding='utf-8', errors='replace')
    rows = []
    if path.suffix.lower() == '.gpx':
        for m in re.finditer(r'<trkpt\b([^>]*)>(.*?)</trkpt>', s, re.S):
            a, body = m.group(1), m.group(2)
            lat = re.search(r'lat="([^"]+)"', a); lon = re.search(r'lon="([^"]+)"', a)
            tm = re.search(r'<time>([^<]+)</time>', body); ele = re.search(r'<ele>([^<]+)</ele>', body)
            if lat and lon and tm:
                rows.append((_iso(tm.group(1)), float(lon.group(1)), float(lat.group(1)), float(ele.group(1)) if ele else np.nan))
    else:
        for block in re.findall(r'<gx:Track\b.*?</gx:Track>', s, re.S):
            w = re.findall(r'<when>([^<]+)</when>', block); c = re.findall(r'<gx:coord>([^<]+)</gx:coord>', block)
            if len(w) != len(c):
                raise ValueError(f'{path.name}: when/coord 数量不一致 ({len(w)}/{len(c)})')
            for a, b in zip(w, c):
                v = [float(q) for q in b.split()]
                rows.append((_iso(a), v[0], v[1], v[2] if len(v) > 2 else np.nan))
    if len(rows) < 3:
        return None
    a = np.array(rows, float)
    a = a[np.argsort(a[:, 0], kind='stable')]
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------- 库 ----------
def _index():
    return json.loads(INDEX.read_text(encoding='utf-8')) if INDEX.is_file() else {}


def _save_index(ix):
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(ix, ensure_ascii=False, indent=1), encoding='utf-8')


def labels():
    if not LABELS.is_file():
        return {}
    with LABELS.open(encoding='utf-8-sig') as f:
        return {r['file'].strip(): dict(mode=r['mode'].strip(), note=r.get('note', '')) for r in csv.DictReader(f) if r.get('file')}


def set_label(file, mode, note=''):
    rows = labels()
    if mode == 'auto':
        rows.pop(file, None)
    else:
        rows[file] = dict(mode=mode, note=note)
    LABELS.parent.mkdir(parents=True, exist_ok=True)
    with LABELS.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f); w.writerow(['file', 'mode', 'note'])
        for k in sorted(rows):
            w.writerow([k, rows[k]['mode'], rows[k]['note']])


def add(paths, mode=None, quiet=False):
    """把文件/目录（递归）中的 KML/GPX 入库；返回新加入的文件名。"""
    ix = _index(); known = {v['sha256'] for v in ix.values()}
    KML_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for p in map(Path, paths):
        p = p.expanduser()
        if p.is_dir():
            files += sorted(q for q in p.rglob('*') if q.suffix.lower() in ('.kml', '.gpx') and q.is_file())
        elif p.is_file():
            files.append(p)
        elif not quiet:
            print('找不到', p)
    new = []
    for f in files:
        h = sha256(f)
        if h in known:
            continue
        try:
            parsed = parse(f)
        except Exception as e:  # 损坏/非轨迹文件不入库
            print(f'跳过 {f}: {e}'); continue
        if parsed is None:
            if not quiet:
                print(f'跳过 {f.name}: 没有逐点时间，无法测速')
            continue
        name = f.name if not (KML_DIR / f.name).exists() else f'{f.stem}_{h[:8]}{f.suffix}'
        shutil.copy2(f, KML_DIR / name)
        ix[name] = dict(sha256=h, source=str(f), added=round(time.time()), points=len(parsed[0]),
                        start=datetime.fromtimestamp(parsed[0][0], TZ).isoformat(timespec='seconds'))
        known.add(h); new.append(name)
        if mode:
            set_label(name, mode)
        if not quiet:
            print(f'入库 {name}（{len(parsed[0])} 点，{ix[name]["start"]}）' + (f'，方式 {mode}' if mode else ''))
    _save_index(ix)
    return new


def scan(quiet=False):
    cfg = config.load()
    srcs = [s for s in cfg['kml_sources'] if Path(s).is_dir()]
    return add(srcs, quiet=quiet) if srcs else []


# ---------- 方式 ----------
def _dirmap_labels():
    if not DIRMAP_TRACKS.is_file():
        return {}
    out = {}
    for t in json.loads(DIRMAP_TRACKS.read_text(encoding='utf-8'))['tracks']:
        a = ALIASES.get(t.get('activity'), t.get('activity'))
        if a in MODES and t.get('sourceSha256'):
            out[t['sourceSha256']] = a
    return out


def moving_speeds(t, lon, lat):
    """5 s 平均位置之间的速度（m/s），用于自动识别与摘要。"""
    x, y = xy(lon, lat)
    b = np.floor((t - t[0]) / 5).astype(int)
    st = np.flatnonzero(np.r_[True, np.diff(b) != 0]); n = np.diff(np.r_[st, len(t)])
    px, py, pt = (np.add.reduceat(v, st) / n for v in (x, y, t))
    dt = np.diff(pt); v = np.hypot(np.diff(px), np.diff(py)) / np.maximum(dt, 1e-6)
    ok = (dt > 0) & (dt < 30)
    return v[ok], dt[ok]


def classify(t, lon, lat):
    """返回 (mode, 说明)。跑步与骑行在 2.5–4.5 m/s 重叠，靠快段速度的稳定度区分：
    跑步配速平稳（快段变异系数 <0.13），校内骑行频繁减速/滑行且有 ≥6 m/s 的段。
    阈值来自 20 条已标注轨迹（全部判对或判 unknown）；不确定时给 unknown，需手动标注。"""
    v, dt = moving_speeds(t, lon, lat)
    mv = v[(v >= 0.5) & (v <= 15)]
    if len(mv) < 10:
        return 'unknown', '有效移动太少'
    p50, p90 = np.percentile(mv, [50, 90])
    fast = v[(v > 2.3) & (v < 15)]
    cv = fast.std() / fast.mean() if len(fast) >= 20 else None
    p95 = np.percentile(fast, 95) if len(fast) >= 20 else 0
    desc = f'移动中位 {p50:.2f} m/s，P90 {p90:.2f}' + (f'，快段变异 {cv:.2f}' if cv is not None else '')
    if p50 < 1.8 and p90 < 2.6:
        return 'walk', desc
    if cv is None:
        return 'unknown', desc + '（快段太少）'
    if cv < 0.13:
        return 'run', desc
    if p95 >= 6.0 or (cv >= 0.25 and p95 >= 5.0 and t[-1] - t[0] < 20 * 60):
        return 'bike', desc
    return 'unknown', desc + '（跑步/骑行难以区分）'


def load_tracks():
    """库中全部轨迹（已排序），附方式与来源。"""
    ix = _index(); man = labels(); dm = _dirmap_labels()
    out = []
    for name in sorted(ix):
        p = KML_DIR / name
        if not p.is_file():
            continue
        t, lon, lat, h = parse(p)
        info = ix[name]
        key = next((k for k in (name, Path(name).stem) if k in man), None)
        if key:
            mode, src = man[key]['mode'], '手动'
        elif info['sha256'] in dm:
            mode, src = dm[info['sha256']], 'DirMap'
        else:
            mode, why = classify(t, lon, lat); src = '自动：' + why
        out.append(dict(file=name, sha256=info['sha256'], label=mode, label_source=src, t=t, lon=lon, lat=lat, h=h))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('scan')
    a = sub.add_parser('add'); a.add_argument('paths', nargs='+'); a.add_argument('--mode', choices=MODES)
    l = sub.add_parser('label'); l.add_argument('file'); l.add_argument('mode', choices=MODES + ('auto',)); l.add_argument('--note', default='')
    sub.add_parser('list')
    args = ap.parse_args(argv)
    if args.cmd == 'scan':
        new = scan()
        print(f'新入库 {len(new)} 个' if new else '没有新轨迹')
    elif args.cmd == 'add':
        mode = ALIASES.get(args.mode, args.mode)
        new = add(args.paths, mode)
        print(f'新入库 {len(new)} 个')
    elif args.cmd == 'label':
        ix = _index()
        match = [n for n in ix if n == args.file or Path(n).stem == args.file or ix[n]['sha256'].startswith(args.file)]
        if len(match) != 1:
            sys.exit(f'找不到唯一匹配：{args.file}（候选 {match}）')
        set_label(match[0], args.mode, args.note)
        print(f'{match[0]} → {args.mode}')
    elif args.cmd == 'list':
        print(f'{"文件":<26}{"开始":<18}{"时长":>7}{"点数":>7}  方式    来源')
        for tr in load_tracks():
            d = datetime.fromtimestamp(tr['t'][0], TZ).strftime('%m-%d %H:%M')
            print(f'{tr["file"]:<26}{d:<18}{(tr["t"][-1]-tr["t"][0])/60:>6.1f}m{len(tr["t"]):>7}  {tr["label"]:<7} {tr["label_source"]}')


if __name__ == '__main__':
    main()
