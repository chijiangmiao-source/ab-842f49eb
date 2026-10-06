# 卫星边缘采集器 · 增量三色标记-清扫缓存回收演练系统

在**不中断采样**的前提下回收旧缓存：审查员可逐事件回放颜色、灰队列、存活图与
首个拒因，确认**任一回收周期都不会释放仍可由当前根集合到达的对象**。

纯 Python 标准库实现（无第三方依赖），SQLite 事件溯源，HTTP API + 浏览器页面，
Docker Compose 一键启动并内置 `verify` 验收服务。

## 核心语义

| 事件 | 载荷 | 说明 |
|---|---|---|
| `start_mark` | `{}` | 启动回收周期，当前根全部置灰入队 |
| `step` | `{}` | 每次仅扫描灰队列中**稳定排序（字典序）**的一个灰对象 |
| `set_edge` | `{"src","old_dst","new_dst"}` | 原子改写边；`old_dst`/`new_dst` 可为 `null`（纯增/纯删） |
| `set_root` | `{"object","is_root"}` | 加入/移除根 |
| `sweep` | `{}` | 清扫；灰队列未空一律拒绝 |
| `reopen` | `{}` | 标记中途重开：颜色/灰队列/证据原样继续 |

**屏障（标记期间改写边或根）**

- 删除边时，旧目标若为白色立即染灰（删除屏障，本周期保留）；
- 新接入的目标若为白色立即染灰（插入屏障）；
- 写入者已扫描为黑时退回灰色重新入队观察新出边；
- 加入/移除根时，白色相关对象染灰。

由此强三色不变量（不存在 黑→白 的当前边）始终成立；清扫时仅回收
**不可达白色**对象，屏障保留的灰/黑对象即使已不可达也延至下一周期。

**拒绝规则（拒单持久化但不驱动状态，恢复状态不被污染）**

- 同 ID 重复投递 → 只回放原结果（`replayed=true`，HTTP 200）；
- 同 ID 不同载荷 → `409 conflicting_event_payload`；
- 引用已回收对象 → `409 reference_reclaimed`；
- 未启动清扫 → `sweep_without_mark`；灰队列非空清扫 → `sweep_not_ready`；
- 非法事件类型/载荷、对象数越界（须 2–24）、事件超过 48 项 → 400 系列拒绝。

## 快速开始（Docker Compose）

```bash
# 可选：自定义宿主机端口
cp .env.example .env          # HOST_PORT / HOST_BIND

docker compose up --build -d
# 页面
open http://localhost:8080/
# 健康响应
curl http://localhost:8080/health
```

### 验收服务 verify

```bash
docker compose --profile verify run --rm verify
# 退出码 0 = 全部通过，非 0 = 验收失败（CI 可直接使用该退出码）
```

`verify` 依次执行：

1. **构建检查**：`compileall` 全部字节码编译；
2. **代码测试**：引擎语义、300 组随机序列不变量（强三色不变量 +
   “清扫绝不回收可达对象”）、事件溯源幂等、拒单不污染、重开恢复一致性；
3. **API/HTTP 冒烟**：真实 HTTP 调用并实际观察三类业务结果——
   屏障保留、非法清扫拒绝、中断恢复后与连续运行一致的存活裁决。

## 本地开发（无需 Docker）

```bash
python3 -m app.server                 # 默认 0.0.0.0:8080，DB /data/gc.db
PORT=8080 DB_PATH=./data/gc.db python3 -m app.server

sh scripts/verify.sh                  # 本地跑全套验收（冒烟脚本会自举服务）
python3 scripts/smoke.py              # 只跑 API/HTTP 冒烟
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/api/drills` | 演练列表 |
| POST | `/api/drills` | 导入演练（`objects` 2–24、`events` ≤48） |
| GET | `/api/drills/{id}` | 当前状态（颜色/灰队列/存活图/证据/首个拒因） |
| GET | `/api/drills/{id}/timeline` | 逐事件快照帧（供逐步/整段回放） |
| GET | `/api/drills/{id}/recovery` | 连续运行 vs 标记中途重开的裁决比对 |
| POST | `/api/drills/{id}/events` | 投递事件（稳定 ID 幂等） |

### 演练 JSON 示例

```json
{
  "id": "demo-1",
  "objects": ["R", "A", "B", "G"],
  "roots": ["R"],
  "edges": {"R": ["A"], "A": ["B"]},
  "events": [
    {"id": "e1", "type": "start_mark", "payload": {}},
    {"id": "e2", "type": "step", "payload": {}},
    {"id": "e3", "type": "set_edge",
     "payload": {"src": "A", "old_dst": "B", "new_dst": null}},
    {"id": "e4", "type": "step", "payload": {}},
    {"id": "e5", "type": "sweep", "payload": {}}
  ]
}
```

页面内置三个演练：`demo-barrier`（屏障保留）、`demo-illegal-sweep`
（非法清扫/引用回收对象拒绝）、`demo-reopen`（中断恢复一致性）。

## 目录结构

```
app/
  engine.py     纯函数三色标记-清扫引擎（屏障/阶段守卫/不变量闸）
  models.py     颜色、阶段、事件、状态
  store.py      SQLite 事件账本（稳定 ID、拒单留痕）
  service.py    导入校验、幂等投递、折叠重放、检查点恢复比对
  server.py     HTTP 服务 + 内置演示演练
  static/       回放页面（SVG 存活图、逐步/整段回放、投递与恢复面板）
tests/          unittest 代码测试
scripts/
  smoke.py      API/HTTP 冒烟（实际观察三类业务结果）
  verify.sh     verify 验收服务入口（退出码即结论）
Dockerfile / docker-compose.yml（web + verify）
```

## 安全裁决依据

- `sweep` 内置防御性不变量闸：最终仍可达的对象若为白色，直接抛
  `invariant_violation`，绝不进入回收；
- 随机不变量测试在 300 组合法/非法事件序列上断言：
  标记期间不存在黑→白当前边、灰队列恒与颜色一致且字典序稳定、
  每次清扫回收集与“可达集”不相交。
