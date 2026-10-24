"""The last N traces, kept in memory so the admin API (and the UI) can show a request's spans.

A span processor, not an exporter: it sees every finished span with no network and no
batching delay, and adding it changes nothing about what the gateway does. Spans are grouped by
trace id; when more than `max_traces` traces are held, the oldest is dropped. Per process: with
two replicas, each one only knows the requests it served.
"""
from __future__ import annotations

import threading
from collections import OrderedDict

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor


def _hex(n: int, width: int) -> str:
    return format(n, f"0{width}x")


def span_dict(s: ReadableSpan) -> dict:
    return {
        "span_id": _hex(s.context.span_id, 16),
        "parent_id": _hex(s.parent.span_id, 16) if s.parent else None,
        "name": s.name,
        "kind": s.kind.name.lower(),
        "start_ns": s.start_time,
        "end_ns": s.end_time,
        "status": s.status.status_code.name.lower(),  # unset | ok | error
        "attributes": {k: (list(v) if isinstance(v, tuple) else v)
                       for k, v in (s.attributes or {}).items()},
        "events": [{"name": e.name, "ts_ns": e.timestamp, "attributes": dict(e.attributes or {})}
                   for e in s.events],
    }


class RecentTraces(SpanProcessor):
    def __init__(self, max_traces: int = 200):
        self.max_traces = max_traces
        self._traces: OrderedDict[str, list[dict]] = OrderedDict()
        self._lock = threading.Lock()  # on_end can run on exporter / worker threads

    def on_end(self, span: ReadableSpan) -> None:
        tid = _hex(span.context.trace_id, 32)
        d = span_dict(span)
        with self._lock:
            self._traces.setdefault(tid, []).append(d)
            self._traces.move_to_end(tid)
            while len(self._traces) > self.max_traces:
                self._traces.popitem(last=False)

    def get(self, trace_id: str) -> list[dict] | None:
        with self._lock:
            spans = self._traces.get(trace_id)
            return sorted(spans, key=lambda s: s["start_ns"]) if spans else None

    def roots(self, limit: int = 50) -> list[dict]:
        """Newest first: the root span of each held trace (the request span), with its id."""
        out = []
        with self._lock:
            for tid, spans in reversed(self._traces.items()):
                ids = {s["span_id"] for s in spans}  # a caller's traceparent: remote parent
                root = next((s for s in spans if s["parent_id"] not in ids), None)
                if root is not None:
                    out.append({"trace_id": tid, **root, "span_count": len(spans)})
                if len(out) >= limit:
                    break
        return out
