# 粤海校区道路通行效率与通勤分析（轨迹重建）

在线地图：**https://85.211.195.104/trackstudy/**（部署在 DirMap 同一台 Azure 机器，随 DirMap 共享轨迹自动更新；单文件、可离线保存）。
旧的 GitHub Pages 地址现在只做跳转。

用自己记录的轨迹重建深圳大学粤海校区各道路**分方向**的步行/骑行通行效率（含坡度、上下课高峰），
在有向路网上规划路线（A→B 与 B→A 分别计算），并给出宿舍→教学楼、**教学区之间通勤**、全地点 OD
和 **5/10/15 分钟（骑行 3/5/10 分钟）可达圈**。

## 数据流

```
DirMap #elevation 页上传 KML / 改方式 ──► /opt/dirmap/data/elevation（tracks.json + tracks/*.npz + models.json，DirMap 写）
                                                     │ 只读（约定见 dirmap_store.py）
trackstudy.service（serve.py，每 60 s 检查；变化稳定 2 分钟、负载不高时）──► analyze.py ──► builds/<时间> ──原子切换──► site/
                                                                                         nginx /trackstudy/ ◄─┘
```

- **轨迹只存一份**：在 [DirMap 共享地形页](https://85.211.195.104/#elevation) 上传 KML、设置方式（步行/跑步/骑行；“自动”由本分析按速度识别），
  约 3 分钟内本站自动重算。只使用 `kibi` 账户的已启用轨迹（结果公开，其他贡献者的轨迹未获同意不使用；见 `config.server.json` 的 `dirmap_owners`）。
- 页面状态栏显示数据更新时间；重算失败时保留上一版并提示，日志在服务器 `/opt/trackstudy/state/last-run.log`。
- DirMap 的识别/配准/地形重建与本服务共用 892 MB 机器：负载 >1.5 或可用内存 <250 MB 时推迟重算（最多 30 分钟）；systemd 限制 450 MB / 1 核。

## 封禁路段与自定义路线

OSM 不知道哪些路实际上不能骑（台阶没标、坡道被标成台阶、和行人共用的天桥等），这些用规则修正：

- 网页“封禁/规则”工具：点路段，为**当前方式**设置 禁行 / 推车（约 1 m/s）/ 可骑（OSM 标为台阶或人行天桥但实际能骑，按步道速度计），可只作用于一个方向。
- “双向路线”里选两个地点后点“按途经点自定义”，按顺序在地图上点途经点（可设回程相同）。OD 表、教学区通勤、宿舍→教学楼都会改用这条路线。
- 修改先在网页上即时预览（网页端与服务器用同一套规划逻辑），点“保存到服务器”（口令见服务器 `/opt/trackstudy/config.json` 的 `admin_token`）后约 20 秒重算全部表格。
  规则存在 `/opt/trackstudy/rules.json`，每次保存前的版本留在 `state/rules-history/`；路段用 OSM way + 两端坐标定位，OSM 参照更新后对不上的规则会在重算日志里报出。
- 初始规则（`deploy/rules.seed.json`）来自实测骑行：斋区⇄汇文楼走学生西路→深大北路，斋区⇄致理楼走学生西路→召南天桥。

## 数据下载

| 地址（`https://85.211.195.104` 下） | 内容 | 口令 |
| --- | --- | --- |
| `/trackstudy/api/data` | 文件清单（大小、下载地址）与数据更新时间 | 否 |
| `/trackstudy/api/data/public.zip` | 公开数据：`od.csv`、`teaching_commute.csv`、`edges.csv`、`isochrones.csv`（聚合结果；单个文件也在 `/trackstudy/data/<文件>`） | 否 |
| `/trackstudy/api/data/full.zip` | 完整结果（即 `out/`）：另含 `summary.json`、`tracks.csv`、`observed_trips.csv`、`validation.csv`、`route_choice.csv`、`edges.geojson`、`od_dorm_teaching.csv` 和带匹配轨迹的完整版 `map.html` | 是 |
| `/trackstudy/api/data/full/<文件>` | 完整结果中的单个文件 | 是 |

口令同规则编辑（`/opt/trackstudy/config.json` 的 `admin_token`），放请求头 `X-Token`，浏览器里也可加 `?token=…`；网页“方法”标签里有下载链接。
下载的总是网页当前显示的那一次重算：服务器把每次重算的公开站点和完整结果放在同一个 `builds/<时间>/` 下（`site/`、`out/`），一起原子切换。

```bash
# 用服务器最新的完整结果替换本地 out/
curl -fsS -H "X-Token: $TOKEN" https://85.211.195.104/trackstudy/api/data/full.zip -o /tmp/trackstudy-full.zip && unzip -o /tmp/trackstudy-full.zip -d out/
```

## 部署与本地开发

```bash
git commit … && bash deploy/deploy.sh          # 发布 HEAD：releases/<UTC>、current 切换、立即重算、重启 trackstudy.service
                                                # 首次会建 venv、装 systemd 单元，并在 nginx dirmap.conf 里加 /trackstudy/（先备份）

source ~/Work/DirMap/tools/runtime-env.sh       # 本地：numpy / scipy
python3 sync_dirmap.py                          # 镜像服务器轨迹库到 data/dirmap/store（与服务器同结构）
python3 analyze.py                              # 本地重算 → out/（完整）与 site/（公开版），并刷新本 README 的结果
python3 -m pytest tests -q
NODE_PATH=~/Work/ChineseDungeonSplit4/node_modules node tests/browser_check.cjs   # 浏览器验收（JS 与 Python 规划一致等）
```

个人路径写在不入库的 `config.local.json`（格式见 `config.example.json`）；服务器配置 `/opt/trackstudy/config.json`（初值 `deploy/config.server.json`）。
本地还可用 `python3 tracks.py add 文件.kml --mode bike` 把只在本地用的轨迹加进 `data/kml/`（按 sha256 与 DirMap 去重，DirMap 优先），
`python3 tracks.py list` 查看全部轨迹及方式来源，`python3 tracks.py label <文件> <方式>` 写本地手动标签（优先于 DirMap）。
网页“KML 轨迹”标签可在本机预览 KML/GPX（不上传）：速度、自动识别结果、与模型最快路线的对比。

自动识别用快段速度稳定度区分跑步（变异系数 <0.13）与骑行，在现有 20 条已标注轨迹上 18 条判对、2 条跑走交替判为 unknown、0 条判错；
unknown 不参与分析，需在 DirMap 里设置方式。

## 方法

1. **道路识别（HMM 地图匹配）**：候选路段垂距（σ 7 m）+ 行进方向与路段切线夹角；转移代价 =
   |路网距离 − GPS 直线距离| / 8 m（截断 Dijkstra）+ 逆行惩罚；楼内/操场为离网状态。匹配点之间的路网路径按长度分配时间，
   得到“记录 × 路段 × 方向”的观测（旧版跨路段时方向一律记 +，已修正）。校内轨迹匹配率 92–100%，偏差中位 2.4–6 m。
2. **方向性坡度**：地形取 DirMap 粤海校区共享轨迹高程（`shared-track-height-v5`，10 m 网格），每条路段按约 10 m 小段积分
   正/反向坡度特征（上坡 / ≤5% 缓下坡 / 陡下坡，与 DirMap `travel_model` 相同），桥/隧道/台阶只用端点。台阶上/下行分开（OSM incline 或地形高差）。
3. **通行效率模型**（与 DirMap `foot-efficiency-corridor-v2` 同构的稳健 log 耗时回归）：每条记录配速项（不惩罚）+ 方向性坡度
   （向先验收缩：步行 Tobler，骑行平地）+ 道路类别 + 路段效应 + 方向残差 + 高峰（只作用于步道/天桥/台阶，且只能变慢）。
   车行道不设高峰项：整段出行的快慢（赶课骑得快）与该记录自己的配速项不可区分，之前分开估计会得到“高峰车行道更快”的假象。跑走共享坡度/道路效应，骑行单独。
   道路效应拟合约每 40 m 一份权重，同一记录同一路段同一方向至多 5 份；通勤基准取各次出行对数配速的等权平均，避免一条长距离记录主导通勤估时。
   骑行走步道/人行天桥有让行/推车先验（2.6 / 1.5 m/s），有数据时被学习值修正。
4. **规划**：有向图，骑行遵守单行与用户规则；起终点投影到附近 6 条路段中最合适的一条（比最近者远不超过 25 m，门到路直线 ×1.3 步行），
   自定义路线的途经点只吸附最近一条路段。网页端 JS 用同一份弧代价与同一投影算法，
   与 Python OD 结果一致（浏览器验收全部 1520 组，最大偏差 <1%）。可达圈从道路端点和起点投影共同推进，计入单行路中途出发的可达片段；
   地点可达时间同样使用自定义路线，网页地图与导出表格一致。
5. **验证**：留一整条记录重拟合并预测其在实际路径上的移动耗时；用实测起终点重新规划并与实际路线比重合度。

<!-- RESULTS:BEGIN（analyze.py 自动生成） -->
## 主要结果（2026-11-07，16 条步行/跑步 + 21 条骑行校内轨迹）

- **坡度（骑行）**：θ = [4.30, -5.35, 0.24]，5% 上坡慢 24%、5% 下坡快 23%；DirMap 独立学到 [3.59, -4.25, 2.25]（5% 上坡慢 20%）
- **坡度（步行）**：θ = [0.67, -0.09, 4.22]，5% 上坡慢 3%、5% 下坡快 0%；DirMap 独立学到 [2.29, -0.06, 4.44]（5% 上坡慢 12%）
- **教学区之间方向差最大（骑行、平峰，路上时间）**：科技楼→致理楼 4.9 分 / 回程 5.7 分（爬升 2 / 22 m）；汇文楼→致理楼 5.7 分 / 回程 6.7 分（爬升 5 / 29 m）；科技楼→致信楼 5.0 分 / 回程 5.8 分（爬升 2 / 23 m）
- **步行换楼**：汇文楼→致理楼 20.8 分，加出入楼 1 分超过大课间 20 分钟，需骑行；5 分钟节间只够近邻楼（致理↔致真 3.3 分、致信↔计软 4.7 分）。
- **宿舍→教学楼（平峰，步行 / 骑行）**：斋区→汇文楼 9.6 / 3.1 分；斋区→致理楼 12.8 / 5.0 分；乔区→汇文楼 20.6 / 6.2 分；乔区→致理楼 15.0 / 5.3 分；南区→汇文楼 20.4 / 6.8 分；南区→致理楼 6.2 / 2.5 分
- **高峰**：只作用于步道/天桥/台阶（人流），骑行耗时 ×1.24（31 条观测）、步行 ×1.00（47 条）；车行道不设高峰项——整段出行的快慢（赶课）与个人配速不可区分。
- **用户规则**：0 条路段规则（封禁/推车/可骑行）、2 条自定义路线已用于规划。
- **验证（留一记录，中位绝对误差）**：步行 常速 12.4% / 完整模型 13.3%；骑行 常速 22.4% / 完整模型 18.2% / 前 40% 校准 11.9%。误差主要来自每次出行本身的快慢；样本少，完整模型未显著优于常速。
- **路线选择校验（骑行）**：规划与实际路线重合中位 63%（21 条）。
- **地图匹配**：35 条在路网上的轨迹匹配率 88–100%，偏差中位 2.2–7.9 m。
<!-- RESULTS:END -->

## 文件

| 文件 | 作用 |
| --- | --- |
| `dirmap_store.py` | 只读访问 DirMap 共享轨迹库（与 DirMap 约定的接口） |
| `tracks.py` | 轨迹来源合并（DirMap + 本地 KML 库）、方式标签、自动识别、CLI |
| `sync_dirmap.py` | 本地开发：镜像服务器轨迹库 |
| `serve.py`、`deploy/` | 服务器常驻重算与原子发布；部署脚本、systemd 单元、服务器配置 |
| `api.py` | 规则保存与数据下载接口（serve.py 内的线程，nginx `/trackstudy/api/`） |
| `network.py` | OSM 路网、拓扑补连、地形与方向坡度、道路类别、投影 |
| `matching.py` | HMM 地图匹配 |
| `model.py` | 逐点方式、路段×方向观测、稳健回归 |
| `routing.py` | 有向规划、一对多、可达圈 |
| `places.py` | 地点（OSM 建筑质心，分组） |
| `analyze.py` / `report.py` | 主流程 / 输出（`out/` 完整，`site/` 公开聚合） |
| `map_template.html` / `map_view.py` | 单文件离线地图（Leaflet 1.9.4 内嵌，见 `vendor/leaflet/`） |

`out/`（不入库）：`edges.csv|geojson` 路段×方向×方式速度与效应、`od.csv`、`od_dorm_teaching.csv`、`teaching_commute.csv`、
`isochrones.csv`、`validation.csv`、`route_choice.csv`、`observed_trips.csv`、`tracks.csv`、`summary.json`、`map.html`（含轨迹匹配明细）。
`site/`（服务器上为 `/opt/trackstudy/site` → `builds/<时间>/site`，完整结果在同一构建的 `out/`）：同一地图的公开版（记录匿名、无时间与原始轨迹）、`status.json` 及 `data/` 聚合 CSV。

**局限**：样本少（尤其高峰骑行），多数路段效应收缩到类别水平；GPS 难以区分极近平行路；坡度效应是经配速调整后的相关，不是因果证明；
地点用建筑群质心，宿舍到停车点/楼门的实际位置会影响几十秒。
