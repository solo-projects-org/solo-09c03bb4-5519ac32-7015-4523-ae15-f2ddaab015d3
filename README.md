# 药品召回更正公告采集系统

药企数据员收到召回**更正公告**后，需要找齐它引用的全部旧公告与涉及批号。
本系统提供：

- 一个可切换版本、每次改动递增全站版号的**本地 HTML 公告站**；
- 一个基于 **FastAPI + SQLite** 的采集 API，提交通报号后沿“更正自”链接递归抓取，
  按通报号合并不同 URL，保存来源、批号与引用边。

## 一、目录结构

```
.
├── collector/             # 采集 API（FastAPI）
│   ├── main.py            # 路由与 Swagger
│   ├── crawler.py         # 沿“更正自”链接 BFS、循环去重、同号异文/跨版检测
│   ├── parser.py          # 标准库 html.parser 页面解析
│   ├── fetcher.py         # httpx 抓取，强制校验 X-Site-Revision
│   ├── service.py         # 采集编排，失败不发布
│   ├── db.py              # SQLite 持久层：单事务原子发布、CAS、崩溃恢复
│   └── models.py          # Pydantic 模型
├── site/                  # 本地公告站（仅标准库）
│   ├── server.py          # 版号管理、版本切换管理 API、静态服务
│   ├── v1/                # 基线版本：更正链、同号镜像、循环引用样例
│   ├── v2/                # 同号异文冲突样例（镜像正文被改写）
│   └── v3/                # 链接失效样例（2024-001.html 被删除）
├── tests/                 # 20 个单元/API/并发测试 + E2E 脚本
├── Dockerfile / site/Dockerfile
└── docker-compose.yml
```

## 二、快速开始（Docker Compose）

```bash
docker compose up --build
```

- 公告站：http://localhost:8080 （管理 API 见下）
- 采集 API Swagger：http://localhost:8000/docs

公告站版本切换（每次切换全站版号 +1，状态持久化在 volume，重启不回退）：

```bash
curl -X POST localhost:8080/admin/version \
  -H 'Content-Type: application/json' -d '{"version":"v2"}'
curl localhost:8080/admin/state     # 查看当前版本与版号
curl -X POST localhost:8080/admin/next      # 轮换到下一个版本
curl -X POST localhost:8080/admin/reload    # 内容热更新后仅递增版号
```

每个页面（含 404）都回传 `X-Site-Revision` 响应头，HTML 内也有同名 meta。

## 三、典型操作流程（Swagger 可直接点）

```bash
# 1) 发起采集：更正公告 2024-003 沿 003→002→001 抓齐旧公告
curl -X POST localhost:8000/collect -H 'Content-Type: application/json' \
  -d '{"notice_number":"2024-003"}'

# 2) 查询某通报的来源、批号、引用关系
curl localhost:8000/notices/2024-003

# 3) 查询可追溯的全部旧公告（祖先链）
curl localhost:8000/notices/2024-003/ancestors

# 4) 最新整张子图 / 运行历史 / 快照历史
curl localhost:8000/graph
curl localhost:8000/runs
curl localhost:8000/snapshots
```

## 四、关键正确性语义

| 需求 | 实现方式 |
| --- | --- |
| 沿“更正自”链接抓取旧公告 | BFS 遍历，边 `from 更正公告 → to 被更正公告` 落库 |
| 按通报号合并不同 URL | 索引页建立 `通报号→URL 列表`，同号多 URL 合并为多来源 |
| 循环链接不重复采集 | 按 URL 全局去重（`queued` 集合），环上节点只抓一次 |
| 每次改动递增全站版号，每页回传 | 站点维护单调递增 revision，所有响应带 `X-Site-Revision` |
| 跨版整次不发布 | 以索引页版号为基准，任一页面版号不同即失败 |
| 同号异文整次不发布 | 同号多 URL 正文 sha256 不一致即失败，返回冲突明细 |
| 链接失效整次不发布 | 任一页面 4xx/5xx 即失败，记录失效 URL 与状态码 |
| 查询保留旧图 | 失败不写任何业务数据；快照发布后不可变，按通报号指针读旧图 |
| 晚完成的旧版不能覆盖新版 | 发布事务内 CAS：同种子 revision 更小则 `StaleRevisionError` 拒绝 |
| 重启读不到半张图 | 整张子图在单个 `BEGIN IMMEDIATE` 事务写入；启动清理 staging 残留；WAL |
| 并发安全 | SQLite 单库 + IMMEDIATE 事务串行化发布；同版同内容幂等 noop |

## 五、内置样例

| 通报号 | 用途 |
| --- | --- |
| `2024-003` | 更正链：003 更正自 002，002 更正自 001 |
| `2024-001` | 同号两个 URL（主公告 + 归档镜像），验证合并；v2 中镜像被改成异文 |
| `2024-004` | 独立公告，跨版本内容不变 |
| `CYCLE-A` / `CYCLE-B` | 互相“更正自”，验证循环去重 |
| v2 | 全站版号 +1，且 `2024-001` 同号异文 → 采集 409 |
| v3 | 全站版号 +1，且 `2024-001.html` 被删 → 采集 409 |

## 六、本地开发与测试（不用 Docker）

```bash
pip install -r requirements.txt pytest

# 终端 1：公告站
SITE_CONTENT_ROOT=./site SITE_STATE_DIR=./data/site SITE_PORT=8080 python site/server.py
# 终端 2：采集 API
COLLECTOR_DB=./data/collector.db SITE_BASE_URL=http://127.0.0.1:8080 \
  uvicorn collector.main:app --reload --port 8000

# 单元/API/并发测试（20 个，无需外部服务）
python -m pytest

# 端到端矩阵（需先按上启动两个服务，默认地址 127.0.0.1:18080 / 18000）
python tests/e2e_check.py
```

## 七、失败响应示例

```json
{
  "run_id": 7,
  "status": "failed",
  "snapshot_id": null,
  "site_revision": 5,
  "failure_reason": "同一通报号在不同 URL 上正文不一致（同号异文），整次采集终止",
  "conflict_detail": {
    "notice_number": "2024-001",
    "urls": [".../2024-001.html", ".../2024-001_mirror.html"],
    "content_hashes": ["22ae...", "597e..."]
  }
}
```

并发下旧版被拒绝时 `status` 为 `rejected_stale`，HTTP 409，已发布新版保持不变。
