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

## 8. 注入机制（.pth；原 design.md §6.5）

宿主以常驻服务 / 多进程形态运行时（jiuwenswarm：app / agentserver / gateway /
worker），`span_exporter_override` 无法作为参数直接传入（SDK 指南 §4.2 是库侧 /
测试侧路径），而宿主 config.yaml 的 `otlp_grpc` 又不满足 AgentArts 契约（agent-core
`build_span_exporter`：`insecure=True` 硬编码 + Langfuse Basic 鉴权）。探针的做法：
`agentarts-instrumentation-inject` 向 venv 的 site-packages 写入带守卫的
`agentarts_instrumentation_openjiuwen_inject.pth`，让**每个**解释器进程都导入包内
inject 模块；模块的 `main()` 加载凭证（三来源见 §3），随后注册 `sys.meta_path`
finder，待宿主自身导完观测模块后，在**两个入口**
（`extensions.observability.setup` + `agent_teams.observability.setup`，沿导入链
清扫原函数的早期绑定引用——详见 §9 差异 1/2）patch `init_observability`，自动注入
exporter。四阶段生命周期：安装 → 每进程布防 → 延迟补丁 → 运行时注入。源码：
[openjiuwen/inject.py](../src/agentarts_instrumentation/openjiuwen/inject.py)。

实测要点（2026-11，jiuwenswarm 0.2.8（develop）+ openjiuwen 0.1.20 + OTel 1.41.x；
真实运行 + AgentArts 回显验证）：

1. **sitecustomize.py 不可用**：Debian/Ubuntu 上系统级
   `/usr/lib/<py>/sitecustomize.py`（apport）在 sys.path 上先于 venv 的
   site-packages，同名文件永远不加载；而 `.pth` 在 site 处理期执行，早于业务
   导入，覆盖所有进程——这是选 .pth 而非 sitecustomize 的原因；
2. **宿主配置**：jiuwenswarm 还需 `observability.agent.enabled: true`（默认
   false——不产 span）与 `attribute_value_max_length: 65536`（实测：生产系统提示词
   16K 字符；默认 10240 会截断）；
3. **结果**：真实会话（WS `chat.send`）产出完整生产 trace（AGENT→invoke→
   task_iteration→react_iteration→LLM 五级树，AGENT tokens = Σ LLM，16420 字符的
   `system_instructions` 纯文本全量回显）。

## 9. 宿主版本兼容性实测（原 design.md §6.5.1 / §6.5.2）

### 9.1 dev-stable 分支验证（2026-11-17；jiuwenswarm 0.2.3 dev-stable + openjiuwen 0.1.16 pin @f3f8f5）

与 develop（jiuwenswarm 0.2.8 + openjiuwen 0.1.20）的五处差异与对策：

1. **遥测消费入口变了**：dev-stable 的 `jiuwenswarm/telemetry/runtime.py` 从
   `openjiuwen.agent_teams.observability` 的**顶层**导入 `init_observability`
   （0.1.16 里该包是转发到共享 extensions runtime 的独立定义），且主路径传的是
   `tracer_provider_override`（与 `span_exporter_override` 互斥；传了会抛
   RuntimeError）→ 模板升级为**双入口 patch**（extensions + agent_teams 两个
   setup 子模块）+ **双注入模式**（provider 形态：orig 成功后向 provider 追加
   `SimpleSpanProcessor(SDK)`，按 provider id 幂等 + provider 主 exporter 已是本
   SDK 时跳过，防双份导出；无 provider 形态：把 override 传入）。
2. **绑定清扫（关键陷阱）**：在注入模块里先导入 `agent_teams.observability.setup`
   会先执行整个 teams 包 `__init__` → `rail.py` → `harness.observability` 导入链，
   链上 `init_observability` / `init_shared_observability` 符号在 patch 生效前就
   绑定到**原函数**——之后再 patch 两个 setup 子模块也救不回那些引用（harness/
   chat 路径与 teams 包顶层消费的是原函数 → provider 加载宿主自配的 file
   exporter，SDK 被整体绕开；实测：chat span 只落到本地 `.trace/*.jsonl`）。
   对策：patch 后按原函数的**对象同一性**扫描 `sys.modules` 中的 openjiuwen 模块
   属性，重绑过期引用（实测命中 3 处——teams 包 `__init__`、teams setup 与
   harness setup 中的 `init_shared_observability`）。
3. **fastmcp 命名空间冲突**：openjiuwen 0.1.16 声明
   `Requires-Dist: fastmcp<3.0,>=2.14.2` → pip 自动装官方 fastmcp 2.14.7，与 jiwen
   分叉 `fastmcp-slim==3.4.0` 共享 `fastmcp/` 路径、相互覆盖（slim 补丁过的
   `auth.py` 有独有的 `PrivateKeyJWTClientAuthenticator`；官方版没有）→ chat 处理中
   ImportError。修复：从官方 PyPI `pip install --force-reinstall --no-deps
   fastmcp-slim==3.4.0`；slim 独占 `fastmcp/` 后服务正常。**任何依赖重装都可能
   复发**——用 `python -c "from fastmcp.server.auth.auth import
   PrivateKeyJWTClientAuthenticator"` 复查。
4. **cron 存储/调度不兼容**：develop 写 `domain/agent/home/cron_jobs.json`；
   dev-stable 的 gateway cron 用独立存储（KV 后端 + 懒迁移），不读旧任务文件；
   CLI 也缺 `--keep-worker`（重启脚本须去掉该参数）。任务经 WS
   `cron.job.create` 重建（帧 `{"type":"req","method":"cron.job.create",
   "params":{"name":...,"cron_expr":"0 */10 * * * *","description":...}}`）；
   调度/触发/回填全部正常（`[Cron][run-trace]` 全链路日志）。
5. **config schema 改名**：`observability` 段拆为 `team_observability` /
   `agent_observability`（后者默认 enabled=false，但被轨迹数据面的按需机制自动拉起
   ——prewarm 打印 `[AgentObservability] enabled: exporter=file`）；无新段的旧
   配置 → 按默认值。注入生效后 override 优先于该段的 exporter 值，但
   `attribute_value_max_length` 等截断设置仍归宿主配置管，需注意。

另两项 dev-stable 实测（宿主缺陷，与探针无关但污染巡检结果）：

6. **split 布局双调度器双跑**：`python -m jiuwenswarm.app` 总是拉起 agentserver +
   gateway（无独立单进程分支）。agentserver 侧的 agent 工具构建链起一个
   `CronSchedulerService`；gateway 启动时也起一个——两者**共享同一 cron 存储、每
   tick 同跑同一任务**：gateway 链产出真实巡检运行，agent 侧链产出内容为空的怪异
   默认会话运行（每轮白烧约 93k input tokens）。幂等闸门是进程内的，跨进程无用。
   **修复（最小宿主补丁）**：在 `cron_tools.py ensure_scheduler()` 顶部加 env 守卫
   ——`JIUWENSWARM_AGENT_CRON_DISABLE=1` 时返回 None（cron 工具读写 / WS `run_now`
   不受影响）；宿主重启脚本导出该变量。验证：重启后日志只剩 gateway 的单条
   `[Cron] scheduler started`，单条平台 trace。**这是 dev-stable 宿主缺陷，应报给
   jiuwenswarm 维护者**——develop 分支的 `ensure_scheduler` 已做"Phase 4 单源收敛"
   （恒返 None）——缺陷是"上游已修但未回流"；env 补丁与上游修复同构且前向兼容
   （develop 版不读 env，升级后自动退化为无害 no-op）。
7. **jiuwenbox 沙箱三层依赖断裂**（所有 TOOL span 409
   `Cannot exec in sandbox: state is error`）。逐层根因与修复：
   ① 缺系统级依赖 **bwrap**（bubblewrap）——`exec of "bwrap" failed: No such
   file or directory` → `apt-get install -y bubblewrap`（apt 包，不在 pip 依赖树
   里；环境重装会复发）；② config.yaml
   `sandbox.jiuwenbox.policy_file: windows-policy.yaml`——**Windows 策略跑在
   Linux 上**：该策略完全没有文件系统 bind，bwrap 沙箱里没有 /usr，守护进程
   `execvp python3: No such file or directory` → 换 `default-policy.yaml`（bind
   /bin /usr /lib /etc 等）；伴生陷阱：`_is_std_cpython` 探测
   `JIUWENBOX_RUNNER_PYTHON` 是 **Windows 专属检查**（找 `python3*.dll`），Linux
   上恒为空 → 守护进程命令回落到裸 `python3` 名，完全依赖策略里 /usr bind 的可见
   性；③ default-policy bind 了 `/opt`，而最小镜像可能没有该目录——`bwrap: Can't
   find source path /opt` → `mkdir -p /opt`。验证：`exited during startup` 日志
   归零，TOOL span `is_error=False`，bash 返回真实 uptime。**排障路径**：宿主日志
   里紧跟 `Failed to create sandbox` 抓捕获的守护进程 stdout/stderr——每修一层，
   错误形态就推进一层；逐层迭代。

验证结果（真实运行 + 平台回显，2026-11-17）：WS chat（`swarmlive-*`）→平台 trace
AGENT→`task 1`→`react step N`→LLM；AGENT tokens = Σ LLM；input/output.messages
结构化；`system_instructions` 扁平化为纯文本；`deepagent.*` /
`context.window.commit` / `execution.subject.*` 裁剪全部生效；cron（`0 */10`）首次
触发于 11:30 → 平台 trace 同构（4 个 react 轮；AGENT tokens 跨轮聚合：120356）。
副作用（无害）：每次启动的 prewarm 会话产出一个 0-token AGENT 桩 span；chat 树比
develop 时期少一层——没有 `invoke_agent main_agent`（AGENT 直连 `task 1`）。

### 9.2 已知兼容性备忘（2026-11-19）

已验证的宿主组合（从零重建并端到端验证）：jiuwenswarm 0.2.8（develop）+
openjiuwen 0.1.20；以及 jiuwenswarm 0.2.3（dev-stable）+ openjiuwen 0.1.16（精确
pin 到 dev-stable 头提交 `77b90c0b`）。可长期依赖的结论：

- 生态依赖全部按精确提交 pin：上游仓库几天内就会移动/重构，dev 分支头可以带着与
  早先发布 tag 相同的版本字符串却装着不同模块（版本号 ≠ 内容）。
- 依赖树上所有 openjiuwen 引用必须解析到同一个字符串相同的 ref；提交 ref 旁边挂
  一个分支名 ref 会让 pip 抛 ResolutionImpossible——哪怕版本一致（修复：本地 clone
  冲突包，把依赖指向本地副本）。
- fastmcp-slim 与官方 fastmcp 共享 `fastmcp/` 路径，openjiuwen 任何（重）装都会
  互相覆盖；之后重跑 `pip install --force-reinstall --no-deps -i
  https://pypi.org/simple fastmcp-slim==3.4.0`；slim 的 server 组件还需要
  `griffe`（其依赖元数据里没有）。
- 无头环境用 app 模式（`python -m jiuwenswarm.start_services app`）；web 模式需要
  构建好的前端 dist；宿主 WS 客户端端口每次启动重算，不可硬编码。
- pin 组齐全时的端到端结果：.pth 注入在每个宿主进程生效；真实 WS chat 产出完整
  15-span ReAct 树，cron 运行在平台上报同构的 12-span trace，聚合/裁剪/事件断言
  全过。

## 10. 用 Docker 起全新宿主做回归

[openjiuwen/docker/](../docker/) 提供**纯净** jiuwenswarm standalone 镜像（app + web，不含探针与凭据）：每次 `docker run` 都是全新环境，探针在容器起来后从 GitHub Release 下载安装，`docker restart` 生效。构建需访问 gitcode（宿主源码与依赖）与 npm（前端 dist）。

```bash
# 构建（宿主源码钉定在 Dockerfile 的 JIUWENSWARM_REF）
docker build -f openjiuwen/docker/Dockerfile -t agentarts/jiuwenswarm-host:dev-stable openjiuwen/docker

# 一键回归：run → 容器内从 GitHub Release 装探针 → restart → 校验日志 → HTTP 造一次对话
uv run python openjiuwen/docker/probe_smoke.py \
  --model-api-base https://your-model-endpoint/v1 --model-api-key sk-xxx --model-name your-model \
  --agentarts-token <token> --agent-resource-id <agent_id>
```

- 探针安装源默认是 GitHub Release 的 wheel URL（`--probe-spec` 可覆盖，换版本时改 `DEFAULT_PROBE_SPEC` 或传参），**不需要在本机构建 wheel**，镜像里也不预置探针。
- 端口：5173（Web UI）、19002（Gateway Web HTTP `/api/v1`）；镜像内已设 `WEB_HOST` / `FRONTEND_HOST=0.0.0.0`（宿主默认只绑 127.0.0.1，容器里不这样改就映射不出去）。模型凭据用 `-e` 注入。
- 探针凭据不要只靠 `-e`：宿主拉起 jiuwenbox 沙箱子进程时按白名单过滤环境变量，token / `agent_resource_id` 不会透传，沙箱内的探针会 `skip`。`probe_smoke.py` 会把它们写进容器内 `~/.jiuwenswarm/config/.env`，保证每个进程（含沙箱）都能布防。
- 手工等价四步：`docker run -p 19002:19002 -p 5173:5173 ...` → `docker exec <c> python -m pip install --no-deps <GitHub wheel URL> && docker exec <c> agentarts-instrumentation-inject` → `docker restart <c>` → `docker logs <c>` 看到 `armed` / `applied` 后再发对话。
- PASS 只看本地：`armed`（进程启动即有）+ `applied`（宿主首次 import 观测模块，通常就是第一条对话）齐，且 `/api/v1/chat/completions` 返回非空回答；数据是否入库仍需到 AgentArts 控制台确认。
- 不要挂载 `~/.jiuwenswarm`，容器销毁即环境归零；需要留现场用 `--keep`。
