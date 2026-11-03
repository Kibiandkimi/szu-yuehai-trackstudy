#!/usr/bin/env bash
# 部署到 DirMap 同一台 Azure 机器：/opt/trackstudy，https://<host>/trackstudy/
#   bash deploy/deploy.sh            # 发布已提交的 HEAD（未提交改动不会上线）
# 远端结构：releases/<UTC>（代码）、current → 最新 release、venv/、config.json、builds/<时间>（站点构建）、
#          site → 当前构建（nginx alias）、out/（完整结果，不公开）、state/（状态与日志）。
set -euo pipefail
cd "$(dirname "$0")/.."
SSH=${TRACKSTUDY_SSH:-"ssh -p 2222 -o ConnectTimeout=15 -i $HOME/.ssh/id_rsa kibi@85.211.195.104"}
REV=$(git rev-parse --short HEAD); STAMP=$(date -u +%Y%m%dT%H%M%SZ)
[ -z "$(git status --porcelain -- . ':!docs')" ] || echo "注意：有未提交改动，只部署 HEAD $REV"
echo "上传 $REV → releases/$STAMP"
git archive --format=tar HEAD | $SSH "mkdir -p /opt/trackstudy/releases/$STAMP && tar x -C /opt/trackstudy/releases/$STAMP && echo $REV > /opt/trackstudy/releases/$STAMP/REVISION"
$SSH "bash /opt/trackstudy/releases/$STAMP/deploy/remote.sh $STAMP"
echo "检查公网…"
curl -fsS "https://85.211.195.104/trackstudy/status.json" && echo
