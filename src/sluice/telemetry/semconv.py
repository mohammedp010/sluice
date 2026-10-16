"""The OpenTelemetry GenAI attribute names Sluice emits, pinned in one place.

Pinned to: semantic conventions schema 1.44.0 (Python package opentelemetry-semantic-conventions
0.66b1). Status: *Development*. They are not stable, and in that release the GenAI names are
marked "moved to the semantic-conventions-genai repository". They have already been renamed
once (`gen_ai.system` -> `gen_ai.provider.name`). Spelling them out here instead of importing
from `opentelemetry.semconv._incubating` means an upgrade can't rename them silently. A rename
is one edit here plus a dashboard change. See ADR 0004.
"""
SCHEMA_URL = "https://opentelemetry.io/schemas/1.44.0"

OPERATION_NAME = "gen_ai.operation.name"  # always "chat" for now
PROVIDER_NAME = "gen_ai.provider.name"  # was gen_ai.system before 1.37
REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
REQUEST_TEMPERATURE = "gen_ai.request.temperature"
RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
TOKEN_TYPE = "gen_ai.token.type"  # "input" | "output", on the token-usage histogram
ERROR_TYPE = "error.type"  # stable (general semconv), low-cardinality reason

# metrics
CLIENT_OPERATION_DURATION = "gen_ai.client.operation.duration"  # seconds, per provider attempt
CLIENT_TOKEN_USAGE = "gen_ai.client.token.usage"

# Bucket boundaries recommended by the GenAI conventions. The SDK default buckets are tuned for
# milliseconds and would put every LLM call (seconds) in the last bucket.
DURATION_BUCKETS = [0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48,
                    40.96, 81.92]
TOKEN_BUCKETS = [1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304]
