# 拦截会污染部件批次的非法测量值基础平台

本项目是一套可离线运行的 Python 服务端平台，服务于具身智能机器人控制域、AI 计算域和国产电子部件质量域。平台管理控制计算节点、实时控制总线、资源时隙、机器人构建、验证测量、分析租约、部件批次与质量决定，业务状态、幂等结果和审计事件保存在 SQLite 中。

## 目录

- `src/robot_control/`：控制节点、实时总线、资源批次、时隙申请、容量分配和架构情景；
- `src/embodied_ai/`：机器人构建、验证协议、测量导入、排除复核、分析任务和准入决定；
- `src/component_qualification/`：国产电子部件批次、信号测量、统计分析、账号权限和质量审批；
- `fixtures/`：离线验收使用的验证协议与结构化测量；
- `tests/`：领域规则、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m robot_control.acceptance --workspace .
PYTHONPATH=src python3 -m embodied_ai.acceptance --workspace .
PYTHONPATH=src python3 -m component_qualification.acceptance
```

三条命令会在临时 SQLite 数据库中完成控制资源分配、AI 验证分析和国产电子部件质量流程，不访问外部网络。

## 测量写入契约（国产部件域）

测量在**写入边界**接受契约校验，任何不合法记录都不会进入业务表或审计链：

- 字段：`sample_key`（采样去重身份）、`signal_frequency_hz`、`response`、`noise`、`instrument`；
- 规则：`required`、`type.number`/`type.string`、`finite`（拒绝 `NaN`/`Infinity`/`-Infinity` 及字符串伪装）、`range`（频率 `(0, 1e12]` Hz、响应 `[0, 2]`、噪声 `[0, 1)`）、`format`、`instrument.registered`（仪器须先登记且在用）、`duplicate`（采样键与批内/库内重复、或采样内容完全相同）；
- JSON 语法层拒绝重复键和非有限常量；成功响应也不允许输出非有限数值；
- 批量写入须带 `Idempotency-Key`，整批原子提交或回滚；等价重放返回首次结果；单条写入按内容自动派生幂等键。

校验失败返回 `422`，并在 `error.violations[]` 中逐条指明 `field` 与 `rule`。

### 隔离与处置

- 打开旧版数据库时自动迁移：合法旧记录补登 `sample_key` 继承新约束；非法记录（如被写成无穷大的量程溢出值）移入 `quarantined_measurements`，附带原因、原始快照与审计事件，不做静默丢弃；
- `POST /lots/{id}/quarantine-scan` 对库内记录复跑契约并原子隔离；
- `POST /quarantine/{id}/resolve` 由质量角色 `release`（重新校验通过后移回业务表）或 `discard`（作废并保留处置人与原因）；
- 质量分析在发现未隔离非法记录时直接报错（不读取时丢弃）；`GET /lots/{id}/quality-report` 只使用可追溯的有效测量，给出 `valid_measurement_ids` 与 `input_sha256`，并把隔离记录作为旁证列出。

```bash
# 对既有数据库执行隔离扫描
PYTHONPATH=src python3 -m component_qualification.maintenance --database component.sqlite3
PYTHONPATH=src python3 -m component_qualification.maintenance --database component.sqlite3 --list
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m robot_control.api --database robot-control.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m embodied_ai.api --database embodied-ai.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_qualification.api --database component.sqlite3 --host 127.0.0.1 --port 8082
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。
