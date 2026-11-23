# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Generic export-time span projection + derivation registry (design.md
§4.2/§4.3).

The generic pipeline: attributes injection → registered derivation →
drop → rename → truncate → clone (the original span is never modified;
see exporter/transform.py). Framework packs (e.g.
agentarts-instrumentation-jiuwen) register named derivations via
``register_derivation`` that map their host data contract onto AgentArts
semantics; an empty config is an identity pass-through.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan

from .config import Projection
from .exporter.transform import clone_readable_span


@dataclass
class DerivationOutcome:
    """Result of one derivation pass over a span.

    The derivation mutates the working ``attrs`` dict in place (generic
    rules run before and after it on the same dict); fields left at
    their defaults keep the original span values in the projected clone.

    Attributes:
        filter: Skip exporting this span entirely (intentional filter —
            ``project_span`` returns None).
        name: Display-name override for the clone (None = original).
        events: Replacement events list (None = keep original).
        links: Replacement links (pass an empty sequence to clear).
        status: Replacement status (None = keep original).
    """

    filter: bool = False
    name: str | None = None
    events: Sequence[Any] | None = None
    links: Sequence[Any] | None = None
    status: Any = None


#: Derivation signature: ``(span, attrs, projection) -> DerivationOutcome | None``.
#: ``span`` is the original read-only span; ``attrs`` is the working copy
#: the derivation may mutate; ``projection`` carries the user config
#: (e.g. ``skill_path_patterns``).
Derivation = Callable[[ReadableSpan, dict[str, Any], Projection], "DerivationOutcome | None"]

_DERIVATIONS: dict[str, Derivation] = {}


def register_derivation(name: str, derivation: Derivation) -> None:
    """Register a named derivation (last registration wins)."""
    _DERIVATIONS[name] = derivation


def get_derivation(name: str) -> Derivation | None:
    """Resolve a preset name to its derivation; None when inactive.

    Raises ValueError with a hint when the name is unknown — the caller
    then knows which framework pack to install/import instead of failing
    deep inside the export path.
    """
    if name in (None, "", "none"):
        return None
    try:
        return _DERIVATIONS[name]
    except KeyError:
        available = ", ".join(sorted(_DERIVATIONS)) or "none"
        raise ValueError(
            f"unknown projection preset {name!r}: no derivation registered under this "
            f"name (available: {available}). The 'agentarts' preset is provided by "
            f"agentarts-instrumentation-jiuwen — install it and import "
            f"agentarts_instrumentation.jiuwen."
        ) from None


def drop_attributes(attrs: dict[str, Any], pattern: str) -> None:
    """Drop by exact key or ``prefix*`` (shared by pipeline and derivations)."""
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        for key in [key for key in attrs if key.startswith(prefix)]:
            del attrs[key]
    else:
        attrs.pop(pattern, None)


def _is_identity(projection: Projection) -> bool:
    return (
        projection.preset in (None, "", "none")
        and not projection.attributes
        and not projection.drop
        and not projection.rename
        and projection.truncate <= 0
        and not projection.service_name
    )


def project_span(span: ReadableSpan, projection: Projection) -> ReadableSpan | None:
    """Return the projected span clone; the input span is untouched.

    With an inactive config this is an identity pass-through (returns the
    original object, zero copies). A derivation may intentionally filter
    a span — those return ``None`` (TransformingSpanExporter skips
    export).
    """
    if _is_identity(projection):
        return span

    attrs: dict[str, Any] = dict(span.attributes or {})
    outcome: DerivationOutcome | None = None

    # 1. attributes injection (never overwrites existing keys)
    for key, value in projection.attributes.items():
        if key not in attrs:
            attrs[key] = value

    # 2. registered derivation (framework semantic mapping; may mutate
    #    attrs in place and override name/events/links/status)
    derivation = get_derivation(projection.preset)
    if derivation is not None:
        outcome = derivation(span, attrs, projection) or DerivationOutcome()
        if outcome.filter:
            return None

    # 3. drop (exact key / prefix*)
    for pattern in projection.drop:
        drop_attributes(attrs, pattern)

    # 4. user rename (existing target keys are not overwritten)
    for src, dst in projection.rename.items():
        if src in attrs and dst not in attrs:
            attrs[dst] = attrs.pop(src)

    # 5. truncate (str values only)
    if projection.truncate > 0:
        for key, value in attrs.items():
            if isinstance(value, str) and len(value) > projection.truncate:
                attrs[key] = value[: projection.truncate]

    resource = None
    if projection.service_name:
        from opentelemetry.sdk.resources import Resource

        base_attributes = dict(getattr(span.resource, "attributes", None) or {})
        base_attributes["service.name"] = projection.service_name
        resource = Resource.create(base_attributes)

    return clone_readable_span(
        span,
        attrs,
        events=outcome.events if outcome is not None else None,
        resource=resource,
        name=outcome.name if outcome is not None else None,
        links=outcome.links if outcome is not None else None,
        status=outcome.status if outcome is not None else None,
    )


__all__ = [
    "Derivation",
    "DerivationOutcome",
    "drop_attributes",
    "get_derivation",
    "project_span",
    "register_derivation",
]
