# NPU Monitor 单机测试指南

## 1. 测试目标

本文档用于在一台现有的昇腾 910B 容器内测试完整的数据链路：

```text
NPU Agent -> Collector -> SQLite -> Console CLI/Web
```

本次测试具有以下约束：

- 不创建新容器；
- 项目代码固定放在 `/work/monitor`；
- 测试数据单独写入 `/work/monitor-test`；
- 不使用正式数据目录 `/work/monitor/data`；
- Agent、Collector 和 Console 全部以前台方式运行；
- 不使用 `nohup`，也不在 Python 命令后添加 `&`；
- 测试完成后可以完整删除 `/work/monitor-test`。

建议准备三个可以进入同一个现有容器的终端：

- 终端 A：运行 Collector；
- 终端 B：运行 Agent 和命令行查询；
- 终端 C：运行 Console Web。

## 2. 初始化测试环境

以下命令全部在现有 910B 容器内执行：

```bash
cd /work/monitor

export MONITOR_ROOT=/work/monitor
export TEST_ROOT=/work/monitor-test
export PYTHONPATH=/work/monitor

mkdir -p "$TEST_ROOT/agent"
mkdir -p "$TEST_ROOT/collector"
```

检查项目文件是否完整：

```bash
test -f "$MONITOR_ROOT/agent/app.py"
test -f "$MONITOR_ROOT/collector/app.py"
test -f "$MONITOR_ROOT/console/cli.py"
test -f "$MONITOR_ROOT/console/web.py"
test -f "$MONITOR_ROOT/cluster_common/protocol.py"

echo "Source tree is ready"
```

检查 Python、SQLite、NPU 和 PID 1：

```bash
python3 --version
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"
npu-smi info
ps -o pid,ppid,stat,args -p 1
```

验收条件：

- Python 版本不低于 3.6；
- `sqlite3` 能输出版本号，不需要安装独立 SQLite 服务；
- `npu-smi info` 能正常看到本机 8 张 910B 卡；
- 可以查看容器 PID 1 的进程信息。

即使当前容器 PID 1 不是 init，也可以进行本次前台测试，但不要把 Python 转到后台运行。

## 3. 运行自动化测试

```bash
cd /work/monitor
export PYTHONPATH=/work/monitor

python3 -m unittest discover -s tests -p 'test_cluster.py' -v
python3 test_tool.py
```

预期输出：

```text
Ran 12 tests
OK

Ran 14 tests
OK
```

其中：

- 12 项集群测试覆盖协议、SQLite、去重、冲突检测、HTTP 接收、补传队列和 Console；
- 14 项原单机测试用于确认原有 `npu-smi` 解析、CSV 和进程逻辑没有回归；
- 自动化测试使用临时目录，不会修改正式数据。

## 4. 验证真实 npu-smi 解析

```bash
cd /work/monitor
export PYTHONPATH=/work/monitor

python3 - <<'PY'
from agent.sampler import collect

cards, error = collect('npu-smi', 10)

print('error:', error)
print('card_count:', len(cards))

for card in cards:
    print(card)

if error:
    raise SystemExit(1)

if len(cards) != 8:
    print('FAILED: expected 8 cards')
    raise SystemExit(2)

if [card['card_id'] for card in cards] != list(range(8)):
    print('FAILED: expected card IDs 0 through 7')
    raise SystemExit(3)

print('SUCCESS: parsed all 8 cards')
PY
```

再执行原始命令进行人工对比：

```bash
npu-smi info
```

需要确认：

- 一共识别到 8 张卡；
- 卡号为 `0` 到 `7`；
- 不存在重复卡号；
- 每张卡的 `utilization` 与 `npu-smi info` 输出一致；
- 如果原始输出包含 HBM，解析出的使用量和总量也应一致。

如果解析失败，保存原始输出：

```bash
npu-smi info > /work/monitor-test/npu-smi-info.txt
```

真实解析没有通过前，不建议继续进行数据准确性验收。

## 5. 在终端 A 启动 Collector

打开终端 A，进入同一个现有容器，然后执行：

```bash
cd /work/monitor

export PYTHONPATH=/work/monitor
export NPU_COLLECTOR_DATA_DIR=/work/monitor-test/collector
export NPU_COLLECTOR_PORT=28080
export NPU_COLLECTOR_RETENTION_DAYS=7
export NPU_COLLECTOR_MIN_FREE_BYTES=0
export NPU_COLLECTOR_MIN_FREE_INODES=0

python3 -u -m collector.app
```

预期日志：

```text
collector listening on 0.0.0.0:28080
```

保持终端 A 和 Collector 前台进程持续运行。

本次使用测试端口 `28080`，避免与已有监控服务的默认端口冲突。

## 6. 在终端 B 检查 Collector

打开终端 B，进入同一个容器：

```bash
cd /work/monitor
export PYTHONPATH=/work/monitor

curl -sS http://127.0.0.1:28080/health
```

如果容器没有安装 `curl`，使用 Python 标准库：

```bash
python3 - <<'PY'
import urllib.request

url = 'http://127.0.0.1:28080/health'
response = urllib.request.urlopen(url, timeout=3)
print(response.read().decode('utf-8'))
PY
```

预期包含：

```text
status: healthy
database_integrity: ok
schema_version: 1
sample_count: 0
```

检查 Collector 数据文件：

```bash
find /work/monitor-test/collector -maxdepth 1 -type f -print
```

预期包含：

```text
cluster.sqlite3
cluster.sqlite3-wal
cluster.sqlite3-shm
latest_snapshot.json
```

## 7. 执行一次真实 Agent 采集

保持 Collector 运行，在终端 B 执行：

```bash
cd /work/monitor

export PYTHONPATH=/work/monitor
export NPU_AGENT_NODE_ID=single-node-910b-01
export NPU_AGENT_NODE_NAME=single-node-910b-01
export NPU_AGENT_COLLECTOR_URL=http://127.0.0.1:28080
export NPU_AGENT_DATA_DIR=/work/monitor-test/agent
export NPU_AGENT_EXPECTED_CARDS=8
export NPU_AGENT_COLLECT_INTERVAL=60
export NPU_AGENT_COMMAND_TIMEOUT=10
export NPU_AGENT_HTTP_TIMEOUT=5
export NPU_AGENT_RETENTION_DAYS=7
export NPU_AGENT_SPOOL_RETENTION_DAYS=7
export NPU_AGENT_MIN_FREE_BYTES=0
export NPU_AGENT_MIN_FREE_INODES=0

python3 -u -m agent.app --once

echo "agent_exit_code=$?"
```

正常情况下应看到：

```text
8/8 cards
status=complete
agent_exit_code=0
```

`--once` 表示只采集、保存和上报一轮，然后退出。

## 8. 检查 Agent 本地数据

查看生成的文件：

```bash
find /work/monitor-test/agent -maxdepth 2 -type f -print | sort
```

检查采集健康状态：

```bash
python3 -m json.tool /work/monitor-test/agent/health.json
```

预期字段：

```text
status: healthy
sample_status: complete
expected_cards: 8
collected_cards: 8
coverage_percent: 100.0
```

检查上传健康状态：

```bash
python3 -m json.tool /work/monitor-test/agent/upload_health.json
```

预期字段：

```text
pending_samples: 0
consecutive_failures: 0
```

检查本地 CSV 和采样状态：

```bash
UTC_DATE=$(date -u +%F)

cat "/work/monitor-test/agent/daily/stats_${UTC_DATE}.csv"
cat "/work/monitor-test/agent/sample_status/samples_${UTC_DATE}.jsonl"
```

完整采样时，CSV 应包含一行表头和 8 行卡数据。

检查待上传队列：

```bash
find /work/monitor-test/agent/spool -maxdepth 1 -name '*.json' | wc -l
```

Collector 正常时，预期结果为：

```text
0
```

## 9. 使用 Console CLI 查询

Console 查询代码与 Collector 代码相互独立。保持 Collector 运行，在终端 B 执行：

```bash
cd /work/monitor
export PYTHONPATH=/work/monitor

python3 -m console.cli \
  --collector-url http://127.0.0.1:28080 \
  summary
```

预期看到：

```text
Nodes: 1
Cards: 8/8
coverage=100.00%
single-node-910b-01 online
```

查询节点明细：

```bash
python3 -m console.cli \
  --collector-url http://127.0.0.1:28080 \
  nodes
```

查询最近一小时历史：

```bash
python3 -m console.cli \
  --collector-url http://127.0.0.1:28080 \
  history --hours 1 --bucket 60
```

人工对比 Console 当前利用率与本轮 CSV 中 8 张卡的平均值：

```text
cluster utilization = sum(card utilization) / valid card count
```

不能使用“先算节点平均值，再平均节点平均值”的方式进行集群计算。

## 10. 在终端 C 启动 Console Web

打开终端 C，进入同一个容器：

```bash
cd /work/monitor

export PYTHONPATH=/work/monitor
export NPU_CONSOLE_COLLECTOR_URL=http://127.0.0.1:28080
export NPU_CONSOLE_PORT=28081

python3 -u -m console.web
```

预期日志：

```text
console listening on 0.0.0.0:28081
```

保持终端 C 前台运行，在终端 B 检查：

```bash
curl -sS http://127.0.0.1:28081/health
curl -sS http://127.0.0.1:28081/api/snapshot
curl -sS 'http://127.0.0.1:28081/api/history?hours=1&bucket=60'
```

如果现有容器创建时没有发布 `28081`，宿主机浏览器无法直接访问该端口，但不影响容器内的 API 测试。

如果容器使用 host 网络或者该端口已经发布，可以访问：

```text
http://SERVER_IP:28081/
```

页面应显示：

- 节点总数；
- 在线、异常、过期和离线节点数；
- 有效卡数和预期卡数；
- 当前集群利用率；
- HBM 使用率；
- 最近 24 小时趋势；
- 节点明细表格。

## 11. 验证重复数据保护

重复和冲突测试已经包含在集群自动化测试中，可以再次运行：

```bash
cd /work/monitor
export PYTHONPATH=/work/monitor

python3 -m unittest discover -s tests -p 'test_cluster.py' -v
```

应满足：

- 第一份有效采样返回 HTTP 201；
- 完全相同的采样再次上报时返回 HTTP 200；
- 重复上报不会增加数据库采样数量；
- 相同节点、相同采样时间却包含不同测量值时返回 HTTP 409；
- 冲突数据不会覆盖数据库中已经接受的数据。

## 12. 测试断线队列和恢复补传

在终端 A 按 `Ctrl+C` 停止 Collector。

然后在终端 B 执行一次 Agent：

```bash
cd /work/monitor
export PYTHONPATH=/work/monitor

python3 -u -m agent.app --once
```

Collector 不可用时，本地采集仍应保存，但上报数据会留在磁盘队列。

检查队列：

```bash
find /work/monitor-test/agent/spool -maxdepth 1 -name '*.json' -print
python3 -m json.tool /work/monitor-test/agent/upload_health.json
```

预期：

- `spool` 至少存在一个 JSON 文件；
- `pending_samples` 大于 0；
- `upload_health.json` 包含连接失败信息；
- 本地 CSV 和采样状态仍然新增了一轮数据；
- Agent 不会因为 Collector 不可用而删除本地数据。

在终端 A 重新启动 Collector：

```bash
cd /work/monitor

export PYTHONPATH=/work/monitor
export NPU_COLLECTOR_DATA_DIR=/work/monitor-test/collector
export NPU_COLLECTOR_PORT=28080
export NPU_COLLECTOR_RETENTION_DAYS=7
export NPU_COLLECTOR_MIN_FREE_BYTES=0
export NPU_COLLECTOR_MIN_FREE_INODES=0

python3 -u -m collector.app
```

在终端 B 再执行一次 Agent：

```bash
python3 -u -m agent.app --once
```

Agent 启动上传线程后，会处理之前积压的数据和本轮新数据。

检查队列是否清空：

```bash
find /work/monitor-test/agent/spool -maxdepth 1 -name '*.json' | wc -l
```

预期结果：

```text
0
```

检查补传历史：

```bash
python3 -m console.cli \
  --collector-url http://127.0.0.1:28080 \
  history --hours 1 --bucket 60
```

断线期间的采样应按照原始采样时间进入历史，不应因为补传而使用恢复时刻作为采样时间。

## 13. 测试部分采样

机器实际有 8 张卡时，将预期卡数临时设置为 9：

```bash
export NPU_AGENT_EXPECTED_CARDS=9
python3 -u -m agent.app --once
```

检查健康状态：

```bash
python3 -m json.tool /work/monitor-test/agent/health.json
```

预期：

```text
sample_status: partial
expected_cards: 9
collected_cards: 8
missing_card_ids: [8]
coverage_percent: 88.89
```

查询 Collector：

```bash
python3 -m console.cli \
  --collector-url http://127.0.0.1:28080 \
  summary
```

预期：

- 节点状态为 `degraded`；
- 已经采集到的 8 张卡不会被丢弃；
- 8 张有效卡仍参与利用率统计；
- 覆盖率的分母按照 9 张预期卡计算。

测试后恢复为 8 张卡：

```bash
export NPU_AGENT_EXPECTED_CARDS=8
```

## 14. 测试完全采集失败

通过指定不存在的命令进行故障注入，不需要修改源代码：

```bash
export NPU_AGENT_NPU_SMI_BIN=/not-exist/npu-smi

python3 -u -m agent.app --once

echo "agent_exit_code=$?"
```

预期：

- Agent 退出码非 0；
- `sample_status` 为 `failed`；
- `collected_cards` 为 0；
- 失败采样仍会写入采样状态文件；
- Collector 将其识别为“节点仍在上报，但本轮采集失败”；
- 节点状态为 `degraded`，而不是立即变成 `offline`。

测试后恢复真实命令：

```bash
unset NPU_AGENT_NPU_SMI_BIN
```

## 15. 运行短时间连续测试

保持终端 A 的 Collector 正常运行。在终端 B 使用 10 秒采集周期：

```bash
cd /work/monitor

export PYTHONPATH=/work/monitor
export NPU_AGENT_NODE_ID=single-node-910b-01
export NPU_AGENT_NODE_NAME=single-node-910b-01
export NPU_AGENT_COLLECTOR_URL=http://127.0.0.1:28080
export NPU_AGENT_DATA_DIR=/work/monitor-test/agent
export NPU_AGENT_EXPECTED_CARDS=8
export NPU_AGENT_COLLECT_INTERVAL=10
export NPU_AGENT_NPU_SMI_BIN=npu-smi
export NPU_AGENT_MIN_FREE_BYTES=0
export NPU_AGENT_MIN_FREE_INODES=0

python3 -u -m agent.app
```

持续运行至少两分钟，然后按 `Ctrl+C` 停止 Agent。

查询结果：

```bash
python3 -m console.cli \
  --collector-url http://127.0.0.1:28080 \
  summary

python3 -m console.cli \
  --collector-url http://127.0.0.1:28080 \
  history --hours 1 --bucket 60
```

检查待发送和拒绝目录：

```bash
find /work/monitor-test/agent/spool -maxdepth 1 -name '*.json' | wc -l
find /work/monitor-test/agent/rejected -maxdepth 1 -name '*.json' | wc -l
```

正常网络和正常数据情况下，两项结果都应为 0。

## 16. 停止所有前台服务

按以下顺序停止：

1. 在终端 B 按 `Ctrl+C` 停止 Agent；
2. 在终端 C 按 `Ctrl+C` 停止 Console；
3. 在终端 A 按 `Ctrl+C` 停止 Collector。

检查组件进程是否残留：

```bash
ps -eo pid,ppid,stat,args | grep -E \
  '[a]gent.app|[c]ollector.app|[c]onsole.web'
```

正常情况下不应有匹配进程。

检查僵尸进程：

```bash
ps -eo pid,ppid,stat,args | awk '$3 ~ /^Z/ {print}'
```

不应新增与本项目相关的 `Z` 状态进程。

Agent 的 HTTP 上传使用线程，不会为每次请求创建 Python 子进程；`npu-smi` 子进程由 `subprocess.run()` 同步等待并回收。

## 17. 检查 SQLite 完整性

Collector 停止后执行：

```bash
cd /work/monitor

export PYTHONPATH=/work/monitor
export NPU_COLLECTOR_DATA_DIR=/work/monitor-test/collector

python3 -m collector.app --check-db
```

预期包含：

```text
database_integrity: ok
schema_version: 1
node_count: 1
sample_count: greater than 0
```

该检查会直接打开测试数据库并执行 SQLite `quick_check`。

## 18. 单机测试通过标准

满足以下全部条件后，单机测试通过：

- 12 项集群自动化测试全部通过；
- 14 项原单机回归测试全部通过；
- 真实解析能够稳定识别 8 张 910B 卡；
- 卡号、利用率和 HBM 与 `npu-smi info` 一致；
- 完整采样时 Agent CSV 每轮包含 8 行卡数据；
- Collector 能接收数据并保持 SQLite 完整；
- 相同采样重复上报不会重复计数；
- 相同采样身份的冲突数据不会覆盖原数据；
- Console 显示 1 个节点和 8 张卡；
- 集群利用率与 8 张卡的人工平均值一致；
- Collector 中断时 Agent 继续保存本地数据；
- Collector 恢复后积压数据能够自动补传；
- 补传数据不会重复，并保留原始采样时间；
- 部分采样和失败采样状态正确；
- 所有前台服务可以通过 `Ctrl+C` 正常退出；
- 没有遗留 Agent、Collector 或 Console 进程；
- 没有新增与监控相关的僵尸进程。

## 19. 清理测试数据

先确认清理目标：

```bash
readlink -f /work/monitor-test
```

输出必须严格等于：

```text
/work/monitor-test
```

确认所有测试服务已经停止后，只删除测试目录：

```bash
rm -rf /work/monitor-test
```

不要删除 `/work/monitor`，该目录包含项目源代码。
