#!/usr/bin/env bash
# 服务器端安装/切换（由 deploy.sh 调用；幂等）。
set -euo pipefail
STAMP=$1; R=/opt/trackstudy; REL=$R/releases/$STAMP
mkdir -p $R/builds $R/state
chmod 755 $R $R/builds
# 每次重算的完整结果在 builds/<时间>/out（不经 nginx，只由下载接口凭口令提供）；out 固定指向当前站点所在的构建
[ -L $R/out ] || { rm -rf $R/out; ln -s site/../out $R/out; }
if [ ! -x $R/venv/bin/python ]; then
  echo "创建 venv（numpy/scipy）"; python3 -m venv $R/venv
  $R/venv/bin/pip install -q --upgrade pip && $R/venv/bin/pip install -q -r $REL/deploy/requirements.txt
fi
$R/venv/bin/pip install -q -r $REL/deploy/requirements.txt
[ -f $R/config.json ] || cp $REL/deploy/config.server.json $R/config.json
[ -f $R/rules.json ] || cp $REL/deploy/rules.seed.json $R/rules.json
# 规则编辑口令：首次部署生成，保存在 config.json（不入库）
python3 - $R/config.json <<'PY'
import json, secrets, sys
p = sys.argv[1]; c = json.load(open(p))
changed = False
for k, v in (('admin_token', None), ('rules_file', '/opt/trackstudy/rules.json'), ('api_port', 8767)):
    if not c.get(k):
        c[k] = v or secrets.token_urlsafe(12); changed = True
if changed:
    json.dump(c, open(p, 'w'), ensure_ascii=False, indent=1)
PY
chmod 600 $R/config.json
# systemd
if ! cmp -s $REL/deploy/trackstudy.service /etc/systemd/system/trackstudy.service; then
  sudo -n cp $REL/deploy/trackstudy.service /etc/systemd/system/trackstudy.service && sudo -n systemctl daemon-reload
  sudo -n systemctl enable trackstudy.service >/dev/null 2>&1
fi
# nginx：照 /commute/ 的做法加到 DirMap 站点的 TLS server 块（DirMap 发布流程不改 nginx 配置）
CONF=$(readlink -f /etc/nginx/sites-enabled/dirmap.conf)   # 生效的是 sites-enabled（当前为普通文件）
if ! grep -q 'location /trackstudy/api/' $CONF; then
  sudo -n cp $CONF $R/dirmap.conf.bak.$STAMP
  python3 - "$CONF" > /tmp/dirmap.conf.new <<'PY'
import sys
s = open(sys.argv[1]).read()
key = '    location = /trackstudy {'
block = '    location /trackstudy/api/ { proxy_pass http://127.0.0.1:8767; proxy_set_header Host $host; client_max_body_size 256k; }\n'
if key in s:
    s = s.replace(key, block + key, 1)
sys.stdout.write(s)
PY
  sudo -n cp /tmp/dirmap.conf.new $CONF
  if ! sudo -n nginx -t; then sudo -n cp $R/dirmap.conf.bak.$STAMP $CONF; echo "nginx 配置检查失败，已还原"; exit 1; fi
  sudo -n systemctl reload nginx
fi
if ! grep -q 'location /trackstudy/' $CONF; then
  sudo -n cp $CONF $R/dirmap.conf.bak.$STAMP
  python3 - "$CONF" > /tmp/dirmap.conf.new <<'PY'
import sys
s = open(sys.argv[1]).read()
block = ('    location /trackstudy/api/ { proxy_pass http://127.0.0.1:8767; proxy_set_header Host $host; client_max_body_size 256k; }\n'
         '    location = /trackstudy { return 301 /trackstudy/; }\n'
         '    location /trackstudy/ { alias /opt/trackstudy/site/; index index.html; add_header Cache-Control "no-cache"; }\n')
key = '    location /commute/'
assert key in s, 'dirmap.conf 中找不到 /commute/ 位置'
sys.stdout.write(s.replace(key, block + key, 1))
PY
  sudo -n cp /tmp/dirmap.conf.new $CONF
  if ! sudo -n nginx -t; then sudo -n cp $R/dirmap.conf.bak.$STAMP $CONF; echo "nginx 配置检查失败，已还原"; exit 1; fi
  sudo -n systemctl reload nginx
fi
ln -sfn $REL $R/current.new && mv -T $R/current.new $R/current
# 立即按新代码重算一次（负载检查跳过），再启动常驻监视
sudo -n systemctl stop trackstudy.service || true
TRACKSTUDY_CONFIG=$R/config.json nice -n 10 $R/venv/bin/python -B $R/current/serve.py --once
sudo -n systemctl restart trackstudy.service
ls -1dt $R/releases/* | tail -n +4 | xargs -r rm -rf
systemctl is-active trackstudy.service
cat $R/state/status.json
