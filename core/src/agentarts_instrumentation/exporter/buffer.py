# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""BufferedOtlpExporter: self-contained batch buffering (design.md §4.5).

The injection path uses SimpleSpanProcessor — ``export()`` is called
synchronously the moment a span ends, so the official BatchSpanProcessor
buffering is not in the chain. Therefore ``export()`` here only
enqueues into a bounded queue and returns immediately; a background
thread sends batches of ``batch_size`` every ``flush_interval`` to the
inner official OTLP exporter. When the queue is full the oldest spans
are dropped with a warning — business threads are never blocked.

Failed batch exports (network timeout / transient faults) get bounded
retries (``export_attempts`` total, exponential backoff); spans are only
counted as dropped after retries are exhausted — measured on slow
cross-network links, a single RPC timeout (DEADLINE_EXCEEDED) would
otherwise irreversibly lose the whole batch.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from ..config import ExporterConfig

logger = logging.getLogger(__name__)

_FLUSHER_NAME = "agentarts-otel-exporter-flush"

#: Diagnostic switch: when set, appends one line per span entering the
#: SDK buffer to the given file. Used to tell "host never ended the
#: span" apart from "export pipeline dropped it" (off by default, zero
#: overhead).
_AUDIT_PATH = os.environ.get("AGENTARTS_PROBE_SPAN_LOG", "").strip()
_audit_lock = threading.Lock()


def _audit_span(spans: Any) -> None:
    """Write span identifiers to the audit file (diagnostics only)."""
    if not _AUDIT_PATH:
        return
    try:
        with _audit_lock, open(_AUDIT_PATH, "a", encoding="utf-8") as fh:
            for span in spans or ():
                ctx = span.get_span_context()
                parent = span.parent.span_id if span.parent is not None else 0
                fh.write(
                    f"{ctx.trace_id:032x}\t{ctx.span_id:016x}\t{parent:016x}\t"
                    f"{span.end_time}\t{span.name}\n"
                )
    except Exception:  # noqa: BLE001 - diagnostics must never affect export
        pass


def _audit_batch(result: Any, count: int, attempt: int = 1, batch_bytes: int = 0) -> None:
    """Record one batch send result (diagnostics only)."""
    if not _AUDIT_PATH:
        return
    try:
        with _audit_lock, open(_AUDIT_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"#BATCH\t{count}\t{result}\tattempt={attempt}\tbytes={batch_bytes}\n")
    except Exception:  # noqa: BLE001
        pass


def _estimate_span_bytes(span: Any) -> int:
    """Estimate a span's memory footprint in bytes (defensive rough
    accounting, not the exact serialized length).

    Basis for the queue byte limit: large payloads concentrate in
    string attribute values (LLM span input/output.messages measured
    up to ~300KB per entry), so summing attribute key/value lengths and
    event names captures the dominant cost; non-string scalars count as
    a fixed 16B. Per-attribute cost is O(#attrs) (usually <30) —
    acceptable on the business-thread export path.
    """
    total = 256  # span object itself + fixed metadata overhead
    total += len(getattr(span, "name", "") or "")
    attributes = getattr(span, "attributes", None) or {}
    for key, value in attributes.items():
        total += len(str(key)) + 16
        if isinstance(value, str):
            total += len(value)
        else:
            total += 16
    for event in getattr(span, "events", None) or ():
        total += len(getattr(event, "name", "") or "") + 128
    return total


class BufferedOtlpExporter(SpanExporter):
    """Bounded queue + background flush thread wrapping an inner exporter.

    Behavior contract (design.md §4.5):

    - ``export(spans)``: enqueue and return SUCCESS immediately; never
      blocks the caller;
    - Batch assembly (count + byte cap): a batch is closed once it
      reaches ``batch_size`` spans **or** estimated ``batch_max_bytes``
      (a span that would overflow it starts the next batch); a single
      span larger than the cap is sent as its own batch — never
      dropped for size alone;
    - Backpressure (dual limit): evict oldest with a warning once the
      queue reaches ``queue_size`` **or** estimated bytes reach
      ``queue_max_bytes`` — never raises into business flow. The count
      limit alone cannot bound memory amplification from large-payload
      spans (a single LLM span can be ~300KB; 4096 entries could
      accumulate GBs);
    - Failed batches: bounded retry (``export_attempts`` total, expo-
      nential backoff); dropped and warned only when exhausted — a slow
      link's single-RPC timeout no longer loses the whole batch;
    - ``force_flush(timeout_millis)``: drains the queue, then delegates
      to inner force_flush when present; returns False on timeout;
    - ``shutdown()``: drain (bounded by export_timeout_ms) → close
      inner → idempotent.
    """

    def __init__(self, exporter: SpanExporter, config: ExporterConfig) -> None:
        self._exporter = exporter
        self._maxsize = config.queue_size
        self._maxbytes = config.queue_max_bytes
        self._batch_size = config.batch_size
        self._batch_max_bytes = config.batch_max_bytes
        self._flush_interval = config.flush_interval_ms / 1000.0
        self._shutdown_timeout = config.export_timeout_ms / 1000.0
        self._max_attempts = max(1, int(config.export_attempts))
        # Queue entries are (span, estimated_bytes) pairs — bytes are
        # accounted on enqueue/dequeue/eviction.
        self._queue: deque[tuple[ReadableSpan, int]] = deque()
        self._queued_bytes = 0
        self._dropped = 0
        self._reported_dropped = 0
        self._warned_oversize = False
        self._closed = False
        self._shutdown_done = False
        self._sending = False
        self._cond = threading.Condition()
        self._thread = threading.Thread(target=self._run, name=_FLUSHER_NAME, daemon=True)
        self._thread.start()

    # ---- SpanExporter API ----

    def export(self, spans: Any) -> SpanExportResult:
        if not spans:
            return SpanExportResult.SUCCESS
        _audit_span(spans)
        with self._cond:
            if self._closed:
                return SpanExportResult.FAILURE
            for span in spans:
                size = _estimate_span_bytes(span)
                if size > self._maxbytes and not self._queue:
                    # Single span exceeds the entire byte budget: enqueuing
                    # would overflow — drop immediately (warn once, then
                    # silent counting; flush/shutdown reports the total).
                    self._dropped += 1
                    if self._dropped == 1:
                        logger.warning(
                            "single span estimated %sB exceeds queue byte cap %sB; dropping",
                            size,
                            self._maxbytes,
                        )
                    continue
                while self._queue and (
                    len(self._queue) >= self._maxsize
                    or self._queued_bytes + size > self._maxbytes
                ):
                    _, dropped_size = self._queue.popleft()
                    self._queued_bytes -= dropped_size
                    self._dropped += 1
                    if self._dropped == 1:
                        # Warn immediately only on the first drop, then count
                        # silently until flush/shutdown summarizes — avoids a
                        # per-span log storm under flood.
                        logger.warning(
                            "export queue full (size=%s spans / %sB); dropping oldest spans",
                            self._maxsize,
                            self._maxbytes,
                        )
                self._queue.append((span, size))
                self._queued_bytes += size
            self._cond.notify_all()
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        deadline = time.monotonic() + max(timeout_millis, 0) / 1000.0
        with self._cond:
            self._cond.notify_all()
            while (self._queue or self._sending) and time.monotonic() < deadline:
                # 0.05s step wait: the background thread notifies after each
                # batch completes.
                self._cond.wait(0.05)
            drained = not self._queue and not self._sending
        if not drained:
            return False
        self._report_drops("force_flush")
        # Official OTLP exporters send immediately and provide no
        # force_flush; delegate when present.
        inner_flush = getattr(self._exporter, "force_flush", None)
        if callable(inner_flush):
            try:
                return bool(inner_flush(timeout_millis=timeout_millis))
            except Exception:
                logger.exception("inner exporter force_flush failed")
                return False
        return True

    def shutdown(self) -> None:
        with self._cond:
            if self._shutdown_done:
                return
            self._closed = True
            self._cond.notify_all()
        self._thread.join(timeout=self._shutdown_timeout)
        try:
            self._exporter.shutdown()
        finally:
            self._report_drops("shutdown")
            with self._cond:
                self._shutdown_done = True

    # ---- internals ----

    def _report_drops(self, context: str) -> None:
        """Drop summary: new + cumulative since the last report."""
        if self._dropped > self._reported_dropped:
            logger.warning(
                "export queue drop summary (%s): +%s new, %s total dropped",
                context,
                self._dropped - self._reported_dropped,
                self._dropped,
            )
            self._reported_dropped = self._dropped

    def _take_batch(self) -> list[tuple[ReadableSpan, int]]:
        """Pop one batch: count cap, byte cap, whichever fires first.

        Entries are ``(span, estimated_bytes)`` pairs. Byte cap: close
        the batch before popping a span that would push it over
        ``batch_max_bytes`` — that span starts the next batch. A single
        span larger than the cap is always taken alone (batch of one,
        never dropped): the cap is a splitting granularity, not a
        request limit — spans up to the 128MB queue budget are legal.
        """
        batch: list[tuple[ReadableSpan, int]] = []
        batch_bytes = 0
        with self._cond:
            while not self._queue and not self._closed:
                self._cond.wait(self._flush_interval)
            while self._queue and len(batch) < self._batch_size:
                span, size = self._queue[0]
                if batch and batch_bytes + size > self._batch_max_bytes:
                    break
                if size > self._batch_max_bytes and not self._warned_oversize:
                    self._warned_oversize = True
                    logger.warning(
                        "single span estimated %sB exceeds batch byte cap %sB; "
                        "sending it as its own batch",
                        size,
                        self._batch_max_bytes,
                    )
                self._queue.popleft()
                self._queued_bytes -= size
                batch.append((span, size))
                batch_bytes += size
        return batch

    def _run(self) -> None:
        while True:
            batch = self._take_batch()
            if batch:
                with self._cond:
                    self._sending = True
                try:
                    spans = [span for span, _ in batch]
                    self._send_with_retry(spans, sum(size for _, size in batch))
                finally:
                    with self._cond:
                        self._sending = False
                        self._cond.notify_all()
                continue
            with self._cond:
                if self._closed and not self._queue:
                    return

    def _send_with_retry(self, batch: list[ReadableSpan], batch_bytes: int = 0) -> None:
        """Send one batch; retry with exponential backoff, count as
        dropped only when attempts are exhausted.

        The official OTLP exporter returns FAILURE on network failures
        (timeout / routing) instead of raising; auth rejection also
        surfaces as FAILURE — both retried (bounded, no infinite loop).
        During backoff no lock is held and ``_sending`` stays True so
        force_flush waits correctly; when shutdown has closed the queue,
        skip backoff (the process is about to exit).
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                result = self._exporter.export(batch)
            except Exception:
                logger.exception(
                    "batch export exception (%s spans, attempt %s)", len(batch), attempts
                )
                result = SpanExportResult.FAILURE
            _audit_batch(result, len(batch), attempts, batch_bytes)
            if result is None or result == SpanExportResult.SUCCESS:
                return
            if attempts >= self._max_attempts:
                with self._cond:
                    self._dropped += len(batch)
                logger.warning(
                    "batch export returned %s %s times in a row (%s spans dropped; "
                    "check endpoint/credentials/network)",
                    result,
                    attempts,
                    len(batch),
                )
                return
            logger.warning(
                "batch export returned %s (%s spans, attempt %s); retrying after backoff",
                result,
                len(batch),
                attempts,
            )
            if not self._closed:
                # Exponential backoff: 1s, 2s, 4s, 8s, 16s, 30s... (capped
                # at 30s; the default 5 attempts only ever sleep 1/2/4/8s).
                # Measured slow-link gateway outages lasted ~90s
                # (2026-11-19: one trace lost 30 spans in two 3-failure
                # streaks); default policy: 5 attempts x 30s RPC + ~15s
                # backoff ~ 3 minutes of outage survival, and the 10MB
                # batch byte cap keeps single requests transferable
                # within the RPC timeout even on slow gateways.
                time.sleep(min(2 ** (attempts - 1), 30))


__all__ = ["BufferedOtlpExporter"]
