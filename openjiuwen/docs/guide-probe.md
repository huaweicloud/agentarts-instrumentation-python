# 探针式安装器使用指南（.pth 注入）

探针形态面向**服务化宿主**：宿主以长驻服务/多进程树运行（如 jiuwenswarm 的 app / agentserver / gateway / workers），无法在代码里传 `span_exporter_override`，或者你**不想改宿主一行代码**。安装后宿主零改动、重启即生效。

SDK 显式集成形态见 [guide-sdk.md](guide-sdk.md)。

## 1. 何时选探针形态

| 条件 | 形态建议 |
|---|---|
| 宿主是多进程服务树，exporter 传不进去 | **探针** |
| 生产/托管环境，不能改宿主代码 | **探针** |
| 开发期调试、想控制每个参数 | SDK |
| 只想先验证平台连通性 | SDK（连通性脚本） |

两种形态不冲突：开发期 SDK 验证通过后，生产直接换探针。

## 2. 安装与注入（在宿主服务的 venv 内）

```bash
# 1. 安装 SDK 包（自包含 wheel，无需 core）
pip install \
  https://github.com/huaweicloud/agentarts-instrumentation-python/releases/download/v0.1.0a4/agentarts_instrumentation_openjiuwen-0.1.0a4-py3-none-any.whl \
  opentelemetry-exporter-otlp-proto-grpc

# 2. 写入 .pth（幂等，可重复执行）
agentarts-instrumentation-inject

# 3. 配置凭证（见下节）

# 4. 重启宿主服务 —— 注入对"新进程"生效
```

成功输出：

```
[agentarts-instrumentation-inject] installed: <site-packages>\agentarts_instrumentation_openjiuwen_inject.pth
[agentarts-instrumentation-inject] Set the official access variables (...) and restart the host service to activate.
```

## 3. 凭证配置

三种来源（同一键优先级：进程环境变量 > env 文件）：

**方式 A：进程环境变量**（容器/k8s 里最常见）

```bash
agentarts_agentops_apm_token=<token>
agentarts_agentops_apm_exporter_endpoint=https://apm-access.cn-southwest-2.myhuaweicloud.com
agent_resource_id=<agent_id>
# 可选：resource_type / agent_domain_id / agentarts_project_id / AGENTARTS_USER_ID
```

**方式 B（默认）：写进宿主自己的 env 文件**——探针默认读取 jiuwenswarm 工作区 `~/.jiuwenswarm/config/.env`，把官方接入变量追加进去即可，无需任何额外设置：

```ini
# 可与宿主自己的键（如 API_BASE）混写；带不带引号均可——解析与 python-dotenv 统一（成对引号自动剥除）
agentarts_agentops_apm_token=<token>
agentarts_agentops_apm_exporter_endpoint=https://apm-access.cn-southwest-2.myhuaweicloud.com
agent_resource_id=<agent_id>
```

**方式 C：`AGENTARTS_ENV_FILE` 指向独立 env 文件**（不想写进宿主 env；设置后方式 B 的默认文件不再读取，解析方式相同）

```bash
AGENTARTS_ENV_FILE=/path/to/agentarts.env   # 文件里写上面这些 KEY=VALUE
```

注意：注入模块在宿主自己的 `load_dotenv` **之前**运行，凭证必须在环境或上述文件里，不能指望宿主配置加载后再注入。

显式关闭开关：`agentarts_agentops_otel_agent_apm_enable=false` → 探针静默停用（凭证齐全也不注入）。

## 4. 验证生效

**看宿主日志（stderr）**，重启后应出现三组行：

```
[agentarts-instrumentation-inject] AgentArts span exporter injection armed (deferred until openjiuwen import)
[agentarts-instrumentation-inject] AgentArts span exporter injection applied to openjiuwen.extensions.observability.setup (swept 0 stale bindings)
[agentarts-instrumentation-inject] AgentArts span exporter injection applied to openjiuwen.agent_teams.observability.setup (swept 0 stale bindings)
```

含义：armed = 凭证齐、已布防（此进程无 openjiuwen 也无害）；applied = 宿主 import 观测模块时补丁生效（双入口都打到）。多进程树中**每个**进程都会各自走一遍。

然后在 AgentArts 控制台对应 Agent 的追踪分析视图看数据。

## 5. 运行行为须知

- **冲突让位**：若宿主自己的原生 OTLP 配置已直接指向 AgentArts 网关，探针会打印 standdown 警告并让位，避免同一条 span 上报两份。localhost/langfuse/其他 collector 不算冲突。
- **幂等**：宿主自建 TracerProvider 时，探针按 provider id 去重追加 processor；`span_exporter_override` 已有值时不覆盖。
- **无害性**：无凭证进程静默跳过；包被卸载后残留 .pth 静默；注入任何失败只打一行 stderr，绝不阻塞宿主启动。
- **延迟补丁**：探针在宿主 import 观测模块的瞬间打补丁，宿主 import 顺序与未装探针时完全一致（不会引发 fastmcp 半初始化类问题）。

## 6. 卸载与升级

```bash
# 卸载（注意顺序：先删 .pth 再卸包）
agentarts-instrumentation-uninject
pip uninstall agentarts-instrumentation-openjiuwen
```

- 卸载后**存量进程**内存里的 exporter 继续工作到重启为止；新进程彻底干净。
- **升级**：`pip install -U <新 wheel>` 后无需重新 inject——`.pth` 引用的是包内模块路径，重装自动指向新版本；重启宿主生效。

## 7. 无数据排查路径

按序检查：

1. 凭证三要素对宿主进程是否可见（进程环境变量，或默认/指定的 env 文件——容器里最常见：只配在了 shell 而没进服务环境）
2. 重启后日志里有没有 `armed` / `applied` 行（没有 armed → 凭证问题；有 armed 无 applied → 宿主没 import 观测模块/没调 `init_observability`）
3. 宿主是否真的产生了 span（看宿主自己的观测日志；空跑不发数据是正常的）
4. 平台 Agent 资源 ID 是否与 `agent_resource_id` 一致（配错 Agent 看不到数据）
5. span 有但字段缺（如 AgentSpan 无 output）：确认 openjiuwen 版本，0.1.16 有已知盖章时序缺陷，建议升级 ≥0.1.20

## 8. 想深入了解机制

四阶段生命周期（安装 → 每进程布防 → 延迟补丁 → 运行时注入）的完整机制说明见 [design.md](design.md)（本目录）与源码注释：[openjiuwen/inject.py](../src/agentarts_instrumentation/openjiuwen/inject.py)。
