# agentarts-instrumentation-openjiuwen

OpenJiuwen 语义包：向华为云 AgentArts（agent-ops）上报 openjiuwen / jiuwenswarm 智能体的 Trace 数据。

- 注册 `agentarts` 投影派生：openjiuwen 数据契约（`openjiuwen.*` 属性、span 命名形态）→ AgentArts `gen_ai.*` 平台语义（span.kind 派生、token/输入跨 span 聚合、AGENT 根状态升级、技能识别、显示名压缩等，详见 [docs/agentarts-semantics.md](docs/agentarts-semantics.md)）
- 提供 openjiuwen 宿主注入：包装 `init_observability(..., span_exporter_override=...)`，宿主原生导出已指向 AgentArts 网关时自动让位（防重复上报）
- 对 openjiuwen **零 import 依赖**：只读取其属性数据契约，可与任意宿主版本共存

## 安装

`agentarts-instrumentation-openjiuwen` 是**自包含发行包**：wheel 内已内嵌 `agentarts-instrumentation-core` 的全部模块（对其无 pip 依赖），安装这一个包即可，无需再装其他 SDK 包。

```bash
pip install "agentarts-instrumentation-openjiuwen[grpc]"   # gRPC 为 AgentArts 接入网关必需
```

> 预览 wheel 直装：见 [Releases](https://github.com/huaweicloud/agentarts-instrumentation-python/releases)（v0.1.0a2 起为单个自包含 wheel）。

## 快速开始

方式一（standalone）：把 exporter 交给宿主的初始化钩子。

```python
import agentarts_instrumentation.openjiuwen  # 注册 agentarts 语义派生
from agentarts_instrumentation import ExporterConfig, Projection, build_exporter

exporter = build_exporter(
    ExporterConfig(
        endpoint="https://apm-access.cn-southwest-2.myhuaweicloud.com",
        protocol="grpc",
        headers={"authentication": "<access_token>"},
        projection=Projection(
            preset="agentarts",
            service_name="AgentArts.<agent_id>.default",   # 平台要求的服务名
        ),
    )
)

# openjiuwen 宿主初始化时注入
init_observability(config, span_exporter_override=exporter)
```

方式二（一键注入）：安装后执行 `agentarts-instrumentation-inject`，向当前 venv 的 site-packages 写入 `.pth`，此后每个解释器进程自动完成注入；执行 `agentarts-instrumentation-uninject` 撤销。

```bash
agentarts-instrumentation-inject
```

> 两种形态的完整步骤、凭证配置与无数据排障，见 [SDK 形态使用指南](docs/guide-sdk.md) 与 [探针式安装器使用指南](docs/guide-probe.md)。

## 访问凭证

通过环境变量提供（探针形态还支持 env 文件：默认读宿主自己的 `~/.jiuwenswarm/config/.env`，或用 `AGENTARTS_ENV_FILE` 指向独立文件；引号写法与 python-dotenv 统一；进程环境优先）：

| 变量 | 说明 |
|---|---|
| `agentarts_agentops_apm_token` | 接入 token（必需） |
| `agent_resource_id` | 智能体资源 ID（必需） |
| `agentarts_agentops_apm_exporter_endpoint` | 接入网关（可选，默认 cn-southwest-2） |
| `agentarts_agentops_otel_agent_apm_enable` | 显式 `false/0/off` 关闭上报 |
| `AGENTARTS_TRACE_TOKEN` / `AGENTARTS_AGENT_ID` / `AGENTARTS_TRACE_ENDPOINT` | 0.1.x 旧名（兼容回退，已废弃） |

## 测试

```bash
uv run pytest openjiuwen/tests                     # 单元组（默认全跑）
uv run pytest openjiuwen/tests/integration         # 需本地 agent-core checkout（AGENT_CORE_DIR）
uv run pytest openjiuwen/tests/live                # 需真实平台凭证
```

## 文档

- [SDK 形态使用指南](docs/guide-sdk.md)（显式集成）
- [探针式安装器使用指南](docs/guide-probe.md)（.pth 注入；含机制与宿主兼容性实测）
- [设计文档](docs/design.md)（架构与组件；§4.5 缓冲）
- [AgentArts 语义映射规范](docs/agentarts-semantics.md)（§4.3 agentarts 派生与裁剪规则）
- [接入指南](docs/接入指南.md)（逐步操作手册）
- [Tutorial](docs/tutorial.md)
