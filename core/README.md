# agentarts-instrumentation-core

框架无关的 OTLP 上报内核，供各框架适配包（如 [agentarts-instrumentation-openjiuwen](../openjiuwen/README.md)）组装使用，也可 standalone 作为普通 `SpanExporter` 挂载到任意 `TracerProvider`。

> **发行状态**：core 目前是 workspace 内部包，**不单独发布**——fat 模式下其模块直接内嵌在 openjiuwen 的自包含 wheel 里一起分发。将来其他框架包（langchain/hermes）需要时，再将 core 发布为独立依赖包。

## 组成

| 模块 | 说明 |
|---|---|
| `exporter.buffer` | `BufferedOtlpExporter`：双限（条数/字节）有界队列 + 后台批量发送 + 指数退避重试，业务线程永不阻塞 |
| `exporter.otlp` | 官方 OTLP gRPC/HTTP exporter 构建（RPC 超时、TLS/plaintext 推断） |
| `exporter.transform` | `TransformingSpanExporter`：copy-on-export 投影包装（克隆上做投影，原 span 不受影响，投影异常回退导出原 span） |
| `projection` | 通用投影管线（属性注入 → 派生 → drop → rename → truncate）+ 派生注册表 |
| `semconv` | AgentArts 平台 `gen_ai.*` 语义键 |
| `internal.credentials` | AgentArts 官方接入凭证解析（环境变量 / .env 文件） |
| `internal.inject` | 通用 .pth 注入工具 + 延迟 import 补丁机制 |

## 安装

```bash
pip install agentarts-instrumentation-core
```

## 使用（standalone）

```python
from agentarts_instrumentation import ExporterConfig, Projection, build_exporter

exporter = build_exporter(
    ExporterConfig(
        endpoint="http://localhost:4318/v1/traces",
        projection=Projection(drop=["secret*"]),  # 通用投影规则；框架语义派生由框架包注册
    )
)

# 作为普通 SpanExporter 挂载
provider.add_span_processor(SimpleSpanProcessor(exporter))
```

## 框架包接入约定

- 导入名共享顶层命名空间 `agentarts_instrumentation`（pkgutil extend_path），框架包只贡献自己的子包；
- 框架语义通过 `register_derivation(name, fn)` 注册，用户在 `Projection(preset=name)` 中启用；
- 框架包 wheel 只携带 `agentarts_instrumentation/<framework>/**`，不覆盖顶层 `__init__.py`（由 core 独家持有）。

## 文档

- [设计文档](../docs/design.md)
