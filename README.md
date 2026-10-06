# 卫星边缘采集器 · 不中断采样的三色标记-清扫演练

事件溯源（Event Sourcing）+ SATB（snapshot-at-the-beginning）写屏障的增量
**三色标记-清扫（tri-color mark-and-sweep）** 回收演练系统。审查员可对任一回收周期
确认：**清扫绝不会释放仍可由标记基线根集合到达的对象**。

纯 Python 3.11 标准库实现，无第三方依赖。

## 核心安全性质

- `start` 时取**根集合与边的不可变快照**作为标记基线；
- 标记期间改写边/根：
  - 被断开的**旧目标/旧根**由 SATB 屏障无条件保留（白色则变灰入队，灰/黑则留证）；
  - **新接入的白色目标/根**同步变灰保留，其出边由后续标记负责扫描；
- 每次 `step` **仅处理一个**按对象标识**稳定排序**的灰对象；
- **灰队列未清空禁止 `sweep`**；清扫时仅「快照不可达 ∧ 白色」对象可被回收，
  并有断言 `回收集 ∩ 快照可达集 = ∅`；
- 已回收对象从边/根物理摘除，再次引用直接拒绝。

事件均带**稳定标识**：同 ID 同载荷重复投递返回 `duplicate` 并回放原结果；
同 ID 不同载荷、引用已回收对象、非法阶段清扫一律拒绝，拒绝只写独立审计文件
（`rejected.audit.jsonl`），**绝不进入恢复日志**（`events.jsonl`）。
标记中途 `reopen` 保留相同颜色、灰队列与屏障证据继续；
进程重启后从持久化事件重放，裁决与连续运行一致。

## 目录

```
app/engine.py     引擎（状态机、屏障、步进、清扫、拒绝）
app/store.py      JSONL 持久化（原子写 + fsync；事件/拒绝物理分离）
app/runtime.py    演练管理、事件分发、重启重放恢复
app/server.py     零依赖 HTTP/JSON 服务（含 /health）
app/index.html    演练页面：颜色、灰队列、存活图、屏障证据、首个拒因
tests/            25 项单元/性质测试（含 60 组随机图安全性质）
scripts/acceptance.py  验收脚本（测试 + 构建检查 + HTTP 冒烟 + 三类业务结果实测）
Dockerfile, docker-compose.yml
```

## Compose 启动（宿主端口可配置）

```bash
docker compose up --build -d            # 默认宿主端口 8080
HOST_PORT=9090 docker compose up --build -d   # 自定义宿主端口
```

- 页面： http://localhost:8080/
- 健康： http://localhost:8080/health → `{"status":"ok",...}`

## Compose 验收服务 verify

```bash
docker compose run --rm verify
```

verify 在容器内依次执行：① 代码测试（unittest）② 构建检查（py_compile）
③ API/HTTP 冒烟（/health、页面）④ **实际观察三类业务结果**：
屏障保留、非法清扫拒绝、中断恢复与连续运行一致的存活裁决；
完成后打印 `ALL ACCEPTANCE CHECKS PASSED` 并以退出码 0 报告结论（失败为 1）。

## 本地（无 Docker）

```bash
python3 -m app.server                      # HOST / PORT / DATA_DIR 可配
python3 -m unittest discover -s tests      # 单元测试
PORT=8080 python3 scripts/acceptance.py    # 自拉临时服务全量验收
python3 scripts/acceptance.py --base http://127.0.0.1:8080  # 对已运行服务验收
```

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/drills` | `{id, spec}` 导入演练（2–24 对象） |
| GET  | `/api/drills/{id}` | 状态快照 + 安全报告 + 事件流水 |
| POST | `/api/drills/{id}/events` | 投递单个事件（稳定 `id`） |
| POST | `/api/drills/{id}/replay` | `{events:[...]}` 整段回放 |
| POST | `/api/drills/{id}/recover` | 丢弃内存状态，从持久化日志重建（模拟重启） |
| GET  | `/health` | 健康响应 |

事件类型：`start` / `mutate_edge`（`set|add|remove`）/ `mutate_root`（同）
/ `step` / `sweep` / `reopen`，每演练至多 48 项接受事件。

示例：

```json
{"id":"e3","type":"mutate_edge","payload":{"src":"A","action":"set","targets":["F"]}}
```

返回中 `barrier_retained` 列出本次屏障保留的对象；拒绝返回
`{"status":"rejected","reasons":["首个拒因", ...]}`（HTTP 422）。
