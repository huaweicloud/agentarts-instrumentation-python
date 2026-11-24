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

脚本核心是下面的 exporter 组装——SDK 标准组装方式，用法二、用法三复用同一 `exporter` 对象：

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

跑完到控制台该 Agent 的**追踪分析**视图确认 echo 出现。这一步验证了 endpoint/token/agent_id 三要素匹配，与后续宿主集成的问题域解耦；用法二、用法三在此基础上直接挂载同一 `exporter`。

## 4. 用法二：挂到 openjiuwen 宿主（核心场景）

承接用法一组装好的 `exporter`（组装代码见上节，不再重复）：openjiuwen 的 `init_observability` 提供 `span_exporter_override` 官方钩子，传入即可：

```python
from openjiuwen.extensions.observability import init_observability

init_observability(
    config,
    span_exporter_override=exporter,   # SDK exporter 成为 span 汇出口
)
```

> 前置：exporter 只负责导出，span 得由宿主自己产生——宿主观测开关没打开就没有数据可导。接 jiuwenswarm 时注意其 `config.yaml`：`team_observability` 默认已启用，但单 agent 路径的 `agent_observability.enabled` 默认 `false`，需显式改为 `true`。

从此 openjiuwen 产生的所有 span（agent / llm / tool / iteration）在导出时流经：**投影管线**（openjiuwen 数据契约 → `gen_ai.*` 平台语义）→ **缓冲队列**（计数+字节双限、后台线程批量、退避重试）→ OTLP gRPC → AgentArts。

## 5. 用法三：挂到任意 TracerProvider（通用 SDK）

`exporter` 仍取用法一组装的那份（含 `import agentarts_instrumentation.openjiuwen` 派生注册），只是不接 openjiuwen 宿主——它本身就是一个标准 `SpanExporter`，可挂到任意 `TracerProvider`：

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
tracer = provider.get_tracer("my-app")
```

适合把 SDK 用在其他自研框架的 PoC 上（注意：`agentarts` 派生规则按 openjiuwen 属性契约编写，跨框架时投影可能不全）。

## 6. 配置速查

`ExporterConfig` 常用项：

| 字段 | 默认 | 说明 |
|---|---|---|
| `endpoint` | — | 接入网关地址 |
| `protocol` | `http/protobuf` | AgentArts 用 `grpc` |
| `headers` | `{}` | 认证头，键须小写 |
| `projection` | None | 投影配置，见下 |
| `queue_size` / `queue_max_bytes` | `4096` / `128MB` | 缓冲队列双上限，满则丢弃计数并告警 |
| `batch_size` | `32` | 单批 span 数上限（小批次在慢网关下不易整批超时） |
| `flush_interval_ms` | `500` | 后台线程批量节奏 |
| `otlp_timeout_ms` | `30000` | 单次 RPC 超时（小值在慢网关下会整批 DEADLINE_EXCEEDED） |
| `export_attempts` | `10` | 失败批次重试次数，指数退避 1/2/4/8/16/30s 封顶，耗尽才丢弃 |

`Projection` 常用项：`preset="agentarts"`（平台语义派生）、`service_name`（平台服务命名）、`attributes`（公共属性注入）、`drop`（正则删属性）、`rename`、`truncate`（超长截断）。

## 7. 验证与排障

| 现象 | 排查 |
|---|---|
| `unknown projection preset 'agentarts'` | 忘了 `import agentarts_instrumentation.openjiuwen`（注册派生） |
| 平台无数据 | 先跑用法一隔离三要素问题；再查 span 是否产生（宿主日志） |
| 上报报错 404 | 用了 HTTP 协议——网关仅 gRPC |
| AgentSpan 缺 output | 宿主 openjiuwen 0.1.16 的盖章时序缺陷，升级 ≥0.1.20（见仓库 docs） |

## 8. 从 SDK 形态切到探针形态

开发期用 SDK 显式集成验证通了，生产部署不想改代码时：删掉显式集成代码，按 [guide-probe.md](guide-probe.md) 执行 `agentarts-instrumentation-inject` 即可——两种形态的 exporter 组装与投影配置完全一致。
