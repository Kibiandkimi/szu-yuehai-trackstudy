"""下载接口：清单、公开打包免口令、完整结果需口令、只认清单内的文件名、随构建切换。python3 -m pytest tests -q"""
import io
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api  # noqa: E402

TOKEN = 'test-token'


def make_build(root, stamp, public, full):
    b = root / 'builds' / stamp
    for d, files in ((b / 'site' / 'data', public), (b / 'out', full)):
        d.mkdir(parents=True)
        for name, text in files.items():
            (d / name).write_text(text, encoding='utf-8')
    return b


@pytest.fixture
def server(tmp_path):
    b = make_build(tmp_path, '20261107T000000', {'od.csv': 'origin,dest\n'}, {'od.csv': 'origin,dest\n', 'tracks.csv': 'file,minutes\n'})
    (tmp_path / 'site').symlink_to(b / 'site')
    (tmp_path / 'secret.json').write_text('{}')
    cfg = dict(site_link=str(tmp_path / 'site'), admin_token=TOKEN, api_port=0,
               rules_file=str(tmp_path / 'rules.json'), state_dir=str(tmp_path / 'state'))
    srv = api.start(cfg, threading.Event(), lambda: dict(built_at='2026-11-07 08:00:00', revision='abc1234', tracks=3))
    yield f'http://127.0.0.1:{srv.server_address[1]}/trackstudy/api', tmp_path
    srv.shutdown()


def req(url, data=None, method='GET', **headers):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers)) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def names(body):
    return zipfile.ZipFile(io.BytesIO(body)).namelist()


def test_catalog_lists_both_tiers(server):
    base, _ = server
    code, _, body = req(base + '/data')
    c = json.loads(body)
    assert code == 200 and c['built_at'] == '2026-11-07 08:00:00' and c['tracks'] == 3
    assert c['public']['files'] == [dict(name='od.csv', bytes=12, url='/trackstudy/data/od.csv')]
    assert [f['name'] for f in c['full']['files']] == ['od.csv', 'tracks.csv']


def test_public_zip_needs_no_token(server):
    base, _ = server
    code, h, body = req(base + '/data/public.zip')
    assert code == 200 and h['Content-Type'] == 'application/zip'
    assert 'filename="trackstudy-public-20261107-080000.zip"' in h['Content-Disposition']
    assert names(body) == ['od.csv']


def test_full_requires_token(server):
    base, _ = server
    assert req(base + '/data/full.zip')[0] == 403
    assert req(base + '/data/full.zip', **{'X-Token': 'wrong'})[0] == 403
    assert req(base + '/data/full.zip?token=' + urllib.parse.quote('口令'))[0] == 403      # 非 ASCII 口令按错误处理
    code, h, body = req(base + '/data/full.zip', **{'X-Token': TOKEN})
    assert code == 200 and names(body) == ['od.csv', 'tracks.csv'] and 'trackstudy-full-' in h['Content-Disposition']
    code, h, body = req(base + f'/data/full/tracks.csv?token={TOKEN}')
    assert code == 200 and body == b'file,minutes\n' and h['Content-Type'].startswith('text/csv')


def test_full_file_only_serves_listed_names(server):
    base, _ = server
    for name in ('../../secret.json', '..%2F..%2Fsecret.json', 'secret.json', ''):
        assert req(base + '/data/full/' + name, **{'X-Token': TOKEN})[0] == 404
    assert req(base + '/data/other.zip')[0] == 404


def test_downloads_follow_site_switch(server):
    base, tmp = server
    assert names(req(base + '/data/public.zip')[2]) == ['od.csv']                       # 先进缓存
    b = make_build(tmp, '20261107T010000', {'od.csv': 'x\n', 'edges.csv': 'y\n'}, {'summary.json': '{}'})
    (tmp / 'site.new').symlink_to(b / 'site'); (tmp / 'site.new').replace(tmp / 'site')
    assert names(req(base + '/data/public.zip')[2]) == ['edges.csv', 'od.csv']
    assert names(req(base + '/data/full.zip', **{'X-Token': TOKEN})[2]) == ['summary.json']
    # 旧布局（构建根目录即站点，没有 out/）：公开数据照常，完整结果 404
    old = tmp / 'builds' / '20261101T000000'; (old / 'data').mkdir(parents=True); (old / 'data' / 'od.csv').write_text('z\n')
    (tmp / 'site.new').symlink_to(old); (tmp / 'site.new').replace(tmp / 'site')
    assert names(req(base + '/data/public.zip')[2]) == ['od.csv']
    assert req(base + '/data/full.zip', **{'X-Token': TOKEN})[0] == 404


def test_put_rules_still_checks_token(server):
    base, tmp = server
    body = json.dumps(dict(edges=[], routes=[])).encode()
    assert req(base + '/rules', body, 'PUT', **{'X-Token': 'wrong'})[0] == 403
    code, _, out = req(base + '/rules', body, 'PUT', **{'X-Token': TOKEN, 'Content-Type': 'application/json'})
    assert code == 200 and json.loads(out)['ok'] and (tmp / 'rules.json').is_file()
