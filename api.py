"""规则与数据下载接口（serve.py 内的线程，nginx /trackstudy/api/ → 127.0.0.1:api_port）。

    GET  /trackstudy/api/rules           当前规则（公开）
    PUT  /trackstudy/api/rules           保存规则，需口令；保存后立即触发重算
    GET  /trackstudy/api/status          重算状态（含是否有待处理的规则修改）
    GET  /trackstudy/api/data            可下载文件清单与数据时间（公开）
    GET  /trackstudy/api/data/public.zip 公开数据打包：site/data 的聚合 CSV（记录匿名，无逐轨迹时间与原始轨迹）
    GET  /trackstudy/api/data/full.zip   完整结果打包（out/：含逐轨迹明细与匹配轨迹的完整版地图），需口令
    GET  /trackstudy/api/data/full/<文件> 完整结果中的单个文件，需口令

口令 = config.admin_token，放请求头 X-Token；浏览器直接下载时也可用 ?token=。
下载的总是当前站点那一次构建：serve.py 把公开站点与完整结果建在 builds/<时间>/site 与 builds/<时间>/out，
site 链接一次原子切换，完整结果即 site/../out。

规则文件格式（rules.json）：
  edges:  [{key, mode: bike|walk, dir: +|-|both, action: block|push|ride, name?, note?}]
          key 为 network.edge_key（OSM way + 两端坐标）；push = 必须推车，ride = 可骑（OSM 标为台阶/人行天桥但实际能骑）
  routes: [{origin, dest, mode, via: [[lon, lat], ...], both?, note?}]   自定义路线：按途经点逐段规划
"""
import functools
import hmac
import io
import json
import os
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import network

MAX_BODY = 200_000
LON, LAT = (network.BBOX[0], network.BBOX[1]), (network.BBOX[2], network.BBOX[3])
BASE = '/trackstudy'
TYPES = {'.csv': 'text/csv; charset=utf-8', '.json': 'application/json', '.geojson': 'application/geo+json',
         '.html': 'text/html; charset=utf-8', '.zip': 'application/zip'}


def validate(r):
    if not isinstance(r, dict):
        raise ValueError('规则必须是对象')
    edges, routes = r.get('edges', []), r.get('routes', [])
    if not isinstance(edges, list) or not isinstance(routes, list) or len(edges) > 2000 or len(routes) > 300:
        raise ValueError('edges/routes 必须是列表（≤2000 / ≤300）')
    out_e, seen = [], set()
    for e in edges:
        mode, d, act = e.get('mode'), e.get('dir', 'both'), e.get('action')
        if not isinstance(e.get('key'), str) or len(e['key']) > 120:
            raise ValueError('路段 key 无效')
        if mode not in network.RULE_ACTIONS or act not in network.RULE_ACTIONS[mode] or d not in ('+', '-', 'both'):
            raise ValueError(f'路段规则无效：{mode}/{d}/{act}')
        k = (e['key'], mode, d)
        if k in seen:
            continue
        seen.add(k)
        out_e.append(dict(key=e['key'], mode=mode, dir=d, action=act, name=str(e.get('name', ''))[:60], note=str(e.get('note', ''))[:200]))
    out_r = []
    for x in routes:
        via = x.get('via')
        if x.get('mode') not in ('walk', 'bike') or not isinstance(via, list) or not 1 <= len(via) <= 20:
            raise ValueError('自定义路线需要方式与 1–20 个途经点')
        pts = []
        for p in via:
            lon, lat = float(p[0]), float(p[1])
            if not (LON[0] < lon < LON[1] and LAT[0] < lat < LAT[1]):
                raise ValueError('途经点不在校园范围内')
            pts.append([round(lon, 7), round(lat, 7)])
        out_r.append(dict(origin=str(x.get('origin', ''))[:30], dest=str(x.get('dest', ''))[:30], mode=x['mode'], via=pts,
                          both=bool(x.get('both')), note=str(x.get('note', ''))[:200]))
    return dict(version=1, updated=time.strftime('%Y-%m-%d %H:%M:%S'), edges=out_e, routes=out_r)


def save(cfg, rules):
    path = Path(cfg['rules_file']); path.parent.mkdir(parents=True, exist_ok=True)
    hist = Path(cfg['state_dir']) / 'rules-history'; hist.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        (hist / f'{time.strftime("%Y%m%dT%H%M%S")}.json').write_bytes(path.read_bytes())
        for old in sorted(hist.glob('*.json'))[:-30]:
            old.unlink()
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(rules, ensure_ascii=False, indent=1), encoding='utf-8'); os.replace(tmp, path)


def published(cfg):
    """当前站点所在的构建：(公开站点目录, 同一次重算的完整结果目录)。"""
    site = Path(cfg['site_link']).resolve()
    return site, site.parent / 'out'


def listing(d):
    return sorted(p for p in d.iterdir() if p.is_file()) if d.is_dir() else []


@functools.lru_cache(maxsize=4)
def bundle(d):
    """把目录打包成 zip。构建完成后目录不再改动，新构建路径不同，所以按路径缓存。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in listing(Path(d)):
            z.write(p, p.name)
    return buf.getvalue()


def catalog(cfg, st):
    site, out = published(cfg)
    item = lambda p, url: dict(name=p.name, bytes=p.stat().st_size, url=url)
    return dict(built_at=st.get('built_at'), revision=st.get('revision'), tracks=st.get('tracks'),
                public=dict(zip=f'{BASE}/api/data/public.zip', files=[item(p, f'{BASE}/data/{p.name}') for p in listing(site / 'data')]),
                full=dict(zip=f'{BASE}/api/data/full.zip', token='需要口令：请求头 X-Token 或 ?token=',
                          files=[item(p, f'{BASE}/api/data/full/{p.name}') for p in listing(out)]))


def start(cfg, kick, status):
    """kick: threading.Event，置位后 serve.py 主循环立即重算；status(): 当前状态 dict。"""
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def _file(self, body, name):
            self.send_response(200)
            self.send_header('Content-Type', TYPES.get(Path(name).suffix, 'application/octet-stream'))
            self.send_header('Content-Disposition', f'attachment; filename="{name}"')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def _authorized(self):
            token = (cfg.get('admin_token') or '').encode()
            given = self.headers.get('X-Token') or parse_qs(urlsplit(self.path).query).get('token', [''])[0]
            return bool(token) and hmac.compare_digest(given.encode(), token)

        def _download(self, name):
            site, out = published(cfg)
            stamp = (status().get('built_at') or 'latest').replace('-', '').replace(':', '').replace(' ', '-')
            if name == 'public.zip':
                if not listing(site / 'data'):
                    return self._send(404, {'error': '还没有构建好的数据'})
                return self._file(bundle(str(site / 'data')), f'trackstudy-public-{stamp}.zip')
            if name != 'full.zip' and not name.startswith('full/'):
                return self._send(404, {'error': 'not found'})
            if not self._authorized():
                return self._send(403, {'error': '口令错误或缺失（请求头 X-Token 或 ?token=）'})
            files = {p.name: p for p in listing(out)}
            if not files:
                return self._send(404, {'error': '当前站点没有对应的完整结果（等下一次重算）'})
            if name == 'full.zip':
                return self._file(bundle(str(out)), f'trackstudy-full-{stamp}.zip')
            p = files.get(name.removeprefix('full/'))       # 只认清单里的文件名，不拼接路径
            return self._file(p.read_bytes(), p.name) if p else self._send(404, {'error': 'not found'})

        def do_GET(self):
            p = self.path.split('?')[0].removeprefix('/trackstudy/api')
            if p == '/rules':
                return self._send(200, network.load_rules(cfg['rules_file']))
            if p == '/status':
                return self._send(200, dict(status(), pending=kick.is_set()))
            if p == '/data':
                return self._send(200, catalog(cfg, status()))
            if p.startswith('/data/'):
                return self._download(p.removeprefix('/data/'))
            self._send(404, {'error': 'not found'})

        def do_PUT(self):
            if self.path.split('?')[0].removeprefix('/trackstudy/api') != '/rules':
                return self._send(404, {'error': 'not found'})
            if not self._authorized():
                return self._send(403, {'error': '口令错误'})
            n = int(self.headers.get('Content-Length') or 0)
            if n > MAX_BODY:
                return self._send(413, {'error': '规则过大'})
            try:
                rules = validate(json.loads(self.rfile.read(n)))
            except Exception as e:
                return self._send(400, {'error': str(e)})
            with lock:
                save(cfg, rules)
            kick.set()
            self._send(200, {'ok': True, 'rules': rules})

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(('127.0.0.1', int(cfg['api_port'])), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
