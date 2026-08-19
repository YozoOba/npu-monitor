# NPU Monitor 离线集群部署方案

## 1. 目标和约束

本方案用于十几台 8 卡 Ascend 910B 机器，满足以下约束：

- 机器已有不同版本的 `vllm-ascend` 镜像；
- 不制作新的监控镜像；
- 不使用 Docker Compose；
- Docker `--init` 不可用；
- 不重启或修改 Docker daemon；
- Agent 到主节点的 `18080` 端口可达；
- 项目在宿主机统一放置于 `/work/monitor`。

## 2. 最终拓扑

```text
NPU node 01: Agent --\
NPU node 02: Agent ---+--> Collector:18080 --> SQLite + latest snapshot
...                  /                            |
NPU node NN: Agent --                             +--> Console:18081
```

- 每台 NPU 节点运行一个 Agent 容器；
- 主节点运行一个 Collector 容器；
- 主节点通常再运行一个 Console 容器；
- 三个角色可以复用同一个本地镜像，三个容器不等于三个镜像；
- Collector 只负责接收、校验、去重、存储和基础汇聚；
- Console 负责 Web、CLI 和 JSON 查询。

## 3. PID 1 和僵尸进程方案

所有角色统一使用项目内置的 `deploy/mini_init.py` 作为容器 PID 1：

```text
PID 1: mini_init.py
└── Agent / Collector / Console
    └── npu-smi 等临时子进程
```

mini-init 会：

- 在 Linux 上注册 child subreaper；
- 转发 SIGTERM、SIGINT、SIGHUP 和 SIGQUIT；
- 持续执行 `waitpid()` 回收退出子进程和孤儿后代；
- 保留主服务退出码；
- 主服务超时未退出时转发 SIGKILL。

因此部署中不使用 Docker `--init`、PID 文件、`nohup` 或后台 `&`。更换 PID 1 必须重新创建监控容器，但不需要重启 Docker daemon。

## 4. 部署入口

```text
deploy/mini_init.py
deploy/create_agent_container.sh
deploy/create_collector_container.sh
deploy/create_console_container.sh
```

三个创建脚本都只接收两个位置参数：

```text
IMAGE_ID_OR_NAME HOST_PROJECT_DIR
```

角色参数通过环境变量配置。脚本会检查镜像、项目文件、同名容器和必要的 NPU 挂载源；不会自动删除已有容器。

### 4.1 主节点

```bash
cd /work/monitor
chmod +x deploy/*.sh

./deploy/create_collector_container.sh 13315b656180 /work/monitor

export NPU_CONSOLE_COLLECTOR_URL=http://127.0.0.1:18080
./deploy/create_console_container.sh 13315b656180 /work/monitor
```

### 4.2 每台 NPU 节点

```bash
cd /work/monitor
export NPU_AGENT_NODE_ID=npu-node-01
export NPU_AGENT_NODE_NAME=910b-server-01
export NPU_AGENT_CLUSTER_ID=training-a
export NPU_AGENT_COLLECTOR_URL=http://192.168.10.20:18080
./deploy/create_agent_container.sh 本机镜像ID /work/monitor
```

每台机器必须使用不同且长期不变的 `NPU_AGENT_NODE_ID`。`NPU_AGENT_CLUSTER_ID` 用于集群或业务分组；未设置时归入 `default`，节点 ID 仍需在整个 Collector 中全局唯一。

## 5. 数据持久化

Agent 数据保存在每个节点：

```text
/work/monitor/runtime-data/agent/
├── daily/
├── monthly/               # stats_YYYY-MM.xlsx，每日一个 Sheet
├── sample_status/
├── spool/
├── rejected/
├── health.json
└── upload_health.json
```

Collector 数据保存在主节点：

```text
/work/monitor/runtime-data/collector/
├── cluster.sqlite3
├── cluster.sqlite3-wal
├── cluster.sqlite3-shm
└── latest_snapshot.json
```

默认本地采样、月度 XLSX 和 Collector 历史保留 180 天；Agent 待上传队列默认最多保留 7 天、20000 个文件或 512 MB。

Agent 继续将每日 CSV 作为可靠原始记录，并在每天第一次采集时用 Python 标准库原子更新月度 `stats_YYYY-MM.xlsx`。工作簿按 UTC 日期创建 Sheet，不依赖 `openpyxl` 等第三方包；当前日期的数据会在次日完成后进入 XLSX，监控开始后的数据缺失日期以空 Sheet 表示。

Collector 在 SQLite 中持久化原始采样、逐卡数据、节点最新状态、集群分组和告警生命周期。旧 schema 1 数据库会自动原地升级到 schema 2，不需要清库。Console 支持自定义时间、节点、单卡和集群筛选，原始采样分页，告警查询，以及最长默认 31 天的 CSV/XLSX 导出。

## 6. 日志和容器生命周期

所有创建脚本设置：

```text
restart policy: unless-stopped
stop timeout:   30 seconds
log driver:     json-file
max log file:   20 MB
log files:      5
```

这些都是单容器参数，不需要修改或重启 Docker daemon。运行日志通过 `docker logs` 查看，业务数据存储在项目的 `runtime-data/` 中。

## 7. 部署顺序

1. 主节点启动 Collector 并检查 `/health`；
2. 主节点启动 Console 并检查 `/health`；
3. 选择两台 NPU 节点灰度启动 Agent；
4. 验证 8 卡采集、上传、汇聚和 Web 展示；
5. 完成断网补传和 20 次容器启停测试；
6. 灰度稳定至少 24 小时；
7. 每批 3–5 台扩展到全部节点。

## 8. 验收标准

- Agent、Collector、Console 的 PID 1 均为 `mini_init.py`；
- 容器中不存在 `Z` 状态进程；
- Agent 每次完整采样包含 8 张卡；
- 节点本地 CSV 在网络中断时继续写入；
- 月度 XLSX 每天更新一次，Sheet 与已完成 UTC 日期一一对应；
- Collector 恢复后 `spool` 自动清空且数据不重复；
- 上传失败状态跨 Agent 重启保留；
- 集群利用率按所有新鲜有效卡加权；
- stale/offline 节点不参与当前利用率，但登记容量不消失；
- SQLite 完整性为 `ok`；
- 旧数据库升级后历史样本数量不变，旧 Agent 队列补传仍保持幂等；
- 单卡历史、原始采样分页、节点搜索、集群分组和告警生命周期查询正确；
- CSV/XLSX 导出逐卡行数和源数据一致，XLSX 可正常打开和筛选；
- Docker 日志轮转参数生效。

详细操作命令见 [使用指导.md](使用指导.md)，完整测试步骤见 [测试指南.md](测试指南.md)。
