"""服务器常驻：DirMap 共享轨迹库或代码变化 → 自动重算并原子发布静态站点。

    TRACKSTUDY_CONFIG=/opt/trackstudy/config.json python3 serve.py [--once]

- 每 60 s 读输入指纹（DirMap tracks.json + models.json + 用户规则的内容哈希 + 代码版本）；变化后需稳定 120 s 再重算，
  避免上传/改标签/地形重建过程中反复计算。网页保存规则（api.py）后立即重算。
- DirMap 的识别、配准、地形重建在同一台 892 MB 机器上运行：负载 >1.5 或可用内存 <250 MB 时推迟（最多 30 分钟）。
- 在 builds/<时间>/site（公开站点）与 builds/<时间>/out（同一次重算的完整结果，供 api.py 下载接口）中构建，
  成功后原子切换 site → builds/<时间>/site，保留最近 3 份；失败保留旧站点，错误写进 state/status.json，
  页面读取同目录 status.json 显示数据时间与最近一次失败。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import threading

import api
import config
import dirmap_store

ROOT = Path(__file__).resolve().parent
POLL, SETTLE, MAX_DEFER = 60, 120, 1800


def bj():
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')


def code_version():
    p = ROOT / 'REVISION'
    return p.read_text().strip() if p.is_file() else 'dev'


def busy():
    load = os.getloadavg()[0]
    avail = next((int(l.split()[1]) // 1024 for l in open('/proc/meminfo') if l.startswith('MemAvailable')), 10**6)
    return (load > 1.5 or avail < 250), f'负载 {load:.2f}，可用内存 {avail} MB'


def state_dir(cfg):
    p = Path(cfg['state_dir']); p.mkdir(parents=True, exist_ok=True); return p


def read_status(cfg):
    p = state_dir(cfg) / 'status.json'
    return json.loads(p.read_text(encoding='utf-8')) if p.is_file() else {}


def write_status(cfg, st):
    p = state_dir(cfg) / 'status.json'; tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8'); os.replace(tmp, p)
    link = Path(cfg['site_link'])
    if link.exists():   # 当前站点里也放一份（页面读取）
        (link / 'status.json').write_text(json.dumps(st, ensure_ascii=False), encoding='utf-8')


def build(cfg, fp):
    builds = Path(cfg['builds_dir']); builds.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%dT%H%M%S'); target = builds / stamp
    site, out = target / 'site', target / 'out'      # out 不在 nginx alias 下，只经下载接口凭口令提供
    out.mkdir(parents=True, exist_ok=True); out.chmod(0o700)
    env = dict(os.environ, TRACKSTUDY_SITE=str(site), TRACKSTUDY_OUT=str(out))
    t0 = time.time()
    proc = subprocess.run(['nice', '-n', '19', sys.executable, '-B', str(ROOT / 'analyze.py'), '--no-scan'],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    log = (proc.stdout + proc.stderr)[-6000:]
    (state_dir(cfg) / 'last-run.log').write_text(proc.stdout + proc.stderr, encoding='utf-8')
    if proc.returncode != 0 or not (site / 'index.html').is_file():
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(log[-1500:])
    st = dict(input=fp, built_at=bj(), seconds=round(time.time() - t0, 1),
              revision=code_version(), tracks=len(dirmap_store.records()), error=None)
    (site / 'status.json').write_text(json.dumps(st, ensure_ascii=False), encoding='utf-8')
    link = Path(cfg['site_link']); tmp = link.with_name(link.name + '.new')
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(site); os.replace(tmp, link)          # 原子切换（完整结果随之切换：site/../out）
    for old in sorted(p for p in builds.iterdir() if p.is_dir())[:-3]:
        shutil.rmtree(old, ignore_errors=True)
    return st


def fingerprint(cfg):
    import hashlib
    p = Path(cfg['rules_file'])
    rules = hashlib.sha256(p.read_bytes()).hexdigest()[:8] if p.is_file() else '-'
    return f'{dirmap_store.fingerprint()}-{rules}-{code_version()}'


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--once', action='store_true', help='若输入变化立即重算一次后退出')
    args = ap.parse_args()
    cfg = config.load()
    kick = threading.Event()
    if not args.once and cfg.get('admin_token'):
        api.start(cfg, kick, lambda: read_status(cfg))
    seen, since, deferred = None, time.time(), None
    while True:
        try:
            fp = fingerprint(cfg)
            st = read_status(cfg)
            if fp != seen:
                seen, since = fp, time.time()
            urgent = args.once or kick.is_set()
            due = fp != st.get('input') and (urgent or time.time() - since >= SETTLE)
            if due:
                hot, why = busy()
                deferred = deferred or time.time()
                if hot and not urgent and time.time() - deferred < MAX_DEFER:
                    print('推迟重算：', why, flush=True)
                else:
                    print('重算', fp, flush=True)
                    try:
                        st = build(cfg, fp); print('完成', st, flush=True)
                    except Exception as e:
                        st = dict(st, input=fp, error=str(e)[-1500:], failed_at=bj())
                        print('失败', e, flush=True)
                    write_status(cfg, st); deferred = None
            kick.clear()
            if args.once:
                return
        except Exception as e:   # 读 DirMap 文件时的瞬时错误：下一轮再试
            print('轮询出错：', e, flush=True)
        kick.wait(POLL)


if __name__ == '__main__':
    main()
