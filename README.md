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

## HTTP 服务

```bash
PYTHONPATH=src python3 -m robot_control.api --database robot-control.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m embodied_ai.api --database embodied-ai.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m component_qualification.api --database component.sqlite3 --host 127.0.0.1 --port 8082
```

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 测量写入契约（国产电子部件域）

测量在**写入边界**完成契约校验，任何不合规值（含测试台偶发产生的量程溢出 `Infinity`）都不会进入业务表或审计链：

- 请求体按严格 JSON 解析：拒绝 `Infinity/-Infinity/NaN` 常量与重复键，数值保持十进制精度；
- `signal_frequency_hz` ∈ [1, 1e9] Hz、`response`/`noise` ∈ [0, 1]，必须是有限数值（拒绝字符串伪装、布尔、null）；
- `instrument` 必须符合身份格式且事先注册（`POST /instruments`）；
- 批次内重复采样（相同 `sample_id` 或相同测点指纹）被拒绝；
- 单条或批量失败一律原子回滚：零业务行、零审计事件；批量写入携带 `Idempotency-Key`，等价重放返回逐字稳定的结果（同键不同内容返回 409）；
- 校验失败响应在 `error.violations` 中给出每条违规的 `index`/`field`/`rule`/`reason`，测试台可直接定位。

存量非法数据不会在读取时被悄悄丢弃：打开旧版数据库时自动迁移（v1→v2），非法测量进入 `measurement_quarantine` 隔离区并写审计事件；也可随时 `POST /lots/{lot}/quarantine/scan` 主动扫描。隔离记录只能由质量角色 `POST /quarantine/{id}/resolve` 处置为 `discard`（废弃留痕）或 `release`（必须重新通过契约并查重后才回业务表）。`GET /lots/{lot}/quality-report` 的质量结论只使用业务表中可追溯的有效测量。

