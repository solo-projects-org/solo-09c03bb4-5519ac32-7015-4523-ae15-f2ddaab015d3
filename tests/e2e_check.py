#!/usr/bin/env python3
"""端到端场景矩阵：对运行中的站点+采集 API 做全链路验证。"""
import json
import urllib.request

SITE = "http://127.0.0.1:18080"
API = "http://127.0.0.1:18000"


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), "-", name, detail)
    assert cond, detail


# 确保从 v1 开始
call("POST", f"{SITE}/admin/version", {"version": "v1"})

# 1. 正常采集更正链
st, body = call("POST", f"{API}/collect", {"notice_number": "2024-003"})
check("v1 采集更正链成功", st == 200 and body["status"] == "published",
      f"http={st} {body.get('failure_reason')}")
check("更正链抓到 3 通报 2 边", body["notices"] == 3 and body["edges"] == 2)

# 2. 循环引用
st, body = call("POST", f"{API}/collect", {"notice_number": "CYCLE-A"})
check("循环引用采集成功且不重复", st == 200 and body["pages_fetched"] == 3)
st, n = call("GET", f"{API}/notices/CYCLE-A")
check("CYCLE-A 同时有 corrects 与 corrected_by",
      n["corrects"] == ["CYCLE-B"] and n["corrected_by"] == ["CYCLE-B"])

# 3. 镜像合并与祖先
st, n1 = call("GET", f"{API}/notices/2024-001")
check("同号两 URL 合并为两个来源", len(n1["sources"]) == 2,
      str([s["url"] for s in n1["sources"]]))
st, anc = call("GET", f"{API}/notices/2024-003/ancestors")
check("祖先链 003→002→001", [a["notice_number"] for a in anc] == ["2024-002", "2024-001"])

# 4. 幂等
st, body = call("POST", f"{API}/collect", {"notice_number": "2024-003"})
check("同版同内容幂等 noop", body["status"] == "noop")

# 5. v2 同号异文冲突，旧图保留
old_graph = call("GET", f"{API}/notices/2024-003")[1]
call("POST", f"{SITE}/admin/version", {"version": "v2"})
st, body = call("POST", f"{API}/collect", {"notice_number": "2024-003"})
check("v2 同号异文返回 409", st == 409 and "同号异文" in body["failure_reason"])
check("冲突明细指向 2024-001", body["conflict_detail"]["notice_number"] == "2024-001")
still = call("GET", f"{API}/notices/2024-003")[1]
check("失败后查询仍为旧图", still["latest_snapshot_id"] == old_graph["latest_snapshot_id"])
# CYCLE-A 的图不应受影响
st, _ = call("GET", f"{API}/notices/CYCLE-A")
check("其他通报号的图不受失败采集影响", st == 200)

# 6. v3 链接失效
call("POST", f"{SITE}/admin/version", {"version": "v3"})
st, body = call("POST", f"{API}/collect", {"notice_number": "2024-003"})
check("v3 链接失效返回 409", st == 409 and "链接失效" in body["failure_reason"])
check("失效明细含 404 状态", body["conflict_detail"]["status_code"] == 404)

# 7. 回到 v1 重新发布，CYCLE 图仍在
call("POST", f"{SITE}/admin/version", {"version": "v1"})
st, body = call("POST", f"{API}/collect", {"notice_number": "2024-003"})
check("回到 v1 后重新发布成功", st == 200 and body["status"] == "published")
st, _ = call("GET", f"{API}/notices/CYCLE-A")
check("多图并存：CYCLE-A 依旧可查", st == 200)

# 8. 快照与运行审计
snaps = call("GET", f"{API}/snapshots")[1]
check("所有快照保持 published 不可变", all(s["status"] == "published" for s in snaps))
runs = call("GET", f"{API}/runs")[1]
statuses = {r["status"] for r in runs}
check("运行历史含 published 与 failed", {"published", "failed"} <= statuses, str(statuses))

# 9. 历史更正链 API（按已发布快照号还原时点视图）
snap_003 = next(s for s in snaps if s["root_notice_number"] == "2024-003")
sid_003 = snap_003["id"]
st, chain = call("GET", f"{API}/snapshots/{sid_003}/notices/2024-003/correction-chain")
check("历史链按旧快照查询成功", st == 200, f"http={st}")
check("历史链还原 003→002→001 三节点",
      [n["notice_number"] for n in chain["nodes"]] == ["2024-003", "2024-002", "2024-001"])
check("节点带深度、批号、来源与所属快照",
      all({"depth", "snapshot_id", "batches", "sources"} <= set(n) for n in chain["nodes"]))
n001 = next(n for n in chain["nodes"] if n["notice_number"] == "2024-001")
check("旧通报批号依据可还原（LOT-001/LOT-002）",
      {b["batch_number"] for b in n001["batches"]} == {"LOT-001", "LOT-002"})
check("同号镜像的两个来源都在历史视图中",
      len(n001["sources"]) == 2)
check("历史链链接均未断", chain["broken_link_count"] == 0)

# 循环引用快照：环上通报只列一次
snap_cycle = next(s for s in snaps if s["root_notice_number"] == "CYCLE-A")
st, cchain = call(
    "GET",
    f"{API}/snapshots/{snap_cycle['id']}/notices/CYCLE-A/correction-chain")
check("循环历史链查询成功", st == 200)
cycle_numbers = [n["notice_number"] for n in cchain["nodes"]]
check("循环引用只列一次", len(cycle_numbers) == len(set(cycle_numbers))
      and set(cycle_numbers) == {"CYCLE-A", "CYCLE-B"})

# 404：快照不存在 / 通报在该时点尚未发布（用不存在的通报号）
st, _ = call("GET", f"{API}/snapshots/999999/notices/2024-003/correction-chain")
check("不存在的快照号返回 404", st == 404)
st, _ = call("GET", f"{API}/snapshots/{snap_cycle['id']}/notices/NO-SUCH/correction-chain")
check("时点未发布的通报号返回 404", st == 404)

# 历史结果不随后续采集改变：再发一次采集后重查同一旧快照
before = call("GET", f"{API}/snapshots/{sid_003}/notices/2024-003/correction-chain")[1]
call("POST", f"{API}/collect", {"notice_number": "2024-003"})
after = call("GET", f"{API}/snapshots/{sid_003}/notices/2024-003/correction-chain")[1]
check("与新采集交错后历史结果一致", before == after)
# 现有最新查询照常
check("最新查询 /graph 照常", call("GET", f"{API}/graph")[0] == 200)
check("最新查询 /notices 照常", call("GET", f"{API}/notices/2024-003")[0] == 200)

print("\n全部 E2E 场景通过。")
