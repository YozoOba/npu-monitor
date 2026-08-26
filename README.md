# NPU 算力利用率监控工具

当前项目同时保留单机版与集群版。集群版拆分为 `agent/`、`collector/`、`console/` 三个可独立构建和部署的组件，采用 Agent 主动 POST、磁盘断线队列、SQLite 去重存储以及独立查询展示服务。

集群 Agent 同时保留每日 CSV，并使用 Python 标准库每天生成一次月度 XLSX；每个已完成 UTC 日期对应一个 Sheet，不需要安装额外表格处理包。

集群查询支持多集群分组、节点搜索分页、自定义时间范围、节点/单卡历史、原始采样和告警生命周期查询，并可直接导出逐卡 CSV 或 XLSX；Console 还提供带影响预览、确认令牌、自动 SQLite 备份和操作审计的错误节点/错误数据管理。全部运行功能仍只依赖 Python 标准库。

- 集群部署与配置：[使用指导.md](使用指导.md)
- 离线集群部署方案：[DEPLOYMENT_PLAN.md](DEPLOYMENT_PLAN.md)
- 集群测试与验收：[测试指南.md](测试指南.md)
- 原单机采集器仍位于项目根目录，下面内容是其兼容说明。

---

## 单机版兼容说明

## 工具简介

纯Python标准库实现，无任何第三方依赖，专为离线NPU环境设计。每分钟采集NPU利用率数据，存储为CSV文件，提供每日、每周、每月统计查询。

## 核心特性

✅ **零依赖**：仅使用Python标准库，断网环境直接运行
✅ **纯文件存储**：CSV格式，易于查看、备份、迁移
✅ **资源占用低**：每分钟采集一次，不影响业务
✅ **冷热归档**：180天后移入归档目录，不直接删除历史数据

---

## 数据采集方式

### 采集命令
```bash
npu-smi info
```

### 采集频率
- **默认**：每60秒采集一次
- **可调整**：设置环境变量 `NPU_MONITOR_COLLECT_INTERVAL`

### 运行时配置

| 环境变量 | 默认值 | 说明 |
|------|------:|------|
| `NPU_MONITOR_COLLECT_INTERVAL` | 60 | 采集间隔（秒） |
| `NPU_MONITOR_RETENTION_DAYS` | 180 | 热目录保留天数，超期文件转入归档 |
| `NPU_MONITOR_ARCHIVE_DIR` | `data/archive` | 超过热数据期限后的永久归档目录 |
| `NPU_MONITOR_EXPECTED_NPU_COUNT` | 8 | 预期卡数；数量不符时保留已采卡并标记覆盖率 |
| `NPU_MONITOR_COMMAND_TIMEOUT` | 10 | `npu-smi` 超时（秒） |
| `NPU_MONITOR_LOG_MAX_BYTES` | 10485760 | 单个日志文件最大字节数 |
| `NPU_MONITOR_LOG_BACKUP_COUNT` | 5 | 日志轮转备份数 |
| `NPU_MONITOR_MIN_FREE_BYTES` | 104857600 | 数据盘最小剩余字节数 |
| `NPU_MONITOR_MIN_FREE_INODES` | 1000 | 数据盘最小剩余 inode 数 |

### 采集指标
- NPU卡号（自动识别所有卡）
- AICORE利用率（百分比）

### 解析规则
正则匹配以下模式：
- `AICORE Utilization : XX.X %`
- `Utilization : XX.X %`

---

## 平均利用率计算方式

### 计算公式
```
平均利用率 = Σ(所有采集点利用率) / 采集点总数
```

### 统计维度
| 维度 | 时间范围 | 采集点数量（估算） |
|------|----------|---------------------|
| 每日 | 当天 00:00-23:59 | 1440 × 卡数 |
| 每周 | 周一至周日 | 10080 × 卡数 |
| 每月 | 当月全部天数 | 约43200 × 卡数 |

### 示例
假设4张卡，每分钟采集一次：
- **每日**：1440分钟 × 4卡 = 5760个数据点
- **日平均**：5760个点的平均值

---

## 数据存储方式

### 文件结构
```
npu-monitor/
├── data/
│   ├── daily/              # 每日数据文件
│   │   ├── stats_2026-08-01.csv
│   │   ├── stats_2026-08-02.csv
│   │   └── ...
│   ├── logs/               # 运行日志
│   │   ├── npu_monitor.log
│   │   └── monitor.out
│   └── npu_monitor.pid     # 进程ID文件
├── npu_monitor.py          # 主监控脚本
├── query_stats.py          # 查询脚本
├── start.sh                # 启动脚本
├── stop.sh                 # 停止脚本
├── status.sh               # 状态脚本
├── config.py               # 配置文件
└── README.md               # 本文档
```

### CSV文件格式
**文件名**：`stats_YYYY-MM-DD.csv`（每天一个文件）

**字段说明**：
```
timestamp,card_id,utilization,hbm_used_mb,hbm_total_mb
2026-08-05T14:30:00+08:00,0,45.5,1024,65536
2026-08-05T14:30:00+08:00,1,78.2,2048,65536
2026-08-05T14:30:00+08:00,2,32.1,512,65536
2026-08-05T14:30:00+08:00,3,56.8,4096,65536
```

| 字段 | 类型 | 说明 |
|------|------|------|
| timestamp | datetime | 采集时间（精确到秒） |
| card_id | int | NPU卡号（0, 1, 2...） |
| utilization | float | 利用率百分比（0-100） |

### 存储空间估算
- **单条记录**：约40字节
- **每天文件大小**：1440分钟 × 卡数 × 40字节
  - 4张卡：约230KB/天
  - 8张卡：约460KB/天
- **180天总大小**：
  - 4张卡：约40MB
  - 8张卡：约80MB

### 日志文件
- `npu_monitor.log`：带自动轮转的主运行日志
- `monitor.out`：后台启动失败等早期输出

---

## 安装部署

### 环境要求
- Python 3.6+
- Linux系统
- npu-smi工具已安装

### 快速部署
```bash
# 1. 拷贝到NPU设备
scp -r npu-monitor/ user@npu-device:/home/user/

# 2. 登录设备
ssh user@npu-device

# 3. 进入目录
cd /home/user/npu-monitor

# 4. 设置权限
chmod +x setup.sh && ./setup.sh

# 5. 启动监控
./start.sh
```

### 容器运行建议

当前离线集群不依赖 Docker `--init`。项目提供 `deploy/mini_init.py` 作为容器 PID 1，负责转发信号和回收孤儿进程；不需要安装软件、重新制作镜像或重启 Docker daemon。

Collector使用自身的接收时间判断节点是否 `online/stale/offline`，固定的Agent
时钟偏差不会导致节点离线；时钟告警只关注相邻采样之间的偏移变化。Agent的
原始采样时间仍用于历史数据和断线补传。

集群版直接复用本机已有镜像：

```bash
export NPU_AGENT_NODE_ID=npu-node-01
export NPU_AGENT_CLUSTER_ID=training-a
export NPU_AGENT_COLLECTOR_URL=http://主节点IP:18080
./deploy/create_agent_container.sh 本机镜像ID /work/monitor
```

Agent 容器始终使用 `--privileged=true`，并动态发现宿主机的
`/dev/davinciN` 设备。HCCL/HCCN 挂载默认为 `auto`：检测到310P时跳过，
910B/910C文件齐全时自动保留。也可以在创建容器前设置
`NPU_AGENT_HCCL_MOUNTS=disabled` 或 `enabled` 强制覆盖。

910C按 `npu-smi info` 的 `Phy-ID 0..15` 监控16个逻辑Die；Agent发现编号
超过默认的8时会自动扩展预期数量。正式部署910C时仍建议显式设置
`NPU_AGENT_EXPECTED_CARDS=16`，以便第一次采样就能准确发现任意缺失Die。

不要在容器中使用 `nohup python3 ... &`。已有僵尸进程不能被 `kill`，只能由父进程回收或通过停止旧容器清除。

可将下面的命令配置为 Docker 健康检查：

```bash
python3 /app/healthcheck.py
```

健康检查会把数据采集停滞或失败判为不健康；少量卡缺失属于 `DEGRADED`，会显示缺卡和覆盖率，但不会触发容器重启风暴。

---

## 使用说明

### 启动监控
```bash
./start.sh
```

输出：
```
Starting NPU Monitor...
NPU Monitor started successfully (PID: 12345)
Log file: /home/user/npu-monitor/data/logs/npu_monitor.log
Data directory: /home/user/npu-monitor/data/daily
```

### 检查状态
```bash
./status.sh
```

状态输出除了进程状态，还包括采集器健康状态、最后成功时间、卡覆盖率、缺失卡号和连续失败次数。详细状态保存在 `data/health.json`；每轮采样覆盖率保存在 `data/sample_status/samples_YYYY-MM-DD.jsonl`。

输出：
```
NPU Monitor Status
==================
Status: RUNNING
PID: 12345

Process Info:
  PID  ELAPSED CMD
12345    00:15 python3 npu_monitor.py

Data Files:
  CSV files: 7
  Latest file: stats_2026-08-05.csv (5761 lines, 230K)
```

### 停止监控
```bash
./stop.sh
```

---

## 统计查询

### 查询命令

#### 查询今日统计
```bash
python3 query_stats.py --today
```

#### 查询昨日统计
```bash
python3 query_stats.py --yesterday
```

#### 查询本周统计
```bash
python3 query_stats.py --week
```

#### 查询本月统计
```bash
python3 query_stats.py --month
```

#### 查询指定日期
```bash
python3 query_stats.py --date 2026-08-01
```

#### 查询指定月份
```bash
python3 query_stats.py --monthly 2026-08
```

#### 查询日期范围
```bash
python3 query_stats.py --range 2026-08-01 2026-08-07
```

### 输出示例
```
Daily: 2026-08-05
======================================================================
Card ID   Samples     Avg %       Max %       Min %
----------------------------------------------------------------------
0         1440        65.32       98.50       12.10
1         1440        72.18       99.20       15.30
2         1440        58.45       95.80       8.50
3         1440        69.92       97.60       18.20
----------------------------------------------------------------------
Overall   5760        66.47
======================================================================
```

**字段说明**：
- **Card ID**：NPU卡号
- **Samples**：采集样本数
- **Avg %**：平均利用率
- **Max %**：最大利用率
- **Min %**：最小利用率
- **Overall**：整体平均值

---

## 配置调整

通过容器环境变量调整配置，例如：

```bash
export NPU_MONITOR_COLLECT_INTERVAL=60
export NPU_MONITOR_RETENTION_DAYS=180
export NPU_MONITOR_EXPECTED_NPU_COUNT=8
```

**建议**：
- 生产环境：保持60秒
- 测试环境：可改为300秒

---

## 数据管理

### 查看原始数据
```bash
# 查看今天的数据
cat data/daily/stats_2026-08-05.csv

# 统计记录数
wc -l data/daily/stats_2026-08-05.csv
```

### 备份数据
```bash
# 打包所有数据
tar -czf npu-data-backup.tar.gz data/

# 打包指定月份
tar -czf npu-data-2026-08.tar.gz data/daily/stats_2026-08-*.csv
```

### 迁移数据
```bash
# 拷贝到其他设备
scp -r data/daily/ user@another-device:/path/to/npu-monitor/data/
```

### 归档旧数据
程序自动把超过 180 天的文件移到 `data/archive/`，不会直接删除。归档数据默认无限期保留；如需释放空间，请先完成离线备份，再由管理员人工处理：
```bash
# 查看已归档数据
find data/archive -type f -print

# 将归档复制到其他存储（示例）
tar -czf npu-data-archive-backup.tar.gz data/archive/
```

---

## 性能影响评估

### 资源占用
| 项目 | 占用 |
|------|------|
| CPU | < 2% （仅执行npu-smi查询） |
| 内存 | < 20MB |
| 磁盘写入 | 230KB/天（4张卡） |
| 热目录磁盘占用 | 约40MB（180天，4张卡）；归档目录会持续增长 |

### 对业务的影响
- ✅ **极低影响**：每分钟一次只读查询
- ✅ **不抢占资源**：优先级低，不干扰计算任务
- ✅ **存储友好**：数据量小，不会撑爆磁盘

---

## 故障排查

### 问题1：启动失败
**检查**：
```bash
# 测试npu-smi命令
npu-smi info

# 查看错误日志
cat data/logs/monitor.out
```

### 问题2：无法采集数据
**可能原因**：
1. npu-smi命令不可用
2. 输出格式不匹配

**解决**：
提供实际npu-smi输出，调整 `npu_monitor.py` 的解析规则

### 问题3：CSV文件为空
**检查**：
```bash
# 确认程序运行
./status.sh

# 查看日志
tail -50 data/logs/npu_monitor.log
```

---

## 与总部探针对比

| 项目 | 总部探针 | 本工具 |
|------|---------|--------|
| 采集方式 | npu-smi info | npu-smi info |
| 采集频率 | 每分钟 | 每分钟（可调整） |
| 计算方式 | 全量求和平均 | 全量求和平均 |
| 数据存储 | 总部数据库 | 本地CSV文件 |
| 统计维度 | 日/周/月 | 日/周/月/任意时段 |
| 网络要求 | 需联网上报 | 完全离线 |

---

## 常见问题

**Q：数据文件会无限增长吗？**
A：热目录不会无限增长；180 天前的数据会转入 `data/archive/`。归档不会自动删除，因此需要监控归档盘容量并定期备份。

**Q：可以修改保留天数吗？**
A：可以，设置 `NPU_MONITOR_RETENTION_DAYS` 环境变量；它控制热数据窗口，不控制历史数据寿命。

**Q：CSV文件可以直接用Excel打开吗？**
A：可以，CSV是标准格式。

**Q：如何修改采集间隔？**
A：设置 `NPU_MONITOR_COLLECT_INTERVAL` 环境变量（单位：秒）。

**Q：支持多少张NPU卡？**
A：自动识别卡号，并通过 `NPU_MONITOR_EXPECTED_NPU_COUNT` 校验预期卡数。

---

## 快速参考

```bash
# 启动
./start.sh

# 状态
./status.sh

# 查询
python3 query_stats.py --today
python3 query_stats.py --month

# 停止
./stop.sh

# 备份
tar -czf backup.tar.gz data/daily/
```

---

**版本**：v2.0（纯文件版本）
**更新日期**：2026-08-05
**依赖**：Python 3.6+（标准库）
