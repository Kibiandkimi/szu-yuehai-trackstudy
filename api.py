"""规则接口（serve.py 内的线程，nginx /trackstudy/api/ → 127.0.0.1:api_port）。

    GET  /trackstudy/api/rules           当前规则（公开）
    PUT  /trackstudy/api/rules           保存规则，需请求头 X-Token = config.admin_token；保存后立即触发重算
    GET  /trackstudy/api/status          重算状态（含是否有待处理的规则修改）

规则文件格式（rules.json）：
  edges:  [{key, mode: bike|walk, dir: +|-|both, action: block|push|ride, name?, note?}]
          key 为 network.edge_key（OSM way + 两端坐标）；push = 必须推车，ride = 可骑（OSM 标为台阶/人行天桥但实际能骑）
  routes: [{origin, dest, mode, via: [[lon, lat], ...], both?, note?}]   自定义路线：按途经点逐段规划
"""
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import network

MAX_BODY = 200_000
LON, LAT = (network.BBOX[0], network.BBOX[1]), (network.BBOX[2], network.BBOX[3])


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

        def do_GET(self):
            p = self.path.split('?')[0].removeprefix('/trackstudy/api')
            if p == '/rules':
                return self._send(200, network.load_rules(cfg['rules_file']))
            if p == '/status':
                return self._send(200, dict(status(), pending=kick.is_set()))
            self._send(404, {'error': 'not found'})

        def do_PUT(self):
            if self.path.split('?')[0].removeprefix('/trackstudy/api') != '/rules':
                return self._send(404, {'error': 'not found'})
            token = cfg.get('admin_token') or ''
            if not token or not hmac.compare_digest(self.headers.get('X-Token', ''), token):
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
