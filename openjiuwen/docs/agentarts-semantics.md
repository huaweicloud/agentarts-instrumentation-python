# 华为 AgentArts 语义映射规范（agentarts preset）

> 本文件由 [design.md](design.md) §4.3 与附录 B 拆分而来（2026-11-27）。**小节编号保持原样**——代码注释中的 §4.3.x 引用在本文件仍然有效。
>
> 投影接口（`Projection` 模型、`project_span` 语义）见 design.md §4.2；本文件只讲 `preset="agentarts"` 的规则内容。

## 4.3 华为 AgentArts 语义映射 preset（preset: agentarts）

基于《OpenTelemetry 协议字段映射》（华为云 AgentArts 智能体观测文档，2026-11-02 版，见文末附录 B）。
该 preset 通过下面三类规则，把 openjiuwen 产出的规范 span 投影成平台要求的形态；
规则建立在**属性键数据契约**（`openjiuwen.*` 属性值）之上，对 openjiuwen 无任何代码依赖。

### 4.3.1 派生（derive；span 未携带的平台必填字段）

| 平台字段 | 必填 | 派生规则（按优先级） |
|---|---|---|
| `gen_ai.span.kind` | 是 | `openjiuwen.trace.root=true` 或 `openjiuwen.trajectory.record.kind=turn` → `AGENT`；`gen_ai.tool.name` 存在或 `gen_ai.operation.name=execute_tool` → `TOOL`；`openjiuwen.trajectory.record.kind=step` 或 span 名匹配 `*.react_iteration.*` → `STEP`；`gen_ai.operation.name ∈ {chat, text_completion, generate_content}` → `LLM`；span 名匹配 `team.*` / `task.*` → `WORKFLOW` |
| `gen_ai.usage.total_tokens` | AGENT/LLM 必填 | 按 `input_tokens + output_tokens` 回填；AGENT 根没有 usage 时由 `_TraceTokenAggregator` 跨 span 聚合：先结束的 LLM span 累加 input/output，后结束的 AGENT 根消费总和并回写三个键（宿主已写的值绝不覆盖；实测验证：AGENT tokens = Σ LLM tokens）。两个 cache 键同样聚合（用户需求 2026-11-21）：`gen_ai.usage.cache_read.input_tokens` / `gen_ai.usage.cache_creation.input_tokens`（宿主 semconv 键，只在 LLM span 上写；探针把它们累加到 AGENT = Σ LLM） |
| `gen_ai.agent.name` | AGENT 必填 | 取 `openjiuwen.agent.mode`；缺失时不写、不猜（可直接经 `Projection.attributes` 供给） |
| `gen_ai.react.round` | 否 | `openjiuwen.turn.number` / span 名中 `react_iteration.N` 里的 N |
| `gen_ai.skill.name` / `gen_ai.skill.id` / `gen_ai.skill.version` | 推荐（skill 被调用时 name 必须上报） | 从 TOOL span 的 `gen_ai.tool.call.arguments` 中的路径特征识别为 Skill 文档读取并派生（§4.3.5）；**不建独立 span**，与平台规范一致 |

### 4.3.2 键名修正（rename；平台命名与 OTel GenAI semconv / 项目键不同；已实现并验证）

| 平台字段 | openjiuwen 现有键 | 处理 |
|---|---|---|
| `gen_ai.usage.cache_creation.input_tokens` | `gen_ai.usage.cache_write.input_tokens` | rename（已实现） |
| `gen_ai.response.time_to_first_token`（int, ms） | `openjiuwen.gen_ai.response.time_to_first_token_ms`（int, ms, semconv.py:145） | rename（已实现） |
| `gen_ai.request.is_stream` | `gen_ai.request.stream`（gen_ai_semconv.py:59；写入于 callback_handler.py:1096） | rename（已实现） |
| `gen_ai.tool.call.id` | agent-core 已使用平台同名键（callback_handler.py:1593） | 直通，无需处理 |
| `gen_ai.system_instructions`（纯文本） | agent-core 写 parts 数组 `[{"type","content"}]`（callback_handler.py:1760）；官方探针实测上报**纯字符串**（openclaw trace 契约），控制台按纯文本渲染 | 格式扁平化（已实现）：数组 → content 值拼接的纯文本；已是纯文本 / 解析失败则原样不动 |

### 4.3.3 配置供给属性（公共属性，表 2，注入每个 span）

| 平台字段 | 必填 | 来源 |
|---|---|---|
| `gen_ai.resource.id` | 是 | 用户配置（AgentArts 控制台分配的 agent ID） |
| `gen_ai.resource.type` | 是 | 用户配置（agent / multiagents / workflow） |
| `domain.id` | 否 | 用户配置（租户隔离） |
| `gen_ai.user.id` | 推荐 | 用户配置（终端用户身份；或从 `openjiuwen.execution.subject.*` 派生，实现期定） |
| `gen_ai.conversation.id` | 推荐 | span 已携带（run 根 span）；直通 |
| `resource_version` | 否 | 用户配置 |

### 4.3.4 直通项（openjiuwen 已产出平台要求的结构）

`gen_ai.request.model`（LLM 必填）、`gen_ai.input.messages` /
`gen_ai.output.messages`（推荐；`{"role","parts"}` 结构与平台示例一致）、
`gen_ai.tool.name` / `gen_ai.tool.call.arguments` / `gen_ai.tool.call.result`
（TOOL 必填）、`gen_ai.usage.input_tokens` / `output_tokens`（必填）、
`gen_ai.response.finish_reasons`（推荐）。

### 4.3.5 Skill 调用识别（挂工具 span；不建独立 span）

平台规范明说：skill 调用**不建独立 span**；信息通过在工具执行 span 上设置
`gen_ai.skill.*` 属性（配合 `skill.loaded` / `skill.released` 事件）来承载。在
openjiuwen 场景中，skill 加载/使用表现为**读取 `SKILL.md` 或 skill 目录下文档的工具
调用**（read/grep/glob 等），而路径已携带在 `gen_ai.tool.call.arguments` 里——SDK 在
投影时按路径特征识别并派生，不需要 agent-core 任何改动。

**识别规则（仅作用于 TOOL span；已有信息优先，路径推断只是兜底）**：

0. **让位检查**：若 span 已带 `gen_ai.skill.name` 属性或 `skill.loaded` 事件
   （如未来 agent-core 按平台规范原生产出），**信任已有信息并跳过路径匹配**，
   只回填缺失的 `gen_ai.skill.id` / `version`，绝不覆盖任何已有键（与 design.md §4.2
   `attributes` 注入"绝不覆盖 span 已有键"的原则一致）——一旦 agent-core 原生支持，
   SDK 对 skill 自动退化为直通，无需改代码；经核实（2026-11），当前两个 openjiuwen
   仓库的 span 均不携带 skill 属性/事件，路径匹配是唯一信息来源；
1. 解析 `gen_ai.tool.call.arguments`（JSON 字符串），取路径类参数
   （`path` / `file_path` / `file` / `dir` / `pattern` 等；键集合在实现期对照
   jiuwenswarm / agent-core 实际工具参数形态确定）；
2. 命中任一默认特征（可经 `skill_path_patterns` 扩展）：
   - 文件名为 `SKILL.md`（大小写不敏感）；
   - 路径含名为 `skills` 的目录段（如 `.agents/skills/<name>/...`）；
3. 派生：
   - `gen_ai.skill.name`：`SKILL.md` 的上级目录名，或 `skills` 目录段的下一段；
   - `gen_ai.skill.id`：填 skill 名（平台侧无 id 来源；字段可选）；
   - `gen_ai.skill.version`：路径含版本段则提取，否则不上报（可选）；
4. 事件：命中 `SKILL.md` 读取时，为 span 追加事件 `skill.loaded`，属性
   `{skill.name, skill.path}`（平台示例形态）；追加事件必须构造**新的 events
   列表**（copy + append）——原始 span 绝不能被改动；
5. `gen_ai.operation.name` 保持 `execute_tool` 不变（`load_skill` 是 AgentArts
   自有运行时工具命名；平台表 5 对 TOOL span 的 operation.name 无此要求）；
6. 保守策略：一次工具调用经多个路径参数命中不同 skill 时，只取第一个命中并告警；
   `gen_ai.tool.call.result` 不参与识别（v1）。

### 4.3.6 暂不覆盖的平台规范项

| 平台要求 | 状态 |
|---|---|
| `skill.released` 事件 / `skill_complete` 形态 | openjiuwen 无显式"释放 skill"动作可识别；v1 不派生；待 agent-core 产出 skill 生命周期事件后再补 |
| `MEMORY` span 类型 | openjiuwen 无 memory 服务 span 来源；暂不派生 |
| 指标项（平台表 9/10：`gen_ai.skill.call.count` / `gen_ai.skill.duration` / `gen_ai.usage.status`、`total.requests`、`client.operation.duration`、TTFT Histogram 等——13 项加必填维度） | 超出 SDK v0.1 范围（仅 trace）；平台"用户趋势 / QPS / TTFT 分布 / skill 成功率"等指标看板需后续迭代补指标上报。仅 trace 上报即可用的有：调用链、会话分析、token 用量与工具分析（从 trace 属性计算的部分），以及调用链层面的 skill 展示 |

### 4.3.7 preset 裁剪（框架命名空间整体裁剪 + 轨迹协议专属 span；2026-11-17 实测修订）

导出克隆的属性面**只保留平台语义**（`gen_ai.*` / OTel 标准键）加用户注入属性；宿主
框架命名空间整体裁掉（`AGENTARTS_DROP_ATTRS` 前缀通配）。裁剪发生在**派生之后**——
下表源键在派生中已被完全消费，裁掉它们不影响任何投影行为：

| 已消费的源键 | 消费点 |
|---|---|
| `openjiuwen.trace.root` / `trajectory.record.kind` / `agent.mode` / `turn.number` | kind 派生（§4.3.1）、显示名改写（§4.3.8，**先于裁剪执行**）、`gen_ai.agent.name` 派生、react round 派生 |
| `openjiuwen.span.input` | 无消费者：turn 输入直接取 LLM span 自身的 `gen_ai.input.messages`（`_TurnInputAggregator`，§4.3.2）；该键仅见于历史文档 |
| `openjiuwen.span.output`（0.1.20 契约） | 输出包装源 → `gen_ai.output.messages`（包装后弹出） |
| `langfuse.observation.output`（0.1.16 契约） | run span 无 span.output 时，从其 dict 包装提取 AGENT 最终答案（`_extract_langfuse_output`）。**实测到两种宿主写入形态**（均非严格 JSON；经捕获的调试输出确认）：形态 A，双引号 + 裸换行控制字符（f-string 拼接产物）→ `json.loads(strict=False)`；形态 B，**单引号 Python dict repr**（`str(dict)` 产物，换行是字面 `\n` 转义序列）→ JSON 必然失败；用 `ast.literal_eval` 兜底（无代码执行风险）。两级解析都失败则按缺失处理——绝不写脏值。另实测：宿主偶尔直接写 `gen_ai.output.messages`（带自己的 `finish_reason`，含裸换行）——已有值不覆盖，直通 |
| `openjiuwen.gen_ai.response.time_to_first_token_ms` | rename 源 → `gen_ai.response.time_to_first_token` |

**整命名空间裁剪清单**（`AGENTARTS_DROP_ATTRS`，`*` 前缀通配）：

| 命名空间 | 理由 |
|---|---|
| `openjiuwen.*` | 框架数据契约键：消费后（见上）不承载平台语义；session.id 与 `gen_ai.conversation.id` / `session.id` 同值重复；step.id / request.id 等定位键可由平台 span_id 替代；total_latency_ms / tpot_ms / provider_metadata 无平台展示位（本地轨迹库中仍可查） |
| `langfuse.*` | Langfuse 镜像：observation.input/output/type 与 `gen_ai.*` 值重复（载荷翻倍）；0.1.16 的 AGENT 最终答案已在派生中提取 |
| `agentteam.*` | team 会话 id 等：单 agent 场景下与 session 键重复 |
| `deepagent.*` | jiuwenswarm 执行器内部埋点：iteration 与显示名重复；loop_event 存放未序列化 Python 对象的脏 repr 值 |

**精确键裁剪**（同表，非前缀）：`gen_ai.tool.input` / `gen_ai.tool.output`
（2026-11-17 实测重复值：tool.input 是 `[args 列表 + session 元数据]` 包装（含
session id），tool.output 与 `gen_ai.tool.call.result` 完全相同——平台语义键已由
call.arguments / call.result 携带；保留后者）。

**events / links**（同日实测修订）：流式 LLM span 单次会话可携带上百个
`openjiuwen.stream.chunk` / `llm.chunk` 事件——是 trace 详情载荷的大头（实测：单条
trace 详情 393KB）。preset 对 events 应用**白名单**：只保留宿主产出的 `skill.loaded`
检测事件（平台的 skill 检测事件通道，§4.3.5）+ 新合成的 skill 检测事件；其余宿主
事件一律不上报；links 始终清空（平台按 parent_span_id 建树，不消费 links）。
`otel.scope.name` 是 OTel 标准 instrumentation-scope 键（exporter 追加），不在裁剪
范围。本地轨迹数据面（SQLite 轨迹库 / jsonl）不受影响——投影只作用于导出克隆；
排障仍走全量本地数据。

**整 span 裁剪**（`AGENTARTS_DROP_SPANS`）：`context.window.commit`——上下文窗口
快照链 span；AgentArts 无对应 span 类型，且长会话下窗口载荷可达数十 KB。本地轨迹
数据面（SQLite 轨迹库）不受影响、保留全量数据。实现语义：`project_span` 返回
`None` = 有意过滤；`TransformingSpanExporter` 跳过该 span（区别于 transform 异常时
回退原始 span，design.md §4.4）。

**悬空 task_iteration 标记的结构感知裁剪**（2026-11-17 实测修订，`_ParentRefRegistry`）：
task_iteration span 在两代宿主中扮演结构上不同的角色——容器（0.1.20/develop）：react
轮挂在其下（子 span 的 parent 指向它）；丢弃它会让 react 轮变孤儿、破坏平台树——
必须保留；悬空标记（0.1.16/dev-stable）：react 轮直接挂在 AGENT 根上；该 span 无子
引用、无语义属性（平台 span 类型空白）——纯噪声，不上报。识别利用 OTel"子 span 先于
父结束；投影按结束顺序处理"：每个 span 在投影时记录其父 span id（容量 4096 的有界
FIFO）；task_iteration span 被投影时检查自身 id 是否被引用——被引用 → 保留（显示名
`task N`）；未被引用 → 整 span 过滤。对两种形态都安全，无需版本探测（实测：dev-stable
平台树收敛为 AGENT→react step N→LLM/TOOL）。

### 4.3.7a 显示名压缩（只改写投影克隆的 name）

宿主命名模式很长（jiuwenswarm AGENT 根：`agent.{mode}.{长 session_id}`）。preset
按规则压缩显示名（`_rewrite_agent_span_name`，已实测验证）：

- AGENT 根 → `invoke_agent openjiuwen`（session 后缀与 mode 无展示价值）；
- `agent.{type}.invoke` → `{operation.name} {type}`，如 `invoke_agent main_agent`；
- `agent.{type}.task_iteration.N` → `task N`；
- `agent.{type}.react_iteration.N` → `react step N`（轮号同时经
  `gen_ai.react.round` 属性传递，§4.3.1）；
- TOOL / LLM 及非 `agent.*` 名称原样保留。

`clone_readable_span` 增加了可选 `name` 参数承载改写；trace/span id、父链等身份字段
不受影响，本地轨迹库仍收到原始名称。kind / react.round 派生先于改写运行（使用原始
名称与属性）；显示名压缩不参与任何语义逻辑。

### 4.3.9 子 span 错误传播（AGENT 根状态升级；B 方案修订 2026-11-21）

宿主只对失败的业务子 span 调 `set_status(ERROR)`（LLM 调用错误、工具上报失败
`TOOL_REPORTED_FAILURE`）；AGENT 根不继承。0.1.2 首版策略是"任一子 span 失败 → 根
ERROR"；实测（trace c2acd7**[已脱敏]**：web_search 失败 → fetch_webpage 自愈 → 任务
完成，根却被标 ERROR）表明该策略把自愈运行计为失败，扭曲成功率指标。**B 方案
（0.1.4）收紧为终结性失败**：

- **登记**：业务子 span 被投影时记录其状态——ERROR 登记失败（同时复位恢复标志；
  终结性定义为"最后一次失败之后没有成功的业务 span"）；OK 登记恢复信号（宿主成功
  路径显式调 `Status(OK)`：工具回调 L864、LLM L1199）；None/UNSET（STEP 等）都不
  登记——工具失败后的同轮收尾不构成自愈证据。根本身从不登记；登记发生在
  DROP_SPANS 过滤之后（内部记账 span 不参与）；
- **消费**：在 kind=AGENT 投影时消费。终结性失败（未恢复）→ 投影克隆升级为
  `Status(ERROR, "child span failed: {last failed name}")`；已恢复 → 根状态不动
  （反映运行完成），投影克隆的 `gen_ai.error.recovered` 属性留痕（平台可见；运维
  仍能发现子 span 退化）；已为 ERROR 的根绝不覆盖。

多根 trace 上由先到达的第一个 AGENT span 消费（与 token/input 聚合器同一限制）。

## 附录 B：外部规范引用

| 引用 | 说明 |
|---|---|
| <https://support.huaweicloud.com/ops-agentarts/agentarts_14_0127.html> | 华为云 AgentArts《OpenTelemetry 协议字段映射》（本文 §4.3 preset 的规范来源，版本 2026-11-02）：span 类型（表 1）、公共属性（表 2）、AGENT/LLM/TOOL/STEP/WORKFLOW/MEMORY 各类型字段（表 3-8）、指标项与维度（表 9-11） |
| <https://support.huaweicloud.com/ops-agentarts/agentarts_14_0125.html> | 《上报 Trace 数据》实操（endpoint / 鉴权接入细节；实现期参考） |
| <https://support.huaweicloud.com/ops-agentarts/agentarts_14_0126.html> | 《上报指标数据》实操（后续指标迭代参考） |
| <https://opentelemetry.io/docs/specs/semconv/gen-ai/> | OTel GenAI 语义约定（华为规范的上游参照） |
