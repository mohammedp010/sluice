"""OpenTelemetry wiring: one tracer provider, one meter provider, and the metric instruments.

Providers are created per app and passed in, not set globally, so tests can run several
gateways side by side, each with its own in-memory exporter.

Cardinality rule: metric attributes are only bounded sets (model, provider, outcome). Per-key
and per-team numbers would create one time series per key, so they live in the SQLite usage
ledger instead (see ledger.py).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from opentelemetry.metrics import CallbackOptions, Meter, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)

from . import semconv as sc

BREAKER_VALUES = {"closed": 0, "half_open": 1, "open": 2}


@dataclass
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    prometheus: bool = False  # the app serves /metrics from prometheus_client's registry
    readers: list[MetricReader] = field(default_factory=list)

    def tracer(self):
        return self.tracer_provider.get_tracer("sluice", schema_url=sc.SCHEMA_URL)

    def meter(self) -> Meter:
        return self.meter_provider.get_meter("sluice", schema_url=sc.SCHEMA_URL)

    def shutdown(self) -> None:
        self.tracer_provider.shutdown()  # flushes batched spans
        self.meter_provider.shutdown()


def build_telemetry(
    service_name: str = "sluice",
    traces: str = "none",
    otlp_endpoint: str = "http://localhost:4318",
    prometheus: bool = False,
    span_exporter: SpanExporter | None = None,
    metric_readers: list[MetricReader] | None = None,
) -> Telemetry:
    resource = Resource.create({"service.name": service_name})
    tp = TracerProvider(resource=resource)
    if span_exporter is not None:  # tests and the demo script
        tp.add_span_processor(SimpleSpanProcessor(span_exporter))
    elif traces == "console":
        tp.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif traces == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        # batch: exporting must never sit on the request path
        tp.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=otlp_endpoint.rstrip("/") + "/v1/traces")))

    readers = list(metric_readers or [])
    if prometheus:  # pull model: Prometheus scrapes /metrics, nothing is pushed
        from opentelemetry.exporter.prometheus import PrometheusMetricReader

        readers.append(PrometheusMetricReader())
    mp = MeterProvider(resource=resource, metric_readers=readers)
    return Telemetry(tp, mp, prometheus, readers)


class Metrics:
    def __init__(self, meter: Meter, breaker_states: Callable[[], dict[str, str]]):
        self.requests = meter.create_counter(
            "sluice.requests", "{request}", "Requests handled, by public model and outcome")
        self.duration = meter.create_histogram(
            "sluice.request.duration", "s", "End-to-end gateway time (streams: until last byte)",
            explicit_bucket_boundaries_advisory=sc.DURATION_BUCKETS)
        self.attempt_duration = meter.create_histogram(
            sc.CLIENT_OPERATION_DURATION, "s", "One upstream attempt (streams: to first chunk)",
            explicit_bucket_boundaries_advisory=sc.DURATION_BUCKETS)
        self.tokens = meter.create_histogram(
            sc.CLIENT_TOKEN_USAGE, "{token}", "Tokens per served request",
            explicit_bucket_boundaries_advisory=sc.TOKEN_BUCKETS)
        self.cost = meter.create_counter("sluice.cost", "USD", "Spend from the route price table")
        self.cache_lookups = meter.create_counter(
            "sluice.cache.lookups", "{lookup}", "Semantic cache lookups by result and skip reason")
        self.escalations = meter.create_counter(
            "sluice.escalations", "{escalation}", "Answers re-asked on the stronger route, by check")
        self.stream_resumes = meter.create_counter(
            "sluice.stream_resumes", "{resume}", "Streams continued on another provider")
        self.fallbacks = meter.create_counter(
            "sluice.fallbacks", "{fallback}", "Times a target was abandoned for the next one")
        self.auth_rejections = meter.create_counter(
            "sluice.auth.rejections", "{request}",
            "Requests refused at the key check, by reason (missing, unknown, expired, ...)")

        def observe(_: CallbackOptions):
            for name, state in breaker_states().items():
                yield Observation(BREAKER_VALUES[state], {"sluice.provider": name})

        meter.create_observable_gauge(
            "sluice.breaker.state", [observe], "1", "0 closed, 1 half-open, 2 open")
