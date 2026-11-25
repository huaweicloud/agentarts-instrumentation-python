# agentarts-instrumentation-openjiuwen Design Document

- Version: v0.1 (draft)
- Date: 2026-11-14
- Status: pending review
- Type: standalone SDK repository; non-intrusive integration into jiuwenswarm / agent-core

## 1. Background and Goals

### 1.1 Background

jiuwenswarm (the host application) depends on `openjiuwen` (i.e. the agent-core repository) via pip; the two share the same OTel main stack (`openjiuwen/extensions/observability/`): a process-global singleton `TracerProvider`; exporters are constructed by the `build_span_exporter` factory according to `ObservabilityConfig.exporter`, supporting five kinds — `otlp_grpc` / `otlp_http` / `langfuse` / `file` / `console` — with only one auth shape, langfuse pk:sk → Basic.

New requirement: report third-party agent observability data to the **Huawei Cloud AgentArts intelligent-agent observability platform** (OTLP access; semantic spec:
<https://support.huaweicloud.com/ops-agentarts/agentarts_14_0127.html>), with the following requirements:

1. Support generic authentication (Bearer / API-key / custom headers);
2. Support **attribute projection/trimming**, with a built-in **Huawei AgentArts semantic projection preset** (§4.3):
   backfill platform-mandatory fields such as `gen_ai.span.kind` and `gen_ai.usage.total_tokens`,
   fix key-name differences such as `cache_write → cache_creation`, recognize Skill reads from
   tool-call paths and derive `gen_ai.skill.*` (no standalone spans), and trim optional bulky
   attributes;
3. Deliver as a **standalone SDK repository**, without modifying the agent-core / jiuwenswarm repositories;
4. The same SDK serves both jiuwenswarm and scenarios that use openjiuwen standalone (Team harness,
   single-Agent harness, RL, RSI, etc.).

### 1.2 Goals and Non-Goals

**Goals**

- Provide `build_exporter(config) -> SpanExporter`, mounted into the shared provider via agent-core's
  official injection parameter `span_exporter_override`;
- The SDK has **zero dependency** on openjiuwen, depending only on official opentelemetry packages;
- The local trajectory data plane (`SpanRecordProcessor` → SQLite → trajectory UI / diagnostics) is
  unaffected by trimming.

**Non-Goals**

- No metrics / logs pipeline;
- No generic OTLP receiver / collector;
- No semantic rewriting beyond attribute projection (e.g. GenAI semconv evolution);
- No modifications to agent-core / jiuwenswarm repository code (a first-class config.yaml switch is
  an optional follow-up).

## 2. Current State and Feasibility Basis (all verified against code)

### 2.1 Injection point: `span_exporter_override` is an official first-class API

```python
# agent-core: openjiuwen/extensions/observability/setup.py:21
def init_observability(
    config: ObservabilityConfig,
    *,
    span_exporter_override: SpanExporter | None = None,
    additional_span_processors: Sequence[SpanProcessor] = (),
) -> None: ...
```

Key behaviors (`openjiuwen/extensions/observability/runtime.py`):

- **Provider does not exist yet**: `exporter = span_exporter_override or build_span_exporter(config)`
  (runtime.py:158); the override **fully replaces** the factory product;
- **Provider already exists** (first-initializer-wins): the override **is not dropped**; instead a
  `SimpleSpanProcessor(override)` is appended (runtime.py:139-142; the source comment states
  explicitly: "A later caller can still supply an exporter override that must not be dropped");
- The Team-side `init_observability` passes the override through with the same signature
  (`openjiuwen/agent_teams/observability/setup.py:42-47`).

> Conclusion: the injection channel for external exporters natively exists in agent-core; the SDK
> needs no agent-core changes whatsoever.

### 2.2 Version compatibility: a loose constraint covers both environments

| Environment | opentelemetry constraint | Source |
|---|---|---|
| jiuwenswarm (currently pinned `f3f8f576`) | `>=1.25.0`; jiuwenswarm itself locks `>=1.28,<1.42` | jiuwenswarm pyproject.toml:62-65; f3f8f576 pyproject |
| agent-core HEAD | `>=1.45.1` (base), uv.lock 1.45.2 | agent-core pyproject.toml:86 |

The `SpanExporter` / `ReadableSpan` interfaces are stable across the entire opentelemetry 1.x line.
By setting the SDK constraint to `>=1.25,<2.0`, it installs into both environments, **sidestepping
the "must bump jiuwenswarm's otel pin" dependency-upgrade problem** (unavoidable if the agent-core
factory itself were modified — the core dividend of the standalone-SDK approach).

### 2.3 Limitation: the `acquire_observability` wrapper does not pass the override through

The demand wrappers of the harness (`openjiuwen/harness/observability/setup.py:72`) and team
(`openjiuwen/agent_teams/observability/setup.py:87`) accept only `config`, not exporter parameters.
Therefore, for the SDK to become a first-class citizen of jiuwenswarm's config.yaml, a small amount
of host-side glue is needed (see §6.3, optional).

## 3. SDK Structure

Repository layout (uv workspace, flat at repo root):

```
agentarts-instrumentation-python/
├── pyproject.toml                # workspace root: members = [core, openjiuwen]
├── core/                         # framework-agnostic kernel (workspace-internal, not published separately)
│   └── src/agentarts_instrumentation/
│       ├── config.py             # ExporterConfig: endpoint/protocol/headers/projection/batching params
│       ├── projection.py         # Projection model + derivation registry + project_span pipeline
│       ├── transform.py          # clone_readable_span + TransformingSpanExporter (copy-on-export)
│       ├── exporter/             # otlp.py (official exporter build) + buffer.py (BufferedOtlpExporter)
│       ├── factory.py            # build_exporter(config) -> SpanExporter
│       └── internal/             # credentials.py + inject.py (.pth installer + DeferredPatchFinder)
└── openjiuwen/                   # pip: agentarts-instrumentation-openjiuwen
    ├── src/agentarts_instrumentation/openjiuwen/
    │   ├── preset.py             # agentarts derivation: openjiuwen contract -> gen_ai.* platform semantics
    │   └── inject.py             # init_observability wrapper + inject/uninject command entries
    ├── docs/                     # this document, guides, 接入指南
    ├── examples/
    └── tests/
```

The openjiuwen wheel is **self-contained**: hatch `force-include` bundles the core namespace
modules into the distribution, so users install a single package (no separate core dependency).

Dependencies (openjiuwen fat wheel):

```toml
dependencies = [
    "opentelemetry-sdk>=1.25,<2.0",
    "opentelemetry-exporter-otlp-proto-http>=1.25,<2.0",
    "pydantic>=2.5,<3",
]
[project.optional-dependencies]
grpc = ["opentelemetry-exporter-otlp-proto-grpc>=1.25,<2.0"]
test = ["pytest>=7"]
```

- The HTTP exporter ships in the base deps; gRPC (the AgentArts gateway channel) is an extra:
  `pip install "agentarts-instrumentation-openjiuwen[grpc]"`;
- Config models use pydantic v2 (consistent with the agent-core / jiuwenswarm ecosystem).

## 4. Component Design

### 4.1 ExporterConfig (config.py)

```python
class ExporterConfig(BaseModel):
    endpoint: str = Field(min_length=1)    # OTLP endpoint (HTTP needs the full /v1/traces URL; gRPC takes host:port or a URL form)
    protocol: Literal["grpc", "http"] = "http"
    headers: dict[str, str] = Field(default_factory=dict)  # auth & custom headers, passed straight to the official exporter
    projection: Projection | None = None   # None disables projection (behavior identical to the official exporter)
    queue_size: int = Field(default=4096, ge=1)          # bounded queue capacity (count); evicts oldest when full
    queue_max_bytes: int = Field(default=134217728, ge=1024)  # bounded queue capacity (estimated bytes); same eviction
    batch_size: int = Field(default=32, ge=1)
    batch_max_bytes: int = Field(default=10_000_000, ge=1) # estimated byte cap per batch; a would-overflow span starts the next batch, an oversized single span ships alone (never dropped)
    flush_interval_ms: int = Field(default=500, ge=1)
    otlp_timeout_ms: int = Field(default=30000, ge=100)  # per-RPC timeout of the inner official exporter (incl. internal retries)
    export_attempts: int = Field(default=5, ge=1)          # total attempts per failed batch (exponential backoff 1/2/4/8s; drop only when exhausted)
    export_timeout_ms: int = Field(default=5000, ge=100) # shutdown/force_flush drain timeout, aligned with the main stack
```

### 4.2 Projection (projection.py)

```python
class Projection(BaseModel):
    preset: Literal["agentarts", "none"] = "none"   # agentarts = Huawei semantic preset (§4.3)
    attributes: dict[str, str] = Field(default_factory=dict)  # config-supplied common attributes, injected into every exported span
    drop: list[str] = Field(default_factory=list)    # exact key names, or prefix* matching (e.g. "gen_ai.input.messages*")
    truncate: int = Field(default=0, ge=0)           # 0 = no truncation; positive = string-value truncation at export time
    rename: dict[str, str] = Field(default_factory=dict)  # key renames (projection onto backend-expected names)
    service_name: str = ""                           # rewrites resource service.name on the exported clone (AgentArts requires AgentArts.<agent_id>.default)
    skill_path_patterns: list[str] = Field(default_factory=lambda: ["SKILL.md", "skills"])  # skill-detection path features (§4.3.5)
```

`project_span(span, projection) -> ReadableSpan | None` is a pure function (it never mutates the
input span); semantics:

- `attributes`: config-supplied keys are injected first (explicit user config wins; existing span
  keys are never overwritten);
- `preset="agentarts"`: applies the Huawei semantic derivations and key-name fixes (§4.3);
- `drop`: exact match first, then prefix match (`prefix*` form);
- `truncate`: truncates string values only; int/bool/list untouched;
- `rename`: renames keys; execution order is inject → preset derive → drop → rename → truncate;
- empty config = identity pass-through (zero-overhead short circuit).

### 4.3 Huawei AgentArts semantic mapping preset (preset: agentarts)

Based on "OpenTelemetry Protocol Field Mapping" (Huawei Cloud AgentArts agent observability
documentation, updated 2026-11-02). The preset projects canonical spans produced by openjiuwen
into the shape the platform requires via the three rule classes below; the rules are based on the
**attribute-key data contract** (`openjiuwen.*` attribute values) and create no code dependency on
openjiuwen.

#### 4.3.1 Derivation (derive; platform-mandatory fields the span does not carry)

| Platform field | Mandatory | Derivation rule (by priority) |
|---|---|---|
| `gen_ai.span.kind` | Yes | `openjiuwen.trace.root=true` or `openjiuwen.trajectory.record.kind=turn` → `AGENT`; `gen_ai.tool.name` present or `gen_ai.operation.name=execute_tool` → `TOOL`; `openjiuwen.trajectory.record.kind=step` or span name matching `*.react_iteration.*` → `STEP`; `gen_ai.operation.name ∈ {chat, text_completion, generate_content}` → `LLM`; span name matching `team.*` / `task.*` → `WORKFLOW` |
| `gen_ai.usage.total_tokens` | Mandatory for AGENT/LLM | backfilled as `input_tokens + output_tokens`; when the AGENT root has no usage, `_TraceTokenAggregator` aggregates across spans: LLM spans (which end first) accumulate input/output, and the AGENT root (which ends later) consumes the sum and writes back the three keys (host-written values are never overwritten; verified live: AGENT tokens = Σ LLM tokens). The two cache keys are aggregated the same way (user request 2026-11-21): `gen_ai.usage.cache_read.input_tokens` / `gen_ai.usage.cache_creation.input_tokens` (host semconv keys, written on LLM spans only; the probe accumulates them onto AGENT = Σ LLM) |
| `gen_ai.agent.name` | Mandatory for AGENT | takes `openjiuwen.agent.mode`; when missing, not written and not guessed (can be supplied directly via `Projection.attributes`) |
| `gen_ai.react.round` | No | `openjiuwen.turn.number` / the N in `react_iteration.N` within the span name |
| `gen_ai.skill.name` / `gen_ai.skill.id` / `gen_ai.skill.version` | Recommended (name must be reported when a skill is invoked) | recognized from path features in the TOOL span's `gen_ai.tool.call.arguments` as Skill-document reads and derived (§4.3.5); **no standalone span is created**, consistent with the platform spec |

#### 4.3.2 Key-name fixes (rename; platform naming differs from OTel GenAI semconv / project keys; implemented and verified)

| Platform field | Existing openjiuwen key | Action |
|---|---|---|
| `gen_ai.usage.cache_creation.input_tokens` | `gen_ai.usage.cache_write.input_tokens` | rename (implemented) |
| `gen_ai.response.time_to_first_token` (int, ms) | `openjiuwen.gen_ai.response.time_to_first_token_ms` (int, ms, semconv.py:145) | rename (implemented) |
| `gen_ai.request.is_stream` | `gen_ai.request.stream` (gen_ai_semconv.py:59; written at callback_handler.py:1096) | rename (implemented) |
| `gen_ai.tool.call.id` | agent-core already uses the platform's same-named key (callback_handler.py:1593) | pass-through; no handling needed |
| `gen_ai.system_instructions` (plain text) | agent-core writes a parts array `[{"type","content"}]` (callback_handler.py:1760); the official probe measured-reports a **plain string** (openclaw trace contract), and the console renders it as plain text | format flattening (implemented): array → plain text of concatenated content values; already-plain-text / parse failure left untouched |

#### 4.3.3 Config-supplied attributes (common attributes, table 2, injected into every span)

| Platform field | Mandatory | Source |
|---|---|---|
| `gen_ai.resource.id` | Yes | user configuration (agent ID assigned by the AgentArts console) |
| `gen_ai.resource.type` | Yes | user configuration (agent / multiagents / workflow) |
| `domain.id` | No | user configuration (tenant isolation) |
| `gen_ai.user.id` | Recommended | user configuration (end-user identity; or derived from `openjiuwen.execution.subject.*`, to be decided during implementation) |
| `gen_ai.conversation.id` | Recommended | already carried by the span (run root span); pass-through |
| `resource_version` | No | user configuration |

#### 4.3.4 Pass-through items (openjiuwen already produces the platform-required structures)

`gen_ai.request.model` (mandatory for LLM), `gen_ai.input.messages` /
`gen_ai.output.messages` (recommended; the `{"role","parts"}` structure matches the platform
examples), `gen_ai.tool.name` / `gen_ai.tool.call.arguments` / `gen_ai.tool.call.result`
(mandatory for TOOL), `gen_ai.usage.input_tokens` / `output_tokens` (mandatory),
`gen_ai.response.finish_reasons` (recommended).

#### 4.3.5 Skill-invocation detection (attached to tool spans; no standalone span)

The platform spec is explicit: a skill invocation **does not create a standalone span**; the
information is tracked by setting `gen_ai.skill.*` attributes on the tool-execution span (paired
with `skill.loaded` / `skill.released` events). In openjiuwen scenarios, skill loading/usage
manifests as **tool calls that read `SKILL.md` or documents under a skill directory**
(read/grep/glob etc.), and the paths are already carried in `gen_ai.tool.call.arguments` — the SDK
recognizes and derives them from path features at projection time, with no agent-core changes.

**Recognition rules (applied to TOOL spans only; existing information wins, path inference is
merely the fallback)**:

0. **Standdown check**: if the span already carries a `gen_ai.skill.name` attribute or a
   `skill.loaded` event (e.g. a future agent-core producing them natively per the platform spec),
   **trust the existing information and skip path matching**, backfilling only the missing
   `gen_ai.skill.id` / `version` and never overwriting any existing key (consistent with §4.2's
   `attributes` injection principle of "never overwrite existing span keys") — once agent-core
   supports this natively, the SDK automatically degrades to pass-through for skills, with no
   code change needed; as verified (2026-11), spans in neither openjiuwen repository carry skill
   attributes/events today, so path matching is the only information source;
1. parse `gen_ai.tool.call.arguments` (JSON string) and take path-like parameters
   (`path` / `file_path` / `file` / `dir` / `pattern` etc.; the key set to be checked against the
   actual jiuwenswarm / agent-core tool parameter shapes during implementation);
2. hit any default feature (extendable via `skill_path_patterns`):
   - the file name is `SKILL.md` (case-insensitive);
   - the path contains a directory segment named `skills` (e.g. `.agents/skills/<name>/...`);
3. derive:
   - `gen_ai.skill.name`: the parent directory name of `SKILL.md`, or the segment after the
     `skills` directory segment;
   - `gen_ai.skill.id`: filled with the skill name (no platform-side id source; field optional);
   - `gen_ai.skill.version`: extracted if the path contains a version segment, otherwise not
     reported (optional);
4. events: when a `SKILL.md` read is hit, append the span event `skill.loaded` with
   attributes `{skill.name, skill.path}` (the platform example shape); appending an event must
   construct a **new events list** (copy + append) — the original span must never be mutated;
5. `gen_ai.operation.name` stays `execute_tool` unchanged (`load_skill` is AgentArts' own
   runtime tool naming; platform table 5 imposes no such requirement on TOOL span
   operation.name);
6. conservative policy: when one tool call hits different skills via multiple path parameters,
   take only the first hit and warn; `gen_ai.tool.call.result` does not participate in detection
   (v1).

#### 4.3.6 Platform-spec items not covered for now

| Platform requirement | Status |
|---|---|
| `skill.released` event / `skill_complete` shape | openjiuwen has no explicit "release skill" action to recognize; not derived in v1; to be added later if agent-core emits skill lifecycle events |
| `MEMORY` span type | openjiuwen has no memory-service span source; not derived for now |
| Metric indicators (platform tables 9/10: `gen_ai.skill.call.count` / `gen_ai.skill.duration` / `gen_ai.usage.status`, `total.requests`, `client.operation.duration`, TTFT Histogram etc. — 13 items plus mandatory dimensions) | Out of scope for SDK v0.1 (trace only); platform metric dashboards such as "user trends / QPS / TTFT distribution / skill success rate" require a later iteration adding metric reporting. Available with trace-only reporting: call chains, conversation analysis, token usage & tool analysis (the parts computed from trace attributes), and skill display at the call-chain level |

#### 4.3.7 Preset trimming (framework-namespace wholesale trimming + trajectory-protocol-specific spans; revised live 2026-11-17)

The exported clone's attribute surface **keeps only platform semantics** (`gen_ai.*` / OTel
standard keys) plus user-injected attributes; host framework namespaces are trimmed wholesale
(`AGENTARTS_DROP_ATTRS` prefix wildcards). Trimming happens **after derivation** — the source
keys below were fully consumed during derivation, so trimming them affects no projection behavior:

| Consumed source key | Consumption point |
|---|---|
| `openjiuwen.trace.root` / `trajectory.record.kind` / `agent.mode` / `turn.number` | kind derivation (§4.3.1), display-name rewrite (§4.3.8, **runs before trimming**), `gen_ai.agent.name` derivation, react round derivation |
| `openjiuwen.span.input` | no consumer: the turn input is taken directly from the LLM span's own `gen_ai.input.messages` (`_TurnInputAggregator`, §4.3.2); this key appears only in historical documentation |
| `openjiuwen.span.output` (0.1.20 contract) | output wrapping source → `gen_ai.output.messages` (popped after wrapping) |
| `langfuse.observation.output` (0.1.16 contract) | when a run span has no span.output, the AGENT final answer is extracted from its dict wrapper (`_extract_langfuse_output`). **Two host writer shapes measured** (neither strict JSON; confirmed via captured debug output): shape A, double quotes + bare newline control characters (f-string concatenation) → `json.loads(strict=False)`; shape B, **single-quoted Python dict repr** (a `str(dict)` product, newlines as literal `\n` escape sequences) → JSON always fails; `ast.literal_eval` as the fallback (no code-execution risk). If both parsing levels fail, treat as missing — never write dirty values. Also measured: the host occasionally stamps `gen_ai.output.messages` directly (with its own `finish_reason`, containing bare newlines) — existing values are not overwritten; pass-through |
| `openjiuwen.gen_ai.response.time_to_first_token_ms` | rename source → `gen_ai.response.time_to_first_token` |

**Whole-namespace trimming list** (`AGENTARTS_DROP_ATTRS`, `*` prefix wildcards):

| Namespace | Reason |
|---|---|
| `openjiuwen.*` | framework data-contract keys: after consumption (see above) they carry no platform semantics; session.id duplicates `gen_ai.conversation.id` / `session.id` with the same value; locator keys such as step.id / request.id are replaceable by the platform span_id; total_latency_ms / tpot_ms / provider_metadata have no platform display slot (queryable in the local trajectory store instead) |
| `langfuse.*` | Langfuse mirror: observation.input/output/type duplicate `gen_ai.*` values (doubling the payload); the 0.1.16 AGENT final answer was already extracted during derivation |
| `agentteam.*` | team session ids etc.: duplicates of the session keys in single-agent scenarios |
| `deepagent.*` | jiuwenswarm executor internal instrumentation: iteration duplicates the display name; loop_event holds dirty repr values of unserialized Python objects |

**Exact-key trimming** (same table, non-prefix): `gen_ai.tool.input` / `gen_ai.tool.output`
(measured duplicate values 2026-11-17: tool.input is an `[args list + session metadata]` wrapper
(containing the session id), and tool.output is fully identical to `gen_ai.tool.call.result` —
the platform semantic keys are already carried by `call.arguments` / `call.result`; the latter are
kept).

**events / links** (revised from same-day measurements): a streaming LLM span can carry a
hundred-plus `openjiuwen.stream.chunk` / `llm.chunk` events in a single conversation — the bulk of
trace-detail payload (measured: a single trace detail is 393KB). The preset applies an
**allowlist** to events: keep only the host-produced `skill.loaded` detection events (the
platform's skill-detection event channel, §4.3.5) + the newly synthesized skill-detection
events; all other host events are not reported; links are always cleared (the platform builds its
tree from parent_span_id and does not consume links). `otel.scope.name` is a standard OTel
instrumentation-scope key (added by the exporter) and is outside trimming scope. The local
trajectory data plane (SQLite trajectory store / jsonl) is unaffected — projection applies only
to exported clones; troubleshooting still goes through full local data.

**Whole-span trimming** (`AGENTARTS_DROP_SPANS`): `context.window.commit` — the context-window
snapshot chain span; AgentArts has no matching span type, and the window payload can reach tens
of KB on long sessions. The local trajectory data plane (SQLite trajectory store) is unaffected
and keeps the full data. Implementation semantics: `project_span` returning `None` = intentional
filter; `TransformingSpanExporter` skips that span (distinct from the transform-exception
fallback to the original, §4.4).

**Structure-aware trimming of dangling task_iteration markers** (revised from measurements
2026-11-17, `_ParentRefRegistry`): the task_iteration span plays structurally different roles in
two host generations — container (0.1.20/develop): react rounds hang beneath it (child spans'
parent points at it); dropping it would orphan the react rounds and break the platform tree —
must keep; dangling marker (0.1.16/dev-stable): react rounds hang directly on the AGENT root;
this span has no child references and no semantic attributes (blank platform span type) — pure
noise, not reported. Detection exploits OTel's "child spans end before the parent; projection
processes in end order": each span records its parent span id at projection time (bounded FIFO of
4096); when a task_iteration span is projected, it checks whether its own id was referenced —
referenced → kept (display name `task N`); unreferenced → the whole span is filtered. Safe for
both generation shapes, no version sniffing needed (verified live: the dev-stable platform tree
converges to AGENT→react step N→LLM/TOOL).

#### 4.3.7a Display-name compression (rewrites only the projected clone's name)

Host naming patterns are long (jiuwenswarm AGENT root: `agent.{mode}.{long session_id}`). The
preset compresses display names by rule (`_rewrite_agent_span_name`, verified live):

- AGENT root → `invoke_agent openjiuwen` (the session suffix and mode have no display value);
- `agent.{type}.invoke` → `{operation.name} {type}`, e.g. `invoke_agent main_agent`;
- `agent.{type}.task_iteration.N` → `task N`;
- `agent.{type}.react_iteration.N` → `react step N` (the round number also travels via the
  `gen_ai.react.round` attribute, §4.3.1);
- TOOL / LLM and non-`agent.*` names are kept as-is.

`clone_readable_span` gained an optional `name` parameter to carry the rewrite; identity fields
such as trace/span id and the parent chain are unaffected, and the local trajectory store still
receives the original names. kind / react.round derivation runs before the rewrite (using the
original name and attributes); display-name compression takes part in no semantic logic.

#### 4.3.9 Child-span error propagation (AGENT root status escalation; plan B revision 2026-11-21)

The host calls `set_status(ERROR)` only on failed business child spans (LLM call errors,
tool-reported failures `TOOL_REPORTED_FAILURE`); the AGENT root does not inherit. The 0.1.2
first-version policy was "any failed child span → root ERROR"; live measurement (trace
c2acd7**[redacted]**: web_search failed → fetch_webpage self-healed → task completed, yet the
root was marked ERROR) showed that this policy counted self-healing runs as failures, distorting
the success-rate metric. **Plan B (0.1.4) tightens this to terminal failures**:

- **Registration**: when a business child span is projected, its status is recorded — ERROR
  registers a failure (resetting the recovery flag; terminality is defined as "no successful
  business span after the last failure"); OK registers a recovery signal (the host's success path
  calls `Status(OK)` explicitly: tool callback L864, LLM L1199); None/UNSET (STEP etc.) registers
  neither — the same-turn wrap-up after a tool failure is not evidence of self-healing. The root
  itself is never registered; registration happens after DROP_SPANS filtering (internal
  bookkeeping spans do not participate);
- **Consumption**: taken at kind=AGENT projection. Terminal failure (unrecovered) → the projected
  clone is upgraded to `Status(ERROR, "child span failed: {last failed name}")`; recovered → the
  root status is left untouched (reflecting run completion), and the projected clone's
  `gen_ai.error.recovered` attribute leaves a trace (visible on the platform; ops can still spot
  child-span degradation); an already-ERROR root is never overwritten.

On multi-root traces, the first AGENT span to arrive consumes it (same limit as the token/input
aggregators).

### 4.4 transform.py: self-contained clone + wrapper

Independently implements the equivalent logic of agent-core's
`extensions/observability/exporters/transforming.py` (~100 lines): `clone_readable_span` (full
span copy, replacing only attributes) + `TransformingSpanExporter` (per-span copy + transform at
export time, with two special semantics: **when transform raises, fall back to exporting the
original span with a warning** — telemetry is never lost to a projection failure; **when
transform returns None, the span is intentionally filtered**, used by the preset's whole-span
trimming §4.3.7).

Why openjiuwen is not imported: the SDK's zero dependency (§5.1); the cost is duplicated logic
(§9.1).

### 4.5 BufferedOtlpExporter (exporter.py) — the core of this SDK

**Why built-in batching is mandatory**: the injection path goes through `SimpleSpanProcessor`
(runtime.py:159-160); the moment a span ends, `exporter.export()` is called **synchronously**, so
the official `BatchSpanProcessor`'s buffering is not in the chain. If the SDK merely wrapped the
official exporter, network IO would be pulled into business threads, violating the main-stack
invariant "span export never blocks the business thread".

Structure:

```
SimpleSpanProcessor
  └─ BufferedOtlpExporter            # SDK self-contained
       ├─ bounded queue (queue_size)
       ├─ background flush thread    # takes batch_size items / batch_max_bytes est. bytes / flush_interval_ms, whichever first
       ├─ inner: official OTLPSpanExporter # retry / compression / protocol guaranteed upstream
       └─ Projection (optional; applied to the clone at enqueue or dequeue)
```

Behavior contract:

- `export(spans)`: clone (if projection is configured) → enqueue → **return immediately**, never
  blocking the caller;
- backpressure: when the queue is full, **drop the oldest batch with a warning** (following the
  bounded-queue pattern of jiuwenswarm's trajectory sink); never raise into business flow;
- `force_flush(timeout_millis)`: drain the queue (with a deadline; returns False on timeout) then
  delegate to the inner force_flush. Note: the official OTLP exporter has immediate-send
  semantics and **provides no force_flush method** (verified during implementation) — delegate
  when the inner method exists, treat queue drain as success otherwise;
- `shutdown()`: drain (bounded by export_timeout_ms) → close the inner exporter → idempotent
  (repeated calls have no side effects; lesson source: the span-finalization leak class of
  problems fixed by agent-core `279086272`);
- the projection runs at **pre-enqueue** (lightweight clone/drop on the export-calling thread;
  the background thread only serializes and sends): dropping bulky attributes before enqueue
  significantly reduces queue memory usage.

### 4.6 factory.py

```python
def build_exporter(config: ExporterConfig) -> SpanExporter:
    inner = _build_official_otlp_exporter(config)
    buffered = BufferedOtlpExporter(inner, config)
    projection = config.projection
    if projection is not None and _projection_active(projection):
        return TransformingSpanExporter(
            buffered,
            transform=lambda span: project_span(span, projection),
        )
    return buffered
```

## 5. Key Design Decisions and Rationale

| # | Decision | Rationale |
|---|---|---|
| 1 | Zero dependency on openjiuwen | Version decoupling (§2.2): compatible with both jiuwenswarm's older environment and agent-core's newer environment; the SDK is also usable by any third-party OTel user |
| 2 | Built-in batch buffering | The SimpleSpanProcessor injection semantics are a synchronous call (§4.5); not blocking business threads is a hard constraint |
| 3 | The projection runs on a clone, never modifying the original span | `SpanRecordProcessor` / the SQLite trajectory store sit on another branch of the processor chain and keep receiving **full canonical data**; trajectory UI and diagnostics are unaffected by trimming |
| 4 | Transform failure falls back to the original span | No telemetry loss; same semantics as agent-core's TransformingSpanExporter |
| 5 | Protocol via the official OTLP exporter (gRPC / HTTP-protobuf) | Retry, compression, protocol compliance guaranteed upstream; the SDK only layers buffering and projection on top |
| 6 | Headers passed through as-is (no auth-shape restriction) | Basic / Bearer / x-api-key are all covered by user headers configuration |
| 7 | Loose version constraint `>=1.25,<2.0` | The interfaces are stable across 1.x; three environments (1.28 / 1.42 / 1.45) verified in CI |
| 8 | The Huawei semantic mapping is a **configurable preset** (`preset: agentarts`) rather than hard-coded into the exporter | The platform spec "will keep evolving with the community" (original doc wording); preset rules must be able to evolve with versions; with `preset: none` the SDK is a plain generic OTLP exporter and non-AgentArts users are unaffected by Huawei semantics |
| 9 | Preset rules are based on the attribute-key data contract (`openjiuwen.*` attribute values + span names), not importing openjiuwen | Keeps zero dependency (decision 1); isomorphic to how jiuwenswarm's frontend consumes semconv constants |

## 6. Integration

### 6.1 Standalone agent-core (zero changes)

```python
from openjiuwen.extensions.observability import init_observability
from openjiuwen.extensions.observability.config import ObservabilityConfig
import agentarts_instrumentation.openjiuwen
from agentarts_instrumentation import build_exporter, ExporterConfig

sdk_exporter = build_exporter(ExporterConfig(
    endpoint="https://<backend>/v1/traces",
    protocol="http",
    headers={"authorization": "Bearer <token>"},
))
init_observability(
    ObservabilityConfig(enabled=True, service_name="my-app"),
    span_exporter_override=sdk_exporter,
)
```

(Team scenarios use `openjiuwen.agent_teams.observability.init_observability` — same signature.)

### 6.2 jiuwenswarm (zero changes, append-mount)

In a process startup hook / plugin layer:

```python
from openjiuwen.extensions.observability import init_observability, get_config
import agentarts_instrumentation.openjiuwen
from agentarts_instrumentation import build_exporter, ExporterConfig

cfg = get_config() or ObservabilityConfig(enabled=True, service_name="jiuwenswarm")
init_observability(
    cfg,
    span_exporter_override=build_exporter(ExporterConfig.from_env_or_yaml(...)),
)
```

- If the provider does not exist yet: the SDK becomes the sole main exporter;
- If the provider was already established by acquire (the common ordering): append
  `SimpleSpanProcessor(SDK)` — "main exporter (e.g. file) + SDK" dual-path coexistence.

### 6.2.1 Integration pitfall: the root span must go through agent-core's run span API (a measured lesson)

If the host opens the root span with a bare `tracer.start_as_current_span(...)`, it **only enters
the OTel context, not agent-core's session root span registry**; when `OtelCallbackHandler` opens
LLM/TOOL spans it looks up the parent from the registry — not finding one, it produces no span
(anti-orphan mechanism) — the SDK would receive only a lone root span and lose the entire
LLM/tool layer (reproduced and fixed in live testing).

The correct pattern (the same one jiuwenswarm uses in production):

```python
from openjiuwen.harness.observability import acquire_observability   # opens the tracing gate
from openjiuwen.harness.observability.run_span import (
    open_agent_run_span, stamp_run_output, close_agent_run_span,
)

acquire_observability(cfg)                    # gate: is_tracing_enabled() is True
init_observability(cfg, span_exporter_override=sdk)  # append dual-path (§6.2)
handle = open_agent_run_span(session_id=..., mode="code")   # registered into the registry
...  # LLM/tool calls: OtelCallbackHandler finds the parent via the registry and produces child spans normally
stamp_run_output(handle, answer)              # trace-level output (redaction via the active config)
# The SDK side automatically backfills the platform-mandatory AGENT input/output (zero host
# changes), consistent with the LLM span format (the platform table 3 examples are exactly
# [{"role","parts"}] structured JSON):
#   output —— the preset's built-in wrapping openjiuwen.span.output → gen_ai.output.messages
#            (the plain text written by stamp_run_output → an assistant structured message;
#             an existing structured LLM output target is not overwritten)
#   input  —— the preset's _TurnInputAggregator aggregates across spans: LLM spans end first,
#            and the first user message of their gen_ai.input.messages is the turn input,
#            recorded as-is in structured form (first-write-wins; non-JSON plain text is
#            wrapped as a user message); the AGENT root ends later and consumes it at
#            projection to backfill; bounded at 1024 traces, thread-safe; exact for
#            single-root scenarios; multi-root traces only backfill the first root to arrive.
# Verified via live platform echo-back (2026-11-14 trace real-llm-20261114-215233: AGENT in/out both present).
close_agent_run_span(handle, session_id=...)  # wrap-up + best-effort closing of child spans
```

Measured result (a real deepseek call, SESSION_ID `real-llm-20261114-200821`): an AGENT root +
LLM (`chat`) + `context.window.commit` — three spans forming a tree; real usage (input 18 /
output 64 / total 82) was projected by the SDK, reported, and echoed back by the platform.

### 6.3 jiuwenswarm config.yaml first-class switch (optional, later iteration)

Precondition: the `acquire_observability` wrapper does not pass the override through (§2.3).
Touch points:

1. `jiuwenswarm/agents/harness/observability_runtime.py`'s `build_observability_config` gains
   recognition and construction of the SDK config section (~a dozen lines);
2. the `resources/config.yaml` template gains an example section;
3. because the demand wrapper does not pass the override through, the exporter instance must be
   passed into `init_observability` at the acquire call site (or take the §6.2 append path,
   accepting dual-path semantics).

### 6.4 Recommended configuration example (AgentArts preset + trimming of bulky attributes)

```yaml
endpoint: https://<agentarts-trace-endpoint>     # e.g. https://apm-access.cn-southwest-2.myhuaweicloud.com
protocol: grpc                                   # measured: apm-access publishes only the gRPC channel (HTTP POST 404 APIG.0101)
headers:
  authentication: "<trace_token>"               # HTTP/2 metadata requires lowercase keys; the gateway is case-insensitive
projection:
  preset: agentarts
  service_name: "AgentArts.<agent_id>.default"   # mandatory: a missing service.name gets a gateway 504 (unroutable to a backend by service name)
  attributes:
    gen_ai.resource.id: "<agent_resource_id>"   # assigned by the AgentArts console; every span must carry it
    gen_ai.resource.type: "agent"
    domain.id: "<domain_id>"
    gen_ai.user.id: "<end_user_id>"
  drop:            # optional; input/output.messages are "recommended" on the platform — trimmable for privacy/weak-network scenarios
    - "gen_ai.input.messages*"
    - "gen_ai.output.messages*"
  truncate: 4096
```

**Measured integration recipe (2026-11, verified by live echo-back on cn-southwest-2)**:

1. gRPC + TLS: with an https endpoint the SDK automatically uses a secure_channel; **an explicit
   `insecure=True` overrides the official exporter's scheme inference**, and plaintext gRPC
   against a TLS gateway always fails (504) — the SDK factory already decides insecure from the
   endpoint scheme (§4.6);
2. the auth header uses lowercase `authentication` (HTTP/2 metadata spec; uppercase keys are
   rejected outright by grpc-core with "Illegal header key");
3. resource `service.name` must be `AgentArts.<agent_id>.default`: requests with an empty
   service.name hit a gateway 504 (no routing target); with a real token and the correct
   service.name, a complete span payload was measured as accepted by the platform (returning
   SUCCESS).

Corresponding Python:

```python
build_exporter(ExporterConfig(
    endpoint="https://<agentarts-otlp-endpoint>/v1/traces",
    headers={"authorization": "Bearer <token>"},
    projection=Projection(
        preset="agentarts",
        attributes={"gen_ai.resource.id": "...", "gen_ai.resource.type": "agent"},
    ),
))
```

- `preset="agentarts"` automatically performs: `gen_ai.span.kind` derivation,
  `usage.total_tokens` backfill, key-name fixes such as `cache_write → cache_creation`, and
  `gen_ai.agent.name` derivation;
- without a projection configured (or with `preset: none`), it is a plain generic OTLP exporter
  whose behavior is fully identical to the status quo (zero breakage).

### 6.5 Service-style host runtime injection (.pth mechanism; jiuwenswarm measured recipe)

When the host runs as a resident service / multi-process (jiuwenswarm: app / agentserver /
gateway / worker), `span_exporter_override` cannot be passed directly as a parameter (§6.2 is
the library-side/test-side path), and config.yaml's `otlp_grpc` does not satisfy the AgentArts
contract (agent-core `build_span_exporter`: `insecure=True` hard-coded + Langfuse Basic auth).
Approach: **pip-install the package and run `agentarts-instrumentation-inject`** — it writes a
guarded `agentarts_instrumentation_openjiuwen_inject.pth` into the venv's site-packages which
imports the in-package inject module in **every** interpreter process; the module's `main()`
loads credentials, then registers a `sys.meta_path` finder so that once the host itself finishes
importing the observability module, `init_observability` is patched at **both entry points**
(`extensions.observability.setup` + `agent_teams.observability.setup`, sweeping early-bound
references of the original function along the import chain — see §6.5.1) to inject the exporter
automatically.

Measured essentials (2026-11, jiuwenswarm 0.2.8 (develop branch) + openjiuwen 0.1.20 +
OTel 1.41.x; live runs + real AgentArts echo-back verification):

1. **sitecustomize.py cannot be used**: on Debian/Ubuntu the system-level
   `/usr/lib/<py>/sitecustomize.py` (apport) precedes the venv's site-packages on sys.path, so a
   same-named file never loads; a `.pth` executes during site processing, earlier than business
   imports, covering all processes;
2. **token timing**: injection happens before the host's `load_dotenv`; the module must read the
   official access variables itself — from the environment, from the env file pointed to by
   `AGENTARTS_ENV_FILE`, or (default, since 2026-11-24) from the host's own
   `~/.jiuwenswarm/config/.env` (quote handling unified with python-dotenv: matched surrounding
   quotes are stripped, so the same file can mix the host's quoted LLM keys and bare access
   variables) — aligned with the console access guide
   2026-11-20; 0.1.x legacy `AGENTARTS_*` names fall back — see the key configuration table in
   README);
3. **host configuration**: jiuwenswarm additionally needs `observability.agent.enabled: true`
   (default false — no spans produced) and `attribute_value_max_length: 65536` (measured: the
   production system prompt is 16K characters; the default 10240 truncates it);
4. **result**: real conversations (WS `chat.send`) produce complete production traces
   (an AGENT→invoke→task_iteration→react_iteration→LLM five-level tree, AGENT tokens = Σ LLM,
   and the 16420-character `system_instructions` plain text echoed back in full).

### 6.5.1 dev-stable branch compatibility verification (2026-11-17, live measurements: jiuwenswarm 0.2.3 dev-stable + openjiuwen 0.1.16 pin @f3f8f5)

Five differences from develop (jiuwenswarm 0.2.8 + openjiuwen 0.1.20) and countermeasures:

1. **The telemetry consumption entry changed**: dev-stable's `jiuwenswarm/telemetry/runtime.py`
   imports `init_observability` from the **top level** of
   `openjiuwen.agent_teams.observability` (in 0.1.16 that package is an independent definition
   forwarding to the shared extensions runtime), and the main path passes
   `tracer_provider_override` (mutually exclusive with `span_exporter_override`; passing it
   raises RuntimeError) → the template was upgraded to **dual-entry-point patching** (the two
   setup submodules, extensions + agent_teams) + **dual injection modes** (provider shape: after
   orig succeeds, append `SimpleSpanProcessor(SDK)` to the provider, idempotent per provider id +
   skip when the provider's main exporter is already this SDK, preventing double export;
   no-provider shape: pass the override in).
2. **Binding sweep (the key pitfall)**: importing `agent_teams.observability.setup` in the
   injection module itself first executes the whole teams package `__init__` → `rail.py` →
   `harness.observability` import chain, on which the `init_observability` /
   `init_shared_observability` symbols bind to the **original functions** before the patch
   takes effect — patching only the two setup submodules afterwards cannot save those
   references (the harness/chat path and the teams package top level consume the original
   functions → the provider loads the host's own configured file exporter and the SDK is
   bypassed entirely; measured, chat spans only landed in local `.trace/*.jsonl`).
   Countermeasure: after patching, scan the openjiuwen module attributes in `sys.modules` by
   the original function's **object identity** and rebind the stale references (measured: 3
   found — the teams package `__init__`, and `init_shared_observability` in the teams setup and
   the harness setup).
3. **fastmcp namespace collision**: openjiuwen 0.1.16 declares
   `Requires-Dist: fastmcp<3.0,>=2.14.2` → pip auto-installs official fastmcp 2.14.7, which
   shares the `fastmcp/` path with the jiuwen fork `fastmcp-slim==3.4.0` and they overwrite each
   other (slim's patched `auth.py` has the unique `PrivateKeyJWTClientAuthenticator`; the
   official version does not) → ImportError in chat processing. Fix:
   `pip install --force-reinstall --no-deps fastmcp-slim==3.4.0` from official PyPI; with slim
   owning `fastmcp/` exclusively, the service works. **This can recur on any dependency reinstall** — re-verify with:
   `python -c "from fastmcp.server.auth.auth import PrivateKeyJWTClientAuthenticator"`.
4. **cron storage/scheduling incompatibility**: develop writes
   `domain/agent/home/cron_jobs.json`; dev-stable's gateway cron uses an independent store
   (KV backend + lazy migration) and does not read the old job file; the CLI also lacks
   `--keep-worker` (the restart script must drop the argument). Rebuild jobs via WS
   `cron.job.create` (frame `{"type":"req","method":"cron.job.create","params":{"name":...,
   "cron_expr":"0 */10 * * * * *","description":...}}`); scheduling/triggering/backfill all
   work (`[Cron][run-trace]` full-chain logs).
5. **config schema rename**: the `observability` section split into `team_observability` /
   `agent_observability` (the latter defaults enabled=false but is auto-lifted by the
   trajectory data plane's demand — prewarm prints `[AgentObservability] enabled:
   exporter=file`); old configs without the new sections → defaults apply. After injection takes
   effect, the override takes priority over that section's exporter value, but truncation
   settings like `attribute_value_max_length` remain under host config control and need
   attention.
6. **split layout dual scheduler double-run** (measured 2026-11-17): `python -m
   jiuwenswarm.app` always starts agentserver + gateway (no standalone single-process branch).
   The agentserver-side agent tool build chain (triggered at prewarm:
   `cron_runtime.build_tools` → `ensure_scheduler_started` → `CronTools.ensure_scheduler`)
   starts an agent-side `CronSchedulerService`; the gateway starts one too at boot — the two
   **share the same cron store and each tick the same job** (each tick double-runs; run_id is
   the same `{job_id}:{ts}` on both): the gateway chain produces the real inspection run
   (cron_xxx session, complete answer), while the agent-side chain produces a weird
   default-session run with empty content (burning ~93k input tokens per round for nothing; the
   message template timestamp stays at the first tick's old value). The idempotency guard is
   in-process (`self._runs`) and useless across processes; the `enterprise_cron_enabled`
   pseudo-switch is unusable (`is_enterprise()` has too large a blast radius),
   `default_cron_enabled` has no consumer (dead code), and `ensure_scheduler` has no config
   switch at all. **Fix (minimal host patch)**: add an env guard at the top of `cron_tools.py
   ensure_scheduler()` — with `JIUWENSWARM_AGENT_CRON_DISABLE=1`, return None (cron tool
   reads/writes / WS `run_now` are unaffected; run_now is single-process single-trigger
   anyway); the host restart script exports the variable. Verification: after
   restart, the logs show only the gateway's single `[Cron] scheduler started`, and the 19:10
   tick produces a single platform trace. **This is a dev-stable host defect and should be
   reported to the jiuwenswarm maintainers** (under split layout the agent side should yield
   scheduling to the gateway by default, or provide a config switch). **Correction after deeper
   digging**: on the develop branch, `cron_tools.py ensure_scheduler` already did the "Phase 4
   single-source convergence" (always returns None; the docstring explicitly names the
   dual-scheduler double-trigger; the type is locked to `self._scheduler: None = None`) — the
   defect is "fixed upstream but not backported"; the correct action is to request a backport;
   our env patch is isomorphic to the upstream fix and forward-compatible (the develop version
   does not read the env, so it automatically becomes a harmless no-op after upgrade).
7. **jiuwenbox sandbox three-layer dependency breakage** (measured 2026-11-17: all TOOL spans
   409 `Cannot exec in sandbox: state is error`). Layer-by-layer root causes and fixes:
   ① missing system-level dependency **bwrap** (bubblewrap) — `exec of "bwrap" failed: No such
   file or directory` → `apt-get install -y bubblewrap` (an apt package, not in the pip
   dependency tree; environment reinstalls will recur); ② config.yaml
   `sandbox.jiuwenbox.policy_file: windows-policy.yaml` — **a Windows policy running on
   Linux**: that policy has no filesystem binds at all, the bwrap sandbox has no /usr, daemon
   `execvp python3: No such file or directory` → switched to `default-policy.yaml`
   (binds /bin /usr /lib /etc etc.); companion pitfall: `_is_std_cpython` probed by
   `JIUWENBOX_RUNNER_PYTHON` is a **Windows-only check** (looks for `python3*.dll`), always
   empty on Linux → the daemon command falls back to the bare `python3` name, entirely
   dependent on the policy's /usr bind visibility; ③ default-policy binds `/opt` but a minimal
   image may lack that directory — `bwrap: Can't find source path /opt` → `mkdir -p /opt`.
   Verification: `exited during startup` logs dropped to zero, TOOL spans `is_error=False`,
   bash returns the real uptime. **Debugging path**: in the host logs, grab the captured daemon
   stdout/stderr immediately following `Failed to create sandbox` — the error shape advances
   one layer after each fix; iterate layer by layer.

Verification results (live runs + platform echo-back, 2026-11-17): WS chat (`swarmlive-*`) →
platform trace AGENT→`task 1`→`react step N`→LLM; AGENT tokens = Σ LLM; input/output.messages
structured; `system_instructions` flattened to plain text; `deepagent.*` /
`context.window.commit` / `execution.subject.*` trimming all effective; cron (`0 */10`) first
fired at 11:30 → the platform trace is isomorphic (4 react rounds; AGENT tokens aggregated
across rounds: 120356). Side effects (harmless): each startup's prewarm session produces one
0-token AGENT stub span (session `__prewarm___session`); the chat tree has one level fewer than
the develop era — no `invoke_agent main_agent` (AGENT→`task 1` directly).

### 6.5.2 Known compatibility notes (2026-11-19)

Verified host combinations (recreated from scratch and verified end-to-end): jiuwenswarm 0.2.8
(develop) + openjiuwen 0.1.20, and jiuwenswarm 0.2.3 (dev-stable) + openjiuwen 0.1.16 pinned
at the exact dev-stable head commit `77b90c0b`. Durable conclusions:

- Pin every ecosystem dependency by exact commit: upstream repositories moved/were refactored
  within days, and a dev-branch head can carry the same version string as an earlier published
  tag yet contain different modules (version number ≠ content).
- All openjiuwen references across the dependency tree must resolve to one string-identical
  ref; a branch-name ref beside a commit ref makes pip raise ResolutionImpossible even at
  identical versions (fix: clone the conflicting package locally and point the dependency at
  the local copy).
- fastmcp-slim and official fastmcp share the `fastmcp/` path and overwrite each other on any
  openjiuwen (re)install; afterwards re-run `pip install --force-reinstall --no-deps -i
  https://pypi.org/simple fastmcp-slim==3.4.0`; slim's server component also needs `griffe`
  (absent from its dependency metadata).
- In headless environments use app mode (`python -m jiuwenswarm.start_services app`); web mode
  requires the built frontend dist; host WS client ports are recomputed at every boot and must
  not be hard-coded.
- End-to-end result with a working pin set: the .pth injection arms in every host process;
  real WS chat yields a complete 15-span ReAct tree and cron runs report an isomorphic
  12-span trace on the platform, with all aggregation/trimming/events assertions passing.

## 7. Semantic Contract (the SDK's documented promises)

1. **Coexistence, not replacement**: the override and the main exporter coexist as dual paths;
   only when the SDK is the first initializer is it a single path. Integration order determines
   topology; host integration docs must state the expected ordering.
2. **shutdown propagation chain**: `provider.shutdown()` → `SimpleSpanProcessor.shutdown()` →
   the SDK's `shutdown()` drains. The SDK takes no part in demand accounting; its lifecycle
   fully follows the shared provider.
3. **Hot-reload limitation**: as with the main stack, an exporter is built only once per
   process; config changes require a process restart.
4. **Backpressure semantics**: queue-full drops the oldest + warns; never blocks, never raises
   into business flow.
5. **Data-plane isolation**: the projection only affects the SDK's exported clones;
   `SpanRecordProcessor` / SQLite / trajectory UI / diagnostics always see full canonical data.

## 8. Test Plan

| Layer | Content |
|---|---|
| Unit | projection semantics (exact/prefix drop, truncate, rename, empty-config pass-through); agentarts preset assertions (`gen_ai.span.kind` derivation covering six span shapes, `total_tokens` backfill, `cache_write→cache_creation`, common-attribute injection, mandatory-field completeness checks); skill detection assertions (standdown check — skip inference without overwriting when `gen_ai.skill.*` / `skill.loaded` are already present, `SKILL.md` / `skills` directory-segment hits and misses, name extraction, `skill.loaded` event appended without modifying the original span's events, conservative multi-hit policy, custom `skill_path_patterns`); transform failure fallback; BufferedExporter enqueue/batched flush/backpressure drop/shutdown drain & idempotence/force_flush |
| Integration (agent-core) | `span_exporter_override` injection: harness + team dual tier; both orderings — provider built first then append vs. not; shutdown propagation; verify preset derivations with spans produced by the real OtelCallbackHandler (AGENT/LLM/TOOL/STEP, four kinds) |
| Production-pipeline e2e (agent-core) | `tests/integration/test_production_pipeline.py` (P1–P6, FakeModelClient monkeypatching `create_model_client` — no network, real event decoration chain): P1 full production-order chain (acquire gate → SDK append dual-path → registry run-span root → real LLM events → handler produces child spans forming a tree → per-span assertions on the platform mandatory contract → aggregation/rename/usage backfill → service_name rewrite); P2 bare-tracer root counterexample (§6.2.1 pitfall regression guard — child spans lost); P3 failure propagation (run span ERROR + status_message; measured: the failed LLM span's status is UNSET — still exported with the aggregation chain intact); P4 multi-turn trace isolation (no cross-trace aggregation leakage); P6 lifecycle wrap-up (shutdown exactly once; all spans flushed) |
| Integration (jiuwenswarm) | file main exporter + SDK dual-path e2e; confirm the SQLite trajectory data is unaffected by projection |
| Security/edges/reliability | `tests/test_security_and_edges.py` (21 cases): **security** — headers CRLF/control-character injection rejected at the config layer (`config.py` field_validator, a second line of defense beyond gRPC metadata rejecting uppercase keys), endpoint non-empty, drop-warning logs never contain span attribute values (assertions hooked directly on the handler — credentials/sensitive content never enter logs); **edges** — Windows backslash skill-path recognition (the main-battlefield shape), aggregator LRU eviction and take idempotence, five malformed input.messages shapes — neither crash nor backfill, Unicode truncation by character, 1000 spans in a single batch with zero loss, post-shutdown export FAILURE / slow-inner force_flush timeout False; **reliability** — 8 threads × 800 spans concurrently, no duplicates, no losses; explicit recovery after sustained inner failures (thread survives; batches keep arriving). Test design note: the background `_take_batch` takes whatever is available (it does not wait to fill batch_size), so **assertions must not depend on batch partitioning** — create determinism with a switch-style failure window or a slow inner exporter |
| Compatibility matrix | opentelemetry 1.28 / 1.42 / 1.45 — three environments running the full unit suite (CI three jobs). **Locally verified (2026-11-14)**: 1.28.0 and 1.42.0, each in an independent venv with the official family installed (api/proto/sdk/otlp exporter at the same version) + the SDK installed from source, then the pure-SDK test group run (unit + security/edge/reliability + protocol line, 78 cases) — **all passed**; the 1.45.2 main environment passed the full 90. The risk surfaces (`SpanExporter`/`ReadableSpan` interfaces, the official OTLP exporter constructor signature headers/timeout, proto encoding) show no differences across the three versions. CI jobs reuse the same venv flow, made permanent |
| Live (real AgentArts) | `tests/live/` (credential-env driven; the whole group skips when absent — zero impact on the regular suite). **L1 static full-chain comparison** (SDK standalone-provider shape, three-span nested tree → gRPC report → KooCLI polling echo-back asserting tree/types/tokens/in-out → ShowOpsTrace spot-checking skill fields; **platform child spans have an empty session_id, so session filtering only locates the root — the tree query takes two steps: session→trace_id→full filter**); **L2 auth negative** (wrong token → the official exporter returns FAILURE → the SDK's warning is visible — live validation of exporter.py's return-value check, which fixed the defect where the background thread swallowed return values and made lost batches invisible); **L3 routing negative** (empty service_name → gateway 504 → warning visible). **L4 production-chain live** (`tests/live/test_production_live.py`: agent-core production ordering + FakeModelClient (lazy dispatch confirms openai is not touched after the monkeypatch) → real gRPC → platform echo-back asserting cross-span aggregation/rename/usage backfill/tree; each test builds its own exporter instance — no session-scoped fixture reuse, because SimpleSpanProcessor.shutdown propagates and closes the exporter). **L5 real-LLM live** (`tests/live/test_real_llm_live.py`, cross-machine live check: within pytest, the real deepseek production ordering and report are executed on a remote test machine and asserted from the local machine via KooCLI — credentials on both sides are read from environment variables and never stored; the suite skips when they are absent). Assertions: AGENT+LLM form a tree; AGENT input cross-span aggregation non-empty; output fully equal to the real answer; platform token counts match the deepseek API response; the LLM span contains the real answer. **Executed live: 5/5 passed (2026-11-14, the live group fully green)** |

## 9. Risks and Legacy Items

| # | Risk/cost | Mitigation |
|---|---|---|
| 1 | clone/transform logic duplicated with agent-core's `transforming.py`; semantics may drift | Simple semantics (~100 lines); integration tests cover the fallback; can sink into a shared package long-term (an optimization, not a prerequisite) |
| 2 | `acquire_observability` does not pass the override through; a config.yaml first-class switch needs host glue | Treated as an optional iteration; the zero-change path (§6.2) goes first |
| 3 | On the SimpleSpanProcessor synchronous chain the SDK is the only buffering point; batch-implementation quality determines backpressure behavior | Bounded queue + drop-and-warn contract + dedicated unit tests |
| 4 | Subtle differences of the official OTLP exporter constructor signature across 1.25–1.45 (e.g. headers/timeout parameters) | Compatibility-matrix CI, verified on three versions |
| 5 | Some key-name mappings of the Huawei spec are inferred from documentation (`is_stream`, `tool.call.id`, the `user.id` derivation source), and skill detection is heuristic (the path-parameter key set, `skills` directory-segment naming) — to be checked during implementation against real spans + AgentArts live reporting (items marked in §4.3.2/§4.3.5) | Do one real reporting verification during implementation (an agent run including one real SKILL.md read), comparing against the platform dashboard echo-back; inferred items and `skill_path_patterns` are centralized as constant tables in code for easy correction |
| 6 | The platform spec "will keep evolving with the community"; the mapping rules risk drifting | Preset rules centralized in `projection.py` constant tables + source annotated per spec doc version (2026-11-02); upgrades go through SDK version releases |
| 7 | ~~Queue-full drops logged a warning per span — log storm under burst loads~~ **Fixed**: switched to a "locating-essential" mode — the first drop warns immediately (1 line) + a summary report at force_flush/shutdown (new/cumulative), silent counting during normal operation; the contract is locked by unit tests (N dropped ≤ 4 warning lines; the summary contains cumulative counts; warnings contain no span content) | — |
| 8 | ~~otel 1.28/1.42 compatibility matrix unverified~~ **Verified**: 1.28.0 / 1.42.0, 78 cases all passed in independent venvs (no version differences in the SDK interfaces or the official exporter constructor signature); the 1.45.2 main environment passed all 89; CI jobs reuse the same venv flow | — |

## 10. Implementation Steps

1. Repository skeleton (pyproject + src layout + CI);
2. `config.py` / `projection.py` / `transform.py` + unit tests;
3. `exporter.py` (BufferedOtlpExporter) + `factory.py` + unit tests;
4. agent-core integration tests (dual tier, dual orderings);
5. jiuwenswarm zero-change integration verification (e2e);
6. (optional iteration) jiuwenswarm config.yaml first-class switch glue.

## Appendix A: Key Code Basis

| Fact | Location (agent-core) |
|---|---|
| `init_observability` supports `span_exporter_override` | `openjiuwen/extensions/observability/setup.py:21-33` |
| When the provider already exists, append `SimpleSpanProcessor(override)` | `openjiuwen/extensions/observability/runtime.py:134-142` |
| override replaces the factory product / goes through SimpleSpanProcessor | `openjiuwen/extensions/observability/runtime.py:158-162` |
| Team side passes through with the same signature | `openjiuwen/agent_teams/observability/setup.py:42-47` |
| The demand wrapper does not pass the override through | `openjiuwen/harness/observability/setup.py:72`; `openjiuwen/agent_teams/observability/setup.py:87` |
| Exporter factory and the existing five types | `openjiuwen/extensions/observability/runtime.py:408-443` |
| TransformingSpanExporter semantics reference | `openjiuwen/extensions/observability/exporters/transforming.py` |
| jiuwenswarm → ObservabilityConfig mapping | `jiuwenswarm/agents/harness/observability_runtime.py:23-56` |
| GenAI standard key generation and vocabulary | `openjiuwen/extensions/observability/gen_ai_semconv.py` (generated at pinned revision); `semconv.py` (`openjiuwen.*` extension keys) |
| run root span attributes (trace.root / record.kind / turn.number / agent.mode) | `openjiuwen/harness/observability/run_span.py:69-169` |

## Appendix B: External Spec References

| Reference | Description |
|---|---|
| <https://support.huaweicloud.com/ops-agentarts/agentarts_14_0127.html> | Huawei Cloud AgentArts "OpenTelemetry Protocol Field Mapping" (the spec source of this document's §4.3 preset, version 2026-11-02): span types (table 1), common attributes (table 2), per-type fields for AGENT/LLM/TOOL/STEP/WORKFLOW/MEMORY (tables 3-8), metric indicators and dimensions (tables 9-11) |
| <https://support.huaweicloud.com/ops-agentarts/agentarts_14_0125.html> | "Report Trace Data" hands-on (endpoint / auth access details; implementation-time reference) |
| <https://support.huaweicloud.com/ops-agentarts/agentarts_14_0126.html> | "Report Metric Data" hands-on (reference for the later metric iteration) |
| <https://opentelemetry.io/docs/specs/semconv/gen-ai/> | OTel GenAI semantic conventions (the upstream reference of the Huawei spec) |
