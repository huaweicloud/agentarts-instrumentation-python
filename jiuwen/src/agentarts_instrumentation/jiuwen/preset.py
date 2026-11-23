# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The ``agentarts`` projection derivation: openjiuwen data contract →
Huawei AgentArts semantics (design.md §4.3).

Runs on a clone in the export path (see the core projection pipeline)
and never mutates the original span; local consumers such as
SpanRecordProcessor / the SQLite trajectory store keep receiving full
canonical data.

The derivation follows the Huawei Cloud doc "OpenTelemetry Protocol
Field Mapping" (version 2026-11-02,
https://support.huaweicloud.com/ops-agentarts/agentarts_14_0127.html)
and is based on the attribute-key data contract (``openjiuwen.*``
attribute values and span naming shapes) without importing openjiuwen.
Order (within the generic pipeline): attributes injection (core) →
derivation here (incl. framework namespace trimming, an events
whitelist and link clearing) → drop → rename → truncate; an inactive
config is an identity pass-through.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from agentarts_instrumentation.config import Projection
from agentarts_instrumentation.projection import DerivationOutcome, drop_attributes
from agentarts_instrumentation.semconv import (
    K_AGENT_NAME,
    K_CACHE_WRITE,
    K_ERROR_RECOVERED,
    K_INPUT_MESSAGES,
    K_OPERATION_NAME,
    K_OUTPUT_MESSAGES,
    K_REACT_ROUND,
    K_REQUEST_STREAM,
    K_SKILL_ID,
    K_SKILL_NAME,
    K_SPAN_KIND,
    K_SYSTEM_INSTRUCTIONS,
    K_TOOL_CALL_ARGUMENTS,
    K_TOOL_NAME,
    K_TTFT,
    K_USAGE_CACHE_CREATION,
    K_USAGE_CACHE_READ,
    K_USAGE_INPUT,
    K_USAGE_OUTPUT,
    K_USAGE_TOTAL,
)
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger(__name__)

# ---- openjiuwen data contract keys (values are read only; no code
# dependency is created) ----
K_TRACE_ROOT = "openjiuwen.trace.root"
K_RECORD_KIND = "openjiuwen.trajectory.record.kind"
K_AGENT_MODE = "openjiuwen.agent.mode"
K_TURN_NUMBER = "openjiuwen.turn.number"
K_OJ_TTFT_MS = "openjiuwen.gen_ai.response.time_to_first_token_ms"
K_OJ_SPAN_OUTPUT = "openjiuwen.span.output"
#: Langfuse mirror key: openjiuwen 0.1.16 run spans do not write
#: ``span.output``; the final answer only lives in that key's JSON
#: wrapper (extracted during derivation; see ``_extract_langfuse_output``).
K_LANGFUSE_OUTPUT = "langfuse.observation.output"

LLM_OPERATIONS = frozenset({"chat", "text_completion", "generate_content"})

#: Preset built-in key-name fixes (design.md §4.3.2): source → target;
#: existing target keys are never overwritten. ``openjiuwen.span.output``
#: normalization is NOT in this table (needs value wrapping; see
#: ``_wrap_output_messages``) — AGENT/LLM input/output stay structured
#: messages, matching platform table 3 examples.
AGENTARTS_RENAME: dict[str, str] = {
    K_CACHE_WRITE: "gen_ai.usage.cache_creation.input_tokens",
    K_REQUEST_STREAM: "gen_ai.request.is_stream",
    K_OJ_TTFT_MS: K_TTFT,
}

SKILL_LOADED_EVENT = "skill.loaded"

#: Trajectory-protocol spans dropped by the preset (design.md §4.3.6):
#: AgentArts has no matching span type, and window payloads on long
#: sessions reach tens of KB — the whole span is not exported
#: (centralized here for easy correction).
AGENTARTS_DROP_SPANS: frozenset[str] = frozenset(
    {
        # context-window snapshot chain (the local trajectory store keeps full data)
        "context.window.commit",
        # Reasoning sub-span: the reasoning text is already carried in
        # the parent LLM span's output.messages (role=reasoning); the
        # platform has no matching span type, so exporting it standalone
        # would add degenerate pseudo-LLM nodes (blank model_name, zero
        # tokens) to the trace tree (local trajectory store unaffected).
        "llm.reasoning",
    }
)

#: Redundant attributes trimmed by the preset (design.md §4.3.6,
#: centralized for easy correction).
#: **Whole-namespace trimming** — it happens after derivation (kind
#: derivation / display-name rewrite / token & input aggregation /
#: output wrapping / TTFT rename have all consumed these keys), so the
#: exported clone keeps only platform semantics (``gen_ai.*`` / OTel
#: standard) plus user-injected attributes; the local trajectory store
#: is unaffected (projection only touches export clones).
AGENTARTS_DROP_ATTRS: tuple[str, ...] = (
    # Framework data-contract namespace: trace.root / agent.mode /
    # record.kind (derivation inputs), span.output (output wrapping
    # source) and TTFT (rename source) were consumed during derivation;
    # session.id etc. duplicate gen_ai.conversation.id / session.id.
    "openjiuwen.*",
    # Langfuse mirror: observation.input/output/type duplicate gen_ai.*;
    # the 0.1.16 AGENT final answer was already extracted from
    # observation.output during derivation.
    "langfuse.*",
    # team session ids etc.: duplicates of session.id /
    # gen_ai.conversation.id in single-agent scenarios.
    "agentteam.*",
    # jiuwenswarm executor internal debug instrumentation: iteration
    # duplicates the display name; loop_event holds dirty repr values
    # of unserialized Python objects.
    "deepagent.*",
    # TOOL duplicate values (measured 2026-11-17): gen_ai.tool.input is
    # a [args list + metadata] wrapper (contains session id) and
    # gen_ai.tool.output is identical to gen_ai.tool.call.result — the
    # platform semantic keys call.arguments / call.result already carry
    # them.
    "gen_ai.tool.input",
    "gen_ai.tool.output",
)


class _TurnInputAggregator:
    """Cross-span turn-input aggregation (bounded, thread-safe).

    Context: on the production path the run root span does not write
    gen_ai.input.messages (no project-side counterpart key); within a
    trace, LLM spans end before the AGENT root and their
    input.messages' first user message is the turn input. The
    derivation records it on the LLM span (first-write-wins; the
    historical first user message is identical across multi-turn LLM
    spans) and consumes it when projecting the AGENT root — zero host
    changes. Exact for single-root traces; multi-root traces only
    backfill the first root to arrive (see design.md).
    """

    def __init__(self, capacity: int = 1024) -> None:
        self._lock = threading.Lock()
        self._cache: OrderedDict[int, str] = OrderedDict()
        self._capacity = capacity

    def record(self, trace_id: int, input_text: str) -> None:
        with self._lock:
            if trace_id in self._cache:
                return
            self._cache[trace_id] = input_text
            if len(self._cache) > self._capacity:
                self._cache.popitem(last=False)

    def take(self, trace_id: int) -> str | None:
        with self._lock:
            return self._cache.pop(trace_id, None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


_input_aggregator = _TurnInputAggregator()


class _TraceTokenAggregator:
    """Cross-span token usage accumulation (bounded, thread-safe).

    Context: the platform requires token usage on the AGENT span too,
    but on the production path the run root span does not write
    ``gen_ai.usage.*`` (tokens live on LLM spans); LLM spans end before
    the AGENT root within a trace. The derivation accumulates
    input/output tokens on LLM spans and consumes the sum when
    projecting the AGENT root — zero host changes. Host-written usage
    is never overwritten; nothing is written when no LLM span hit.
    Multi-root traces only backfill the first root to arrive (same
    limit as ``_TurnInputAggregator``; see design.md).
    """

    def __init__(self, capacity: int = 1024) -> None:
        self._lock = threading.Lock()
        self._cache: OrderedDict[int, dict[str, int]] = OrderedDict()
        self._capacity = capacity

    def add(
        self,
        trace_id: int,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
    ) -> None:
        if (
            input_tokens is None
            and output_tokens is None
            and cache_read_tokens is None
            and cache_creation_tokens is None
        ):
            return
        with self._lock:
            entry = self._cache.get(trace_id)
            if entry is None:
                entry = {
                    "input": 0,
                    "output": 0,
                    "cache_read": 0,
                    "cache_creation": 0,
                }
                self._cache[trace_id] = entry
            if input_tokens:
                entry["input"] += input_tokens
            if output_tokens:
                entry["output"] += output_tokens
            if cache_read_tokens:
                entry["cache_read"] += cache_read_tokens
            if cache_creation_tokens:
                entry["cache_creation"] += cache_creation_tokens
            if len(self._cache) > self._capacity:
                self._cache.popitem(last=False)

    def take(self, trace_id: int) -> dict[str, int] | None:
        with self._lock:
            return self._cache.pop(trace_id, None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


_token_aggregator = _TraceTokenAggregator()


class _TraceErrorAggregator:
    """Cross-span child failure registry (bounded, thread-safe) —
    AGENT root status propagation (§4.3.9).

    Context: the host only calls ``set_status(ERROR)`` on failed
    business child spans (LLM call errors, tool-reported failures); the
    AGENT root does not inherit. **Policy (plan B revision 2026-11-21,
    verified on trace c2acd7**[redacted]**)**: only terminal failures
    escalate the root — a successful business child span after the last
    failed one (the host's success path explicitly calls
    ``set_status(OK)``: tool callback L864, LLM L1199) counts as
    self-healed recovery; the root then reflects run completion and is
    not escalated (the attribute ``gen_ai.error.recovered`` leaves a
    trace). Spans with status=None (STEP etc.) are not recovery signals
    (a post-tool-failure turn wrap-up must not be misread as recovery).
    An already-ERROR root is never overwritten; internal bookkeeping
    spans were already filtered by DROP_SPANS before registration.
    """

    def __init__(self, capacity: int = 1024) -> None:
        self._lock = threading.Lock()
        # trace_id -> [last failed child span name, recovered flag]
        self._cache: OrderedDict[int, list] = OrderedDict()
        self._capacity = capacity

    def record(self, trace_id: int, span_name: str, *, is_error: bool) -> None:
        with self._lock:
            if is_error:
                # A failure resets the recovery flag: terminality means
                # "no successful business span after the last failure".
                self._cache[trace_id] = [span_name, False]
            else:
                entry = self._cache.get(trace_id)
                if entry is not None:
                    entry[1] = True
            if len(self._cache) > self._capacity:
                self._cache.popitem(last=False)

    def take(self, trace_id: int) -> list | None:
        with self._lock:
            return self._cache.pop(trace_id, None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


_error_aggregator = _TraceErrorAggregator()


class _ParentRefRegistry:
    """Cross-span parent reference tracking (bounded, thread-safe) —
    task_iteration structure detection.

    The ``task_iteration`` span plays structurally different roles in
    two host generations (measured):

    - **Container** (openjiuwen 0.1.20 / develop): react rounds hang
      below it (child spans' parent_span_id point at it) — dropping it
      would orphan the react rounds and break the platform tree; must
      keep;
    - **Dangling marker** (openjiuwen 0.1.16 / dev-stable): react
      rounds hang directly on the AGENT root; the span has no child
      references and no semantic attributes (blank platform span type)
      — pure noise, not exported.

    OTel guarantees child spans end before their parent, and projection
    processes spans in end order — each child span records its parent
    span id at projection time, and task_iteration checks whether its
    own span id was referenced: referenced = container → keep;
    unreferenced = dangling marker → drop the whole span. Overflow
    evicts the oldest reference FIFO (capacity far exceeds any single
    trace).
    """

    def __init__(self, capacity: int = 4096) -> None:
        self._lock = threading.Lock()
        self._refs: OrderedDict[int, None] = OrderedDict()
        self._capacity = capacity

    def record(self, parent_span_id: int) -> None:
        with self._lock:
            self._refs[parent_span_id] = None
            if len(self._refs) > self._capacity:
                self._refs.popitem(last=False)

    def contains(self, span_id: int) -> bool:
        with self._lock:
            return span_id in self._refs

    def clear(self) -> None:
        with self._lock:
            self._refs.clear()


_parent_refs = _ParentRefRegistry()


class _TraceRoundCounter:
    """React round renumbering per trace from 1 (bounded, thread-safe).

    The host's ``react_iteration.N`` counter may not start from 1 in
    session-resume / breakpoint-recovery scenarios (residue across
    invokes); the platform (table 6) expects rounds to "usually
    increase from 1". Projection order = end order, and the host's
    ReAct steps serially (the next step opens only after the previous
    one closes), so counting in projection order yields 1..N. The
    AGENT root releases that trace's counter when projected last.
    """

    def __init__(self, capacity: int = 4096) -> None:
        self._lock = threading.Lock()
        self._counts: OrderedDict[int, int] = OrderedDict()
        self._capacity = capacity

    def next(self, trace_id: int) -> int:
        with self._lock:
            value = self._counts.get(trace_id, 0) + 1
            self._counts[trace_id] = value
            if len(self._counts) > self._capacity:
                self._counts.popitem(last=False)
            return value

    def release(self, trace_id: int) -> None:
        """Release one trace's counter (called when projecting the
        AGENT root)."""
        with self._lock:
            self._counts.pop(trace_id, None)

    def clear(self) -> None:
        """Clear everything (test isolation)."""
        with self._lock:
            self._counts.clear()


_round_counter = _TraceRoundCounter()


def _extract_turn_input(raw: Any) -> str | None:
    """Extract the turn input and wrap it as platform-structured
    messages (consistent with LLM spans).

    The LLM span's ``gen_ai.input.messages`` is ``[{"role","parts"}]``
    JSON; the AGENT root consumes this function's result under the same
    key (table 3 examples use exactly this shape).

    User message selection (revised 2026-11-19, measured on resumed
    sessions):

    - Skip messages starting with ``<system-reminder>`` — the host
      injects dynamic runtime context as user-role messages at the head
      of the input, so taking "the first user message" would capture
      injected context;
    - Take the **last** user message with non-empty text — in resumed
      sessions the first LLM span carries the full history and the new
      turn input sits at the end;
    - jiuwenswarm wraps the real input in a
      ``你收到一条消息：{json}`` envelope (``origin_kind=
      external_user_authored``; the ``content`` field holds the raw
      text); unwrap to the plain ``content`` when detected.

    Non-JSON plain text is wrapped as a single user message (previous
    behavior unchanged).
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if not text.startswith("["):
        return json.dumps(
            [{"role": "user", "parts": [{"type": "text", "content": text[:2000]}]}],
            ensure_ascii=False,
        )
    try:
        items = json.loads(text)
    except ValueError:
        return json.dumps(
            [{"role": "user", "parts": [{"type": "text", "content": text[:2000]}]}],
            ensure_ascii=False,
        )
    if not isinstance(items, list):
        return None
    # Scan backwards for the first "real" user input (skipping
    # system-injected system-reminder messages).
    for item in reversed(items):
        if not isinstance(item, Mapping) or item.get("role") != "user":
            continue
        message = {k: v for k, v in item.items() if k in ("role", "parts", "content")}
        if not message.get("parts") and isinstance(message.get("content"), str):
            message["parts"] = [{"type": "text", "content": message.pop("content")}]
        parts = message.get("parts")
        # Must carry non-empty text content; malformed entries with
        # missing/empty content are treated as absent (B3).
        content = next(
            (
                part["content"]
                for part in (parts if isinstance(parts, list) else ())
                if isinstance(part, Mapping)
                and isinstance(part.get("content"), str)
                and part["content"]
                and not part["content"].lstrip().startswith(_SYSTEM_REMINDER_PREFIX)
            ),
            None,
        )
        if content is None:
            continue
        content = _unwrap_message_envelope(content)
        return json.dumps(
            [{"role": "user", "parts": [{"type": "text", "content": content}]}],
            ensure_ascii=False,
        )
    return None


#: Prefix of the host's dynamic context injection — user-role but not
#: user intent (declared at the top of the message).
_SYSTEM_REMINDER_PREFIX = "<system-reminder>"
#: jiuwenswarm user-input envelope prefix; the ``{json}`` body's
#: ``content`` field holds the raw text; ``origin_kind=
#: external_user_authored`` marks real user messages.
_MESSAGE_ENVELOPE_PREFIX = "你收到一条消息："


def _unwrap_message_envelope(content: str) -> str:
    """Unwrap jiuwenswarm's ``你收到一条消息：{json}`` envelope to the
    raw ``content`` text.

    Non-envelope content is returned as-is; so is an envelope with
    broken JSON (an unwrap failure must not lose the input).
    """
    if not content.lstrip().startswith(_MESSAGE_ENVELOPE_PREFIX):
        return content
    body = content.lstrip()[len(_MESSAGE_ENVELOPE_PREFIX) :].strip()
    try:
        payload = json.loads(body)
    except ValueError:
        return content
    if (
        isinstance(payload, Mapping)
        and isinstance(payload.get("content"), str)
        and payload["content"]
    ):
        return payload["content"]
    return content


def _wrap_output_messages(raw: Any) -> str | None:
    """Wrap a plain-text AGENT answer as platform-structured messages
    (consistent with LLM spans).

    The host's ``openjiuwen.span.output`` is plain text
    (``stamp_run_output``); already-structured ``[{...}]`` lists pass
    through unchanged.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
            if isinstance(items, list):
                return text
        except ValueError:
            pass
    return json.dumps(
        [{"role": "assistant", "parts": [{"type": "text", "content": text}]}],
        ensure_ascii=False,
    )


def _extract_langfuse_output(raw: Any) -> Any:
    """Extract the final answer text from the dict wrapper of
    ``langfuse.observation.output``.

    openjiuwen 0.1.16 run spans do not write
    ``openjiuwen.span.output``; the final answer only lives in
    ``langfuse.observation.output``. Measured (via cron debug output
    capture) there are **two host writer shapes**, neither strict JSON:

    - Shape A: double quotes + bare newline control characters
      (f-string concatenation) — ``json.loads(strict=False)`` parses;
    - Shape B: **single-quoted Python dict repr** (product of
      ``str(dict)``), with literal ``\\n`` escape sequences — JSON
      always fails; requires ``ast.literal_eval``.

    Both parsers failing / non-dict / non-string ``output`` → None
    (treated as missing — better absent than dirty;
    ``literal_eval`` has no code-execution risk).
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if not text.startswith("{"):
        return None
    payload: Any = None
    try:
        payload = json.loads(text, strict=False)
    except ValueError:
        try:
            payload = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return None
    if not isinstance(payload, Mapping):
        return None
    output = payload.get("output")
    if isinstance(output, str) and output.strip():
        return output
    return None


_REACT_ITERATION = re.compile(r"react_iteration\.(\d+)$")

#: task_iteration span name (container vs dangling-marker structure
#: detection: see _ParentRefRegistry)
_TASK_ITERATION = re.compile(r"^agent\..+\.task_iteration\.\d+$")


def _flatten_system_instructions(raw: Any) -> str | None:
    """Flatten parts-array system instructions into plain text.

    The agent-core production path writes ``[{"type": "text",
    "content": ...}]`` (message-parts structure); the official
    AgentArts probe reports ``gen_ai.system_instructions`` as a **plain
    string** (measured trace contract), and the console renders it as
    plain text. Returns None for plain text or unparseable input (keep
    the original value).
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if not text.startswith("["):
        return None
    try:
        items = json.loads(text)
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    chunks = [
        item["content"]
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("content"), str)
    ]
    if not chunks:
        return None
    return "".join(chunks)


def _as_int(value: Any) -> int | None:
    """Best-effort int parsing; dirty values (None/''/'N/A' etc.) are
    treated as missing."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _derive_agentarts_kind(name: str, attrs: Mapping[str, Any]) -> str | None:
    """Derive gen_ai.span.kind (design.md §4.3.1; priority
    AGENT>TOOL>STEP>LLM>WORKFLOW).

    Returns None when nothing matches — prefer missing over mislabeled.
    """
    if attrs.get(K_TRACE_ROOT) is True or attrs.get(K_RECORD_KIND) == "turn":
        return "AGENT"
    if (
        K_TOOL_NAME in attrs
        or attrs.get(K_OPERATION_NAME) == "execute_tool"
        or attrs.get(K_RECORD_KIND) == "tool"
    ):
        return "TOOL"
    if attrs.get(K_RECORD_KIND) == "step" or _REACT_ITERATION.search(name):
        return "STEP"
    if attrs.get(K_RECORD_KIND) == "inference" or attrs.get(K_OPERATION_NAME) in LLM_OPERATIONS:
        return "LLM"
    if name.startswith("team.") or name.startswith("task."):
        return "WORKFLOW"
    return None


def _rewrite_agent_span_name(name: str, attrs: Mapping[str, Any], kind: str | None) -> str:
    """Compress agent.* display names (design.md §4.3.8; rewrites the
    projected clone only, never the original span).

    Host naming patterns (jiuwenswarm/agent-core) → target display
    names:

    - AGENT root: ``agent.{mode}.{session_id}`` →
      ``invoke_agent openjiuwen`` (session suffix has no display value;
      mode is unified under the product name);
    - invoke: ``agent.{type}.invoke`` → ``{operation.name} {type}``;
    - task_iteration: ``agent.{type}.task_iteration.N`` → ``task N``;
    - react_iteration: ``agent.{type}.react_iteration.N`` →
      ``react step N`` (the round number travels via the
      ``gen_ai.react.round`` attribute, §4.3.1);
    - TOOL: ``tool.{name}`` → ``execute_tool {name}`` (unifies the
      0.1.16 dot template with the 0.1.20 space template; native
      0.1.20 names already starting with ``execute_tool`` are kept).

    Not enough segments → untouched; prefer a long name over an empty
    display name.
    """
    if kind == "TOOL" and name.startswith("tool.") and not name.startswith("tool. "):
        tool_name = name[len("tool.") :]
        if tool_name:
            return f"execute_tool {tool_name}"
    if not name.startswith("agent."):
        return name
    segments = name.split(".")
    if len(segments) < 3:
        return name
    op = str(attrs.get(K_OPERATION_NAME) or "").strip()
    if kind == "AGENT" or attrs.get(K_TRACE_ROOT) is True:
        return f"{op or 'invoke_agent'} openjiuwen"
    if len(segments) == 3 and segments[2] == "invoke":
        return f"{op or 'invoke'} {segments[1]}"
    if len(segments) == 4 and segments[2] == "task_iteration":
        return f"task {segments[3]}"
    if len(segments) == 4 and segments[2] == "react_iteration":
        # Display name without the round suffix (user request
        # 2026-11-21): the round only travels via the
        # gen_ai.react.round attribute; the platform tree tells rounds
        # apart by that attribute.
        return "react step"
    return name


def _apply_agentarts(span: ReadableSpan, name: str, attrs: dict[str, Any]) -> None:
    """Apply the AgentArts preset's derivations and built-in key-name
    fixes (mutates the projected clone in place)."""
    kind = attrs.get(K_SPAN_KIND) or _derive_agentarts_kind(name, attrs)
    if kind:
        attrs[K_SPAN_KIND] = kind
    # Flatten system_instructions to plain text (official probe
    # contract, §4.3.2)
    if K_SYSTEM_INSTRUCTIONS in attrs:
        flattened = _flatten_system_instructions(attrs.get(K_SYSTEM_INSTRUCTIONS))
        if flattened is not None:
            attrs[K_SYSTEM_INSTRUCTIONS] = flattened
    trace_id = span.get_span_context().trace_id
    if kind == "LLM":
        turn_input = _extract_turn_input(attrs.get(K_INPUT_MESSAGES))
        if turn_input:
            _input_aggregator.record(trace_id, turn_input)
        _token_aggregator.add(
            trace_id,
            _as_int(attrs.get(K_USAGE_INPUT)),
            _as_int(attrs.get(K_USAGE_OUTPUT)),
            _as_int(attrs.get(K_USAGE_CACHE_READ)),
            _as_int(attrs.get(K_USAGE_CACHE_CREATION)),
        )
    elif kind == "AGENT":
        if K_INPUT_MESSAGES not in attrs:
            turn_input = _input_aggregator.take(trace_id)
            if turn_input:
                attrs[K_INPUT_MESSAGES] = turn_input
        # Token usage: consume the sums accumulated from this trace's
        # LLM spans (LLM ends first and is projected first; the AGENT
        # root ends last and is projected last). Host-written values
        # are never overwritten. cache_read/cache_creation are also
        # aggregated (user request 2026-11-21; the host only writes
        # them on LLM spans).
        tokens = _token_aggregator.take(trace_id)
        if tokens is not None:
            if K_USAGE_INPUT not in attrs:
                attrs[K_USAGE_INPUT] = tokens["input"]
            if K_USAGE_OUTPUT not in attrs:
                attrs[K_USAGE_OUTPUT] = tokens["output"]
            if K_USAGE_CACHE_READ not in attrs:
                attrs[K_USAGE_CACHE_READ] = tokens["cache_read"]
            if K_USAGE_CACHE_CREATION not in attrs:
                attrs[K_USAGE_CACHE_CREATION] = tokens["cache_creation"]
        # Trace wrap-up: release this trace's react round counter (the
        # bounded registry is the safety net; the normal path releases
        # right at root projection).
        _round_counter.release(trace_id)
    if kind == "AGENT" and K_AGENT_NAME not in attrs:
        # Agent name falls back to openjiuwen.agent.mode; skipped when
        # missing (can be supplied via Projection.attributes) — never
        # guessed.
        mode = attrs.get(K_AGENT_MODE)
        if mode not in (None, ""):
            attrs[K_AGENT_NAME] = str(mode)
    if kind in ("AGENT", "LLM") and K_USAGE_TOTAL not in attrs:
        input_tokens = _as_int(attrs.get(K_USAGE_INPUT))
        output_tokens = _as_int(attrs.get(K_USAGE_OUTPUT))
        if input_tokens is not None or output_tokens is not None:
            attrs[K_USAGE_TOTAL] = (input_tokens or 0) + (output_tokens or 0)
    if kind == "STEP":
        if _REACT_ITERATION.search(name):
            # React rounds renumbered from 1 per trace (table 6:
            # "usually increase from 1"): the host counter may not
            # start at 1 across session resume / breakpoint recovery.
            # The display-name rewrite consumes the same value, keeping
            # span name and round attribute consistent.
            attrs[K_REACT_ROUND] = _round_counter.next(trace_id)
        elif K_REACT_ROUND not in attrs:
            round_number = _as_int(attrs.get(K_TURN_NUMBER))
            if round_number is not None:
                attrs[K_REACT_ROUND] = round_number
    # Built-in key-name fixes (preset stage, before user drop/rename
    # and namespace trimming).
    # AGENT final answer → structured output.messages (table 3
    # example; structured values pass through, existing targets are
    # not overwritten). Two-generation contract for the source value:
    # - openjiuwen 0.1.20: openjiuwen.span.output plain text (wrapped,
    #   then the source key is popped);
    # - openjiuwen 0.1.16: run spans don't write span.output; the
    #   final answer only exists in langfuse.observation.output's JSON
    #   wrapper (extracted here; the original key is later removed by
    #   namespace trimming).
    if K_OUTPUT_MESSAGES not in attrs:
        candidate = attrs.pop(K_OJ_SPAN_OUTPUT, None)
        wrapped = _wrap_output_messages(candidate) if candidate is not None else None
        if wrapped is None and kind == "AGENT":
            # span.output missing/unwrappable → fall back to the
            # langfuse mirror (0.1.16 contract)
            extracted = _extract_langfuse_output(attrs.get(K_LANGFUSE_OUTPUT))
            if extracted is not None:
                wrapped = _wrap_output_messages(extracted)
        if wrapped is not None:
            attrs[K_OUTPUT_MESSAGES] = wrapped
    for src, dst in AGENTARTS_RENAME.items():
        if src in attrs and dst not in attrs:
            attrs[dst] = attrs.pop(src)


def _iter_string_values(value: Any):
    """Recursively yield candidate path strings (tool arguments may be
    JSON strings / mappings / nested lists)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _iter_string_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_string_values(item)


def _match_skill_path(
    path: str,
    patterns: tuple[str, ...] | list[str],
) -> tuple[str, str, bool] | None:
    """Match skill document reads by path features.

    Returns ``(skill_name, matched_path, is_skill_doc_read)`` or None
    when nothing matches. Patterns ending in ``.md`` match by file
    name (skill name taken from the parent directory); others match by
    directory segment (skill name is the next segment; a file segment
    contributes its stem).
    """
    segments = [segment for segment in re.split(r"[\\/]+", path.strip()) if segment]
    if not segments:
        return None
    for pattern in patterns:
        lowered = pattern.lower()
        if lowered.endswith(".md"):
            for index, segment in enumerate(segments):
                if segment.lower() == lowered:
                    skill_name = segments[index - 1] if index > 0 else None
                    if skill_name:
                        return skill_name, path, True
        else:
            for index, segment in enumerate(segments):
                if segment.lower() == lowered and index + 1 < len(segments):
                    skill_name = segments[index + 1]
                    if "." in skill_name:
                        skill_name = skill_name.rsplit(".", 1)[0]
                    if skill_name:
                        return skill_name, path, False
    return None


def _apply_skill(
    span: ReadableSpan,
    attrs: dict[str, Any],
    patterns: tuple[str, ...] | list[str],
) -> Event | None:
    """Skill detection (design.md §4.3.5): existing information wins;
    path inference is only a fallback.

    Standdown rule: when the span already carries ``gen_ai.skill.name``
    or a ``skill.loaded`` event, trust what's there and skip path
    matching — no existing key is ever overwritten. Returns the Event
    to append (the caller builds the new events list and never mutates
    the original span), or None.
    """
    has_skill_name = K_SKILL_NAME in attrs
    has_loaded_event = any(
        getattr(event, "name", "") == SKILL_LOADED_EVENT for event in span.events
    )
    if has_skill_name and has_loaded_event:
        return None

    match: tuple[str, str, bool] | None = None
    if not has_skill_name:
        raw = attrs.get(K_TOOL_CALL_ARGUMENTS)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                pass
        if raw is not None:
            hits: list[tuple[str, str, bool]] = []
            for candidate in _iter_string_values(raw):
                hit = _match_skill_path(candidate, patterns)
                if hit:
                    hits.append(hit)
                    if len(hits) >= 4:
                        break
            if hits:
                skill_names = {hit[0] for hit in hits}
                if len(skill_names) > 1:
                    logger.warning(
                        "tool span %s matched multiple distinct skills (%s); "
                        "taking the first one %s",
                        span.name,
                        ", ".join(sorted(skill_names)),
                        hits[0][0],
                    )
                match = hits[0]

    if not has_skill_name and match is not None:
        skill_name = match[0]
        attrs[K_SKILL_NAME] = skill_name
        if K_SKILL_ID not in attrs:
            attrs[K_SKILL_ID] = skill_name

    if match is not None and match[2] and not has_loaded_event:
        return Event(
            SKILL_LOADED_EVENT,
            {"skill.name": match[0], "skill.path": match[1]},
            time.time_ns(),
        )
    # Standdown case: still backfill a missing gen_ai.skill.id (never
    # overwriting existing keys).
    if K_SKILL_NAME in attrs and K_SKILL_ID not in attrs:
        attrs[K_SKILL_ID] = str(attrs[K_SKILL_NAME])
    return None


def derive_agentarts(
    span: ReadableSpan,
    attrs: dict[str, Any],
    projection: Projection,
) -> DerivationOutcome:
    """The ``agentarts`` derivation: openjiuwen data contract → AgentArts
    semantics (design.md §4.3).

    Registered as ``agentarts`` by agentarts_instrumentation.jiuwen at
    import time; the core projection pipeline applies it between
    attribute injection and user drop/rename/truncate. Mutates the
    working ``attrs`` in place; returns the clone overrides (display
    name / events / links / status) or a filter marker — the generic
    pipeline skips export on ``filter=True``.
    """
    # Parent-reference recording comes first (including spans that
    # are about to be filtered — conservatively keep containers):
    # the evidence for task_iteration dangling-marker detection
    # (§4.3.7a, _ParentRefRegistry).
    parent = span.parent
    if parent is not None:
        _parent_refs.record(parent.span_id)
    if span.name in AGENTARTS_DROP_SPANS:
        # Intentional filter (transform.py semantics): this span is
        # not reported to the platform
        return DerivationOutcome(filter=True)
    if (
        _TASK_ITERATION.match(span.name)
        and not _parent_refs.contains(span.get_span_context().span_id)
    ):
        # Dangling task marker: no child references, no semantic
        # content (dev-stable/0.1.16 shape, where react rounds hang
        # directly on the AGENT root) — not exported. The container
        # shape (0.1.20, react rounds beneath it) IS referenced by
        # child spans → never reaches this branch.
        return DerivationOutcome(filter=True)
    # Child failure/success registry (consumed at AGENT root
    # projection, §4.3.9 plan B): count only spans that are
    # actually exported — filtered internal bookkeeping spans
    # (context.window.commit / llm.reasoning / dangling task
    # markers) don't participate, so an internal failure can't
    # flag the whole run; the root itself (K_TRACE_ROOT=True) is
    # never registered. ERROR registers a failure (resets the
    # recovery flag); OK registers recovery; None/UNSET (STEP
    # etc.) register nothing — a turn wrap-up after a tool failure
    # must not be misread as self-healing.
    span_status = span.status
    if attrs.get(K_TRACE_ROOT) is not True and span_status is not None:
        code = span_status.status_code
        if code == StatusCode.ERROR or code == StatusCode.OK:
            _error_aggregator.record(
                span.get_span_context().trace_id,
                span.name,
                is_error=code == StatusCode.ERROR,
            )
    _apply_agentarts(span, span.name, attrs)
    # AGENT root status escalation (child failure propagation,
    # §4.3.9 plan B): child spans end and are projected first; the
    # root ends and is projected last. Terminal failure (no
    # successful business child after the last failure) → root
    # escalates to ERROR (description names the last failure);
    # recovered → root status untouched (reflects run completion)
    # and the gen_ai.error.recovered attribute leaves a trace. An
    # already-ERROR root is never overwritten; the original span
    # and the local trajectory store are untouched; multi-root
    # traces are consumed by the first AGENT span to arrive (same
    # limit as the token/input aggregators).
    upgraded_status: Status | None = None
    if attrs.get(K_SPAN_KIND) == "AGENT":
        child_error = _error_aggregator.take(span.get_span_context().trace_id)
        if child_error is not None and (
            span.status is None or span.status.status_code != StatusCode.ERROR
        ):
            last_failed, recovered = child_error
            if recovered:
                # Self-healed: failure visibility is preserved via
                # the attribute (ops can still spot child span
                # degradation)
                attrs.setdefault(K_ERROR_RECOVERED, last_failed)
            else:
                upgraded_status = Status(
                    StatusCode.ERROR, f"child span failed: {last_failed}"
                )
    # Display-name compression (§4.3.8): must run before namespace
    # trimming — one of the rewrite inputs, openjiuwen.trace.root,
    # disappears after trimming. The original span and the local
    # trajectory store are untouched.
    display_name = _rewrite_agent_span_name(
        span.name, attrs, attrs.get(K_SPAN_KIND)
    )
    for pattern in AGENTARTS_DROP_ATTRS:
        drop_attributes(attrs, pattern)
    extra_event = _apply_skill(span, attrs, tuple(projection.skill_path_patterns))
    # Events whitelist: keep only the host-produced skill.loaded
    # detection events (the platform's skill-detection event
    # channel, §4.3.5) plus the newly synthesized detection event;
    # all other host events (streaming chunks etc. — measured, a
    # single LLM span can carry a hundred-plus) are not exported.
    events = [
        event
        for event in span.events
        if getattr(event, "name", "") == SKILL_LOADED_EVENT
    ]
    if extra_event is not None:
        events.append(extra_event)
    # links: the platform builds its tree from parent_span_id and
    # does not consume links — never export them.
    return DerivationOutcome(
        name=display_name,
        events=events,
        links=(),
        status=upgraded_status,
    )


__all__ = [
    "AGENTARTS_DROP_ATTRS",
    "AGENTARTS_DROP_SPANS",
    "AGENTARTS_RENAME",
    "K_AGENT_MODE",
    "K_LANGFUSE_OUTPUT",
    "K_OJ_SPAN_OUTPUT",
    "K_OJ_TTFT_MS",
    "K_RECORD_KIND",
    "K_TRACE_ROOT",
    "K_TURN_NUMBER",
    "LLM_OPERATIONS",
    "SKILL_LOADED_EVENT",
    "derive_agentarts",
]
