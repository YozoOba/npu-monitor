# NPU 集群查询功能手册

本文档集中说明当前版本已经实现的查询能力，包括 Web 页面、命令行、Console HTTP 接口和 Collector 内部接口。部署和启动方法请参见 [使用指导.md](使用指导.md)。

## 1. 查询入口

假设主节点地址为 `21.57.228.91`，端口使用默认值：

| 入口 | 地址或命令 | 适用场景 |
|---|---|---|
| Web 页面 | `http://21.57.228.91:18081/` | 日常查看、筛选、导出和错误数据管理 |
| Console CLI | `python3 -m console.cli --collector-url http://21.57.228.91:18080 ...` | 容器内排障、脚本查询和批量导出 |
| Console HTTP API | `http://21.57.228.91:18081/api/...` | 对接其他内网系统，推荐的对外查询入口 |
| Collector 内部 API | `http://21.57.228.91:18080/internal/v1/...` | Console 与 Collector 通信、现场诊断 |

Console 只负责查询和展示，不保存第二份监控数据。实际热数据、告警和管理审计记录都保存在 Collector 的 SQLite 中。

## 2. Web 页面支持的查询

打开：

```text
http://主节点IP:18081/
```

页面顶部的公共条件包括：

- 集群；
- 开始时间和结束时间；
- 历史聚合粒度：5 分钟、15 分钟、1 小时或 1 天；
- 精确节点 ID；
- 逻辑卡号/Die 编号。

点击“查询”后可以查看：

| 区域 | 查询结果 | 可用筛选 |
|---|---|---|
| 集群概览 | 节点数、登记卡数、新鲜卡数、利用率、HBM、活跃告警 | 集群 |
| 利用率趋势 | 平均/最低/最高利用率、HBM、覆盖率趋势 | 时间、集群、节点、卡号、聚合粒度 |
| 集群分组 | 每个集群的节点、卡覆盖率和利用率 | 无额外条件 |
| 节点明细 | 节点状态、卡数、利用率、HBM、数据年龄和最近采样 | 集群、节点名称/ID 搜索、状态、分页 |
| 告警记录 | 告警类型、级别、状态、开始/恢复时间和信息 | 时间、集群、节点、级别、状态、类型、分页 |
| 原始采样 | 采样状态、卡覆盖、缺失卡、接收时间 | 时间、集群、节点、状态、节点名称/ID 搜索、分页 |
| 数据导出 | 逐卡 CSV 或 XLSX | 时间、集群、节点、卡号、采样状态 |
| 管理审计 | 最近执行的节点/数据管理操作 | 当前页面显示最近 10 条 |

节点状态含义：

| 状态 | 含义 |
|---|---|
| `online` | 最近一次样本新鲜且完整 |
| `degraded` | 最近仍在上报，但样本不完整或采集失败 |
| `stale` | 超过新鲜阈值未收到新样本 |
| `offline` | 超过离线阈值未收到新样本 |

默认采集间隔为 60 秒时，状态阈值通常为：120 秒后 `stale`、300 秒后 `offline`。实际阈值还会根据 Agent 上报的采集间隔动态放大。

## 3. Console CLI 查询命令

以下命令均在项目目录 `/work/monitor` 中执行。

### 3.1 集群整体状态

```bash
cd /work/monitor

python3 -m console.cli \
  --collector-url http://主节点IP:18080 summary

python3 -m console.cli \
  --collector-url http://主节点IP:18080 summary \
  --cluster training-a
```

返回节点状态数量、登记容量、当前新鲜卡数、数据新鲜度、上报完整率、利用率、HBM 和活跃告警数量。

### 3.2 集群分组

```bash
python3 -m console.cli \
  --collector-url http://主节点IP:18080 clusters
```

### 3.3 节点查询

```bash
python3 -m console.cli \
  --collector-url http://主节点IP:18080 nodes \
  --cluster training-a \
  --state offline \
  --search npu-node \
  --page 1 --page-size 50
```

支持参数：

| 参数 | 说明 |
|---|---|
| `--cluster` | 精确集群 ID |
| `--state` | `online`、`degraded`、`stale` 或 `offline` |
| `--search` | 在节点 ID 和节点名称中进行包含搜索 |
| `--page` | 页码，默认 1 |
| `--page-size` | 每页数量，默认 50，最大 500 |

### 3.4 历史趋势

```bash
python3 -m console.cli \
  --collector-url http://主节点IP:18080 history \
  --start 2026-08-01 --end 2026-08-07 \
  --bucket 3600 \
  --cluster training-a \
  --node npu-node-01 \
  --card 0
```

也可以用相对时间：

```bash
python3 -m console.cli \
  --collector-url http://主节点IP:18080 history \
  --hours 24 --bucket 300
```

`--bucket` 单位为秒，范围为 60～86400。一次查询最多生成 10000 个时间桶。

### 3.5 原始采样

```bash
python3 -m console.cli \
  --collector-url http://主节点IP:18080 samples \
  --hours 24 \
  --cluster training-a \
  --node npu-node-01 \
  --status partial \
  --search npu-node \
  --page 1 --page-size 100
```

采样状态：

- `complete`：预期逻辑卡/Die 全部采集成功；
- `partial`：只采集到部分逻辑卡/Die；
- `failed`：本次没有采集到有效卡数据。

每页最多返回 1000 条采样。

### 3.6 告警查询

```bash
python3 -m console.cli \
  --collector-url http://主节点IP:18080 alerts \
  --hours 168 \
  --cluster training-a \
  --severity warning \
  --status active \
  --type card_coverage \
  --page 1 --page-size 100
```

支持的级别为 `warning`、`critical`，生命周期状态为 `active`、`resolved`。当前可能产生的告警类型包括：

- `node_degraded`；
- `node_stale`；
- `node_offline`；
- `card_coverage`；
- `clock_skew`。

### 3.7 CSV/XLSX 导出

```bash
python3 -m console.cli \
  --collector-url http://主节点IP:18080 export \
  --start 2026-08-01 --end 2026-08-07 \
  --cluster training-a \
  --status complete \
  --format xlsx \
  --output /work/monitor/runtime-data/npu-2026-08-01_to_07.xlsx
```

可组合 `--node`、`--cluster`、`--card` 和 `--status`。导出结果一行对应一个逻辑卡/Die，默认单次最多导出 31 天，可通过 `NPU_CONSOLE_MAX_EXPORT_DAYS` 调整。

导出字段为：

```text
cluster_id,node_id,node_name,collected_at,received_at,sample_status,
expected_cards,collected_cards,coverage_percent,card_id,utilization,
hbm_used_mb,hbm_total_mb
```

### 3.8 管理操作记录

```bash
python3 -m console.cli \
  --collector-url http://主节点IP:18080 operations \
  --page 1 --page-size 50
```

返回节点修改、节点删除和错误数据删除的条件、影响范围、执行结果及数据库备份路径。

## 4. Console HTTP 查询接口

业务系统或临时 `curl` 查询建议访问 Console 的 18081 端口。以下接口均为当前已实现接口。

### 4.1 接口总表

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/health` | Console 与上游 Collector 健康状态 |
| GET | `/api/snapshot` | 全集群或指定集群当前快照 |
| GET | `/api/clusters` | 所有集群分组汇总 |
| GET | `/api/nodes` | 节点搜索、状态过滤和分页 |
| GET | `/api/history` | 集群、节点或单卡历史趋势 |
| GET | `/api/samples` | 原始采样查询和分页 |
| GET | `/api/alerts` | 告警查询和分页 |
| GET | `/api/export` | 下载 CSV/XLSX |
| GET | `/api/admin/operations` | 管理操作审计记录 |
| POST | `/api/admin/preview` | 预览节点或数据管理操作，非普通只读查询 |
| POST | `/api/admin/execute` | 执行已确认的管理操作，属于数据修改接口 |

### 4.2 公共时间参数

`/api/history`、`/api/samples`、`/api/alerts` 和 `/api/export` 支持：

| 参数 | 说明 |
|---|---|
| `start` | ISO 8601 时间或 `YYYY-MM-DD` |
| `end` | ISO 8601 时间或 `YYYY-MM-DD` |
| `hours` | 未提供完整时间范围时使用的最近小时数，默认 24 |

日期形式的 `end=2026-08-07` 会包含 8 月 7 日全天。精确时间范围统一使用“开始时间包含、结束时间不包含”的规则，即 `[start, end)`。

### 4.3 查询参数

| 接口 | 支持的参数 |
|---|---|
| `/api/snapshot` | `cluster_id` |
| `/api/clusters` | 无 |
| `/api/nodes` | `cluster_id`、`state`、`q`、`page`、`page_size` |
| `/api/history` | `start`、`end`、`hours`、`bucket`、`cluster_id`、`node_id`、`card_id` |
| `/api/samples` | `start`、`end`、`hours`、`cluster_id`、`node_id`、`status`、`q`、`page`、`page_size` |
| `/api/alerts` | `start`、`end`、`hours`、`cluster_id`、`node_id`、`severity`、`status`、`type`、`page`、`page_size` |
| `/api/export` | `start`、`end`、`hours`、`cluster_id`、`node_id`、`card_id`、`status`、`format` |
| `/api/admin/operations` | `page`、`page_size` |

其中 `cluster_id`、`node_id`、告警 `type` 和各种状态参数均为精确匹配；`q` 是节点 ID/名称的包含搜索。

### 4.4 curl 示例

查询全集群快照：

```bash
curl -sS http://主节点IP:18081/api/snapshot
```

查询离线节点：

```bash
curl -sS --get http://主节点IP:18081/api/nodes \
  --data-urlencode 'cluster_id=training-a' \
  --data-urlencode 'state=offline' \
  --data-urlencode 'page=1' \
  --data-urlencode 'page_size=50'
```

查询一张逻辑卡的 24 小时趋势：

```bash
curl -sS --get http://主节点IP:18081/api/history \
  --data-urlencode 'hours=24' \
  --data-urlencode 'node_id=npu-node-01' \
  --data-urlencode 'card_id=0' \
  --data-urlencode 'bucket=300'
```

查询缺卡样本：

```bash
curl -sS --get http://主节点IP:18081/api/samples \
  --data-urlencode 'hours=24' \
  --data-urlencode 'status=partial' \
  --data-urlencode 'page=1' \
  --data-urlencode 'page_size=100'
```

下载报表：

```bash
curl -fS --get http://主节点IP:18081/api/export \
  --data-urlencode 'start=2026-08-01' \
  --data-urlencode 'end=2026-08-07' \
  --data-urlencode 'cluster_id=training-a' \
  --data-urlencode 'format=xlsx' \
  -o npu-2026-08-01_to_07.xlsx
```

## 5. Collector 内部查询接口

Console 会把查询转发到 Collector。内部接口与 Console 接口的对应关系如下：

| Console | Collector |
|---|---|
| `/api/snapshot` | `/internal/v1/snapshot` |
| `/api/clusters` | `/internal/v1/clusters` |
| `/api/nodes` | `/internal/v1/nodes` |
| `/api/history` | `/internal/v1/series` |
| `/api/samples` | `/internal/v1/samples` |
| `/api/alerts` | `/internal/v1/alerts` |
| `/api/admin/operations` | `/internal/v1/admin/operations` |
| `POST /api/admin/preview` | `POST /internal/v1/admin/preview` |
| `POST /api/admin/execute` | `POST /internal/v1/admin/execute` |

Collector 的 `/health` 可直接检查数据库完整性和磁盘容量。`POST /api/v1/samples` 是 Agent 数据上报接口，不是查询接口。

没有特殊排障需求时，外部查询应使用 Console API；CLI 因为部署在可信内网中，会直接访问 Collector。

## 6. 统计口径和查询边界

### 6.1 当前状态统计

- 当前利用率只统计 `online` 和 `degraded` 节点的最新样本；
- `stale` 和 `offline` 节点保留登记容量与最近已知卡数，但不参与当前实时利用率；
- 集群利用率按所有新鲜逻辑卡/Die 加权，不采用“节点均值再平均”；
- HBM 百分比按 `总已用 HBM / 总 HBM` 计算；
- 910B 通常为 8 个逻辑卡，910C 一卡双 Die 时通常为 16 个逻辑 Die，均按 Agent 实际上报的 `card_id` 统计。

### 6.2 历史趋势统计

- 每个时间桶对其中所有逐卡记录计算平均、最低和最高利用率；
- `coverage_percent` 根据时间桶内采样的预期卡次数和实际采集卡次数计算；
- HBM 仅统计同时存在已用量和总量的卡；
- 没有逐卡数据但存在失败采样的时间桶仍可能返回，利用率为 `null`、覆盖率为 0。

### 6.3 热数据与归档数据

- 普通历史、采样、告警和导出接口查询 Collector 热库；
- 默认热数据保留 180 天，超过 180 天后自动转入归档 SQLite，不会直接删除；
- 当前普通查询接口不会自动跨热库和归档库查询；
- 数据管理功能可以在明确预览、确认并备份后选择处理归档数据，这不属于普通查询行为。

因此，查询超过热数据窗口的长期历史时，应直接保留日常导出的 CSV/XLSX，或后续增加专门的归档查询能力。

## 7. 常见问题

### 页面显示节点存在，但集群实时卡数减少

节点可能已经是 `stale` 或 `offline`。过期数据不参与实时利用率，因此“登记卡数”仍保留，而“新鲜卡数”会减少。

### 查询结果为空

依次检查：

1. 时间范围和时区是否正确；
2. `end` 是否晚于 `start`；
3. 节点 ID、集群 ID 是否为精确值；
4. 数据是否已经超过 180 天并进入归档；
5. Collector 健康状态：`curl -sS http://主节点IP:18080/health`。

### 为什么同一分钟的利用率会变化

`npu-smi info` 返回实时值，不同采样时刻的利用率和 HBM 占用发生变化属于正常现象。历史查询显示的是选定时间桶内逐卡样本的聚合值。

### 为什么 Web 和 CLI 的结果看起来不同

先确认二者使用了相同的 Collector、集群、节点、时间范围、采样状态和聚合粒度。Web 默认最近 24 小时，当前状态卡片则始终展示最新快照，不受顶部历史时间范围影响。
