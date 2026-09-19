"""OTel export for checks. Optional: without the SDK (or without an
endpoint configured) every function here is a silent no-op, so tracing can
never break a check.

Two modes:
  standalone  AGENTCHECK_OTLP_ENDPOINT is set and no provider exists yet:
              we build one with an OTLP/HTTP exporter.
  embedded    the host app (LangChain user, etc.) already configured OTel:
              we reuse their provider, so our spans land in THEIR backend.

A "fail" verdict never sets span ERROR: the verdict is about the tool call,
not about our operation. It rides as attributes instead.
"""

from __future__ import annotations

import os

SERVICE = "agentcheck"
_SPAN = "agentcheck.check"

_configured_endpoint = None


def available() -> bool:
    try:
        __import__("opentelemetry.trace")
        __import__("opentelemetry.sdk.trace")
        return True
    except Exception:
        return False


def enabled() -> bool:
    """Live spans will be produced."""
    if not available():
        return False
    try:
        from opentelemetry import trace as _t
        from opentelemetry.sdk import trace as _s
        return isinstance(_t.get_tracer_provider(), _s.TracerProvider)
    except Exception:
        return False


def configure(endpoint: str | None = None) -> bool:
    """Turn spans on. Idempotent; safe to call on every startup.

    Returns True when spans will flow somewhere.
    """
    global _configured_endpoint
    if not available():
        return False
    ep = (endpoint or os.environ.get("AGENTCHECK_OTLP_ENDPOINT") or "").strip()
    try:
        from opentelemetry import trace as _t
        from opentelemetry.sdk import trace as _s
        provider = _t.get_tracer_provider()
        if not isinstance(provider, _s.TracerProvider):
            # Embedded case with no provider yet AND no endpoint: nothing
            # to send to. Standalone case: build our own.
            if not ep:
                return False
            provider = _s.TracerProvider()
            _t.set_tracer_provider(provider)
        if ep and ep != _configured_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter)
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=ep)))
            _configured_endpoint = ep
        return True
    except Exception:
        return False


def _tracer():
    from opentelemetry import trace as _t
    return _t.get_tracer(SERVICE)


def parent_context(traceparent: str | None):
    """W3C traceparent value -> OTel context, or None to start a root."""
    if not traceparent or not available():
        return None
    try:
        from opentelemetry.propagate import extract
        return extract({"traceparent": traceparent})
    except Exception:
        return None


def start(name: str = _SPAN, traceparent: str | None = None,
          attrs: dict | None = None):
    """Begin a span, or None when tracing is off. Never raises."""
    if not enabled():
        return None
    try:
        from opentelemetry.trace import SpanKind
        span = _tracer().start_span(
            name, context=parent_context(traceparent), kind=SpanKind.SERVER)
        if attrs:
            set_attrs(span, attrs)
        return span
    except Exception:
        return None


def set_attrs(span, attrs: dict) -> None:
    """Set attributes; tolerates None span and bad values."""
    if span is None or not attrs:
        return
    try:
        for k, v in attrs.items():
            if v is None:
                continue
            if isinstance(v, bool):
                span.set_attribute(k, v)
            elif isinstance(v, (int, float)):
                span.set_attribute(k, v)
            else:
                span.set_attribute(k, str(v)[:2000])
    except Exception:
        pass


def end(span) -> None:
    if span is None:
        return
    try:
        span.end()
    except Exception:
        pass


def result_attrs(payload: dict, trace_id=None, span_id=None) -> dict:
    """Span attributes for a finished check. Fail verdicts stay OUT of
    span status; they are data, not errors."""
    # annotate() puts the float at payload["reliability"] and the full
    # lookup (accuracy, source, bin, gap) at payload["calibrated"].
    rel = payload.get("calibrated") or {}
    return {
        "agentcheck.tool": payload.get("tool"),
        "agentcheck.verdict": payload.get("trace_verdict"),
        "agentcheck.confidence": payload.get("confidence"),
        "agentcheck.reliability": rel.get("reliability"),
        "agentcheck.reliability_gap": rel.get("gap"),
        "agentcheck.reliability_source": rel.get("source"),
        "agentcheck.decision": payload.get("decision"),
        "agentcheck.policy": payload.get("policy"),
        "agentcheck.checkset": payload.get("checkset"),
        "agentcheck.trace_id": trace_id,
        "agentcheck.span_id": span_id,
    }
