# agentarts-instrumentation-python

[![License](https://badgen.net/static/License/Apache-2.0/blue)](LICENSE)

华为云 agentarts-instrumentation-python 是一个专为华为云 AgentArts 平台设计的 Python 开发工具包。它帮助使用 LangChain/LangGraph、OpenJiuwen、Hermes 等 Agent 框架开发的智能体一键将 Trace 数据上报 AgentArts 可观测平台，实现智能体的观测分析、评估与优化。

## 仓库结构

本仓库为 uv workspace 多包布局：`agentarts-instrumentation-core` 提供框架无关的 OTLP 上报内核（批量缓冲、投影管线、注入工具），各框架适配独立成包、独立发版。

| 目录 | PyPI 包 | 说明 |
|---|---|---|
| [core/](core/README.md) | `agentarts-instrumentation-core` | 框架无关的 OTLP 上报内核：缓冲导出、投影管线与派生注册表、运行时注入工具 |
| [jiuwen/](jiuwen/README.md) | `agentarts-instrumentation-jiuwen` | OpenJiuwen 语义包：注册 agentarts 投影派生，向 openjiuwen 宿主一键注入 exporter |

> LangChain/LangGraph、Hermes 等框架适配包规划中，按 jiuwen 的同一模式接入。

安装与使用请查看对应框架包目录下的 README；SDK 整体设计见 [docs/design.md](docs/design.md)。

## 本地开发

```bash
uv sync --all-packages --all-extras   # 安装 workspace 全部包与依赖
uv run pytest                          # 运行测试（e2e/integration/live 组按环境自动跳过）
uvx ruff check core jiuwen examples    # lint
uv build core                          # 构建 core 发行包
uv build jiuwen                        # 构建 jiuwen 发行包
```

## API 参考

待补充

## 贡献

查看 [CONTRIBUTING.md](CONTRIBUTING.md)

## 许可证

本项目使用 Apache-2.0 许可证。
