# SDK 形态使用指南（显式集成）

`agentarts-instrumentation-openjiuwen` 以**普通 Python 库**的方式使用：你在代码里显式组装 exporter，并挂到 openjiuwen 的官方观测钩子（或任意 `TracerProvider`）上。适用于：

- 开发期/联调期，希望在代码里**可见可控**地集成上报
- 需要定制投影规则（drop/rename/truncate、附加属性）
- 宿主进程结构简单（单进程、自己掌控初始化流程）
- 想先做**连通性验证**再决定是否上探针形态

探针式（零代码注入）形态见 [guide-probe.md](guide-probe.md)。两种形态可随时互切。

## 1. 前置准备：获取访问凭证

在 AgentArts 控制台创建接入（**观测优化 > 可观测 > Agent 列表 > 接入 Agent**），拿到以下环境变量（官方变量名，2026-11-20 对齐）：

| 变量 | 必填 | 说明 |
|---|---|---|
| `agentarts_agentops_apm_token` | ✓ | Trace 上报令牌 |
| `agentarts_agentops_apm_exporter_endpoint` | ✓ | 接入网关，如 `https://apm-access.cn-southwest-2.myhuaweicloud.com` |
| `agent_resource_id` | ✓ | Agent 资源 ID（写入 `gen_ai.resource.id`） |
| `resource_type` |  | 默认 `agent` |
| `agent_domain_id` |  | 租户隔离（写入 `domain.id`） |
| `agentarts_project_id` |  | 写入 `gen_ai.project.id` |
| `AGENTARTS_USER_ID` |  | 写入 `gen_ai.user.id` |

## 2. 安装

```bash
# 预览版（v0.1.0a2 起为单包自包含 wheel，无需再装 core）
pip install \
  https://github.com/huaweicloud/agentarts-instrumentation-python/releases/download/v0.1.0a4/agentarts_instrumentation_openjiuwen-0.1.0a4-py3-none-any.whl \
  opentelemetry-exporter-otlp-proto-grpc   # gRPC 通道，AgentArts 网关必需
```

> 实测（2026-11）：apm-access 网关只发布 gRPC 通道，HTTP 返回 404，所以 gRPC exporter 必装。

## 3. 用法一：组装 exporter 并做连通性测试（推荐先跑）

用法一/二/三共用同一段 exporter 组装代码，本节先给出并用脚本实测。仓库自带的 [examples/report_to_agentarts.py](../../examples/report_to_agentarts.py) 手工构造一条静态 trace（AGENT + LLM 两级 span）直发平台：

```bash
set agentarts_agentops_apm_token=...
set agentarts_agentops_apm_exporter_endpoint=https://apm-access.cn-southwest-2.myhuaweicloud.com
set agent_resource_id=...

python openjiuwen/examples/report_to_agentarts.py
```

脚本核心是下面的 exporter 组装——SDK 标准组装方式，用法二复用同一 `exporter` 对象：

```python
import os

from agentarts_instrumentation import ExporterConfig, Projection, build_exporter
import agentarts_instrumentation.openjiuwen  # 导入即注册 agentarts 派生，必须

# 取值自第 1 节的环境变量
trace_endpoint = os.environ["agentarts_agentops_apm_exporter_endpoint"]
trace_token = os.environ["agentarts_agentops_apm_token"]
agent_id = os.environ["agent_resource_id"]

exporter = build_exporter(
    ExporterConfig(
        endpoint=trace_endpoint,
        protocol="grpc",                        # 网关仅 gRPC
        headers={"authentication": trace_token},  # HTTP/2 头要求小写
        projection=Projection(
            preset="agentarts",                 # 启用 AgentArts 平台语义投影
            service_name=f"AgentArts.{agent_id}.default",
            attributes={                        # 平台公共属性
                "gen_ai.resource.id": agent_id,
                "gen_ai.resource.type": "agent",
            },
        ),
    )
)
```

跑完到控制台该 Agent 的**追踪分析**视图确认 echo 出现。这一步验证了 endpoint/token/agent_id 三要素匹配，与后续宿主集成的问题域解耦；用法二在此基础上直接挂载同一 `exporter`。

## 4. 用法二：挂到 openjiuwen 宿主（核心场景）

exporter 是进程内对象：第 3 节组装代码必须与挂载点写在**同一处**，不能跨文件/跨进程"复用"。按宿主形态二选一。

### 4.1 自建宿主：你自己调 `init_observability`（agent-core 独立使用即此形态）

```python
from openjiuwen.extensions.observability import init_observability

init_observability(
    config,
    span_exporter_override=exporter,   # SDK exporter 成为 span 汇出口
)
```

team 场景用 `openjiuwen.agent_teams.observability.init_observability`，签名相同。

### 4.2 jiuwenswarm 宿主：按宿主版本二选一

**宿主 openjiuwen ≥0.1.20：免改源码**。启动钩子里再调一次 `init_observability`，provider 已存在时 runtime 会把 SDK processor 追加进去（file 主导出 + SDK 双路共存）：

```python
from openjiuwen.extensions.observability import get_config
from openjiuwen.extensions.observability.config import ObservabilityConfig

cfg = get_config() or ObservabilityConfig(enabled=True, service_name="jiuwenswarm")
init_observability(cfg, span_exporter_override=exporter)
```

**0.1.16（dev-stable）：追加语义为静默 no-op，需改一处源码**。落点 `jiuwenswarm/telemetry/runtime.py` 的 `_start_agent_components`——组装代码放同函数，processor 追加进元组：

```python
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

extra_processors = (
    (trajectory_processor,) if trajectory_processor is not None else ()
) + ((SimpleSpanProcessor(exporter),) if exporter else ())
```

两种方式挂上的都是全局 provider，team 路径的 span 同样被导出。不想改宿主源码 → 探针形态（[guide-probe.md](guide-probe.md)）。

### 4.3 最佳实践清单

- **先跑用法一**：三要素连通性验证先于宿主集成，问题域解耦
- **凭证缺失要可跳过**：`os.environ.get(...)` 取值 + None 判断，本地无 env 时跳过组装而不是崩溃
- **exporter 只导出不产生**：span 由宿主产生——jiuwenswarm 需在 `config.yaml` 显式设 `agent_observability.enabled: true`（默认 false，不开无数据）
- **零阻塞**：`SimpleSpanProcessor` + 本 SDK（内部缓冲队列）在 span 结束时只做入队，业务线程不等待网络
- **退出自动排空**：进程关闭链上 provider.shutdown 会传播到 exporter.shutdown，缓冲队列排空后才退出

从此 openjiuwen 产生的所有 span（agent / llm / tool / iteration）在导出时流经：**投影管线**（openjiuwen 数据契约 → `gen_ai.*` 平台语义）→ **缓冲队列**（计数+字节双限、后台线程批量、退避重试）→ OTLP gRPC → AgentArts。

## 5. 配置速查

`ExporterConfig` 常用项：

| 字段 | 默认 | 说明 |
|---|---|---|
| `endpoint` | — | 接入网关地址 |
| `protocol` | `http/protobuf` | AgentArts 用 `grpc` |
| `headers` | `{}` | 认证头，键须小写 |
| `projection` | None | 投影配置，见下 |
| `queue_size` / `queue_max_bytes` | `4096` / `128MB` | 缓冲队列双上限，满则丢弃计数并告警 |
| `batch_size` | `32` | 单批 span 数上限（小批次在慢网关下不易整批超时） |
| `batch_max_bytes` | `10000000` | 单批估算字节上限；放不下的 span 归下一批，单 span 超限则单独成批（不丢弃） |
| `flush_interval_ms` | `500` | 后台线程批量节奏 |
| `otlp_timeout_ms` | `30000` | 单次 RPC 超时（小值在慢网关下会整批 DEADLINE_EXCEEDED） |
| `export_attempts` | `5` | 失败批次重试次数，指数退避 1/2/4/8s，耗尽才丢弃 |

`Projection` 常用项：`preset="agentarts"`（平台语义派生）、`service_name`（平台服务命名）、`attributes`（公共属性注入）、`drop`（正则删属性）、`rename`、`truncate`（超长截断）。

## 6. 验证与排障

| 现象 | 排查 |
|---|---|
| `unknown projection preset 'agentarts'` | 忘了 `import agentarts_instrumentation.openjiuwen`（注册派生） |
| 平台无数据 | 先跑用法一隔离三要素问题；再查 span 是否产生（宿主日志） |
| 上报报错 404 | 用了 HTTP 协议——网关仅 gRPC |
| AgentSpan 缺 output | 宿主 openjiuwen 0.1.16 的盖章时序缺陷，升级 ≥0.1.20（见仓库 docs） |

## 7. 从 SDK 形态切到探针形态

开发期用 SDK 显式集成验证通了，生产部署不想改代码时：删掉显式集成代码，按 [guide-probe.md](guide-probe.md) 执行 `agentarts-instrumentation-inject` 即可——两种形态的 exporter 组装与投影配置完全一致。
