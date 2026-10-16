"""Opt-in stream resume: when a stream dies after its first chunk, continue it on the next
provider from the text the client already has, instead of ending with an error event.

ADR 0001 forbids *silently restarting* (the client would get two different answers spliced
together). Resuming is different: the new provider is shown the text already sent and asked to
carry on from it.
  prefill   send that text as a trailing assistant message (the model continues its own words)
  instruct  send it, then a user turn asking to continue without repeating anything
Models still sometimes restate the last few words, or start over. So the first part of the
continuation is held back until it is clear how much of it repeats what was already sent, and
that overlap is cut. bench/stream_resume_eval.py measures how often each of these goes wrong.
"""
from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from ..config import Route, Target
from ..providers.base import ChatParams, Chunk, Message, ProviderError
from .executor import AllProvidersFailed, Executor, NonRetryableError, Trail

CONTINUE = ("Continue exactly where your previous message stopped. Output only the "
            "continuation: do not repeat anything already written, do not start over.")
MIN_OVERLAP = 8  # chars; shorter "overlaps" are usually coincidence (a newline, "the ")
HOLD_MAX = 2000  # never hold back more than this many chars of the continuation


def continuation_params(params: ChatParams, prefix: str, strategy: str) -> ChatParams:
    msgs = [*params.messages, Message("assistant", prefix)]
    if strategy == "instruct":
        msgs.append(Message("user", CONTINUE))
    left = None
    if params.max_tokens is not None:  # the client's output cap covers both segments
        left = max(16, params.max_tokens - len(prefix) // 4)
    return dataclasses.replace(params, messages=msgs, max_tokens=left)


def overlap(prefix: str, head: str, minimum: int = MIN_OVERLAP) -> int:
    """Length of the longest start of `head` that `prefix` already ends with (>= minimum).
    A continuation that starts over from the top is the extreme case: all of `prefix`."""
    for k in range(min(len(prefix), len(head)), minimum - 1, -1):
        if prefix.endswith(head[:k]):
            return k
    return 0


@dataclass
class Segment:
    """One provider's part of the answer, for billing and the trace."""

    trail: Trail
    text: str = ""
    usage: object = None  # Usage when the provider reported it
    cut: bool = False  # died mid-stream


@dataclass
class ResumableStream:
    ex: Executor
    route: Route
    params: ChatParams
    strategy: str  # "off" | "prefill" | "instruct"
    on_resume: Callable[[str, int, str], None] = lambda provider, chars, nxt: None
    segments: list[Segment] = field(default_factory=list)
    trimmed: int = 0  # chars of repeated text cut at the seam

    async def chunks(self, first: Chunk | None, upstream: AsyncIterator[Chunk],
                     trail: Trail) -> AsyncIterator[Chunk]:
        seg = Segment(trail)
        self.segments.append(seg)
        try:
            for c in ([first] if first else []):
                seg.text += c.content
                yield c
            async for c in upstream:
                seg.text += c.content
                seg.usage = c.usage or seg.usage
                yield c
            return
        except ProviderError as e:
            seg.cut = True  # the app reports cut segments to the breakers when the stream ends
            if self.strategy == "off":
                raise
            error = e
        finally:
            await upstream.aclose()  # also on client disconnect: stop the provider generating
        prefix = seg.text
        try:
            (first2, up2), trail2 = await self.ex.stream(
                self.route, continuation_params(self.params, prefix, self.strategy),
                skip=frozenset({trail.provider}))
        except (AllProvidersFailed, NonRetryableError):
            raise error from None  # nowhere to resume: the original error stands
        self.on_resume(trail.provider, len(prefix), trail2.provider or "")
        seg2 = Segment(trail2)
        self.segments.append(seg2)
        held, released = "", False
        try:
            async for c in _chain(first2, up2):
                seg2.text += c.content
                seg2.usage = c.usage or seg2.usage
                if released:
                    yield c
                    continue
                held += c.content
                if len(held) >= min(len(prefix), HOLD_MAX) + MIN_OVERLAP or c.finish_reason:
                    yield self._release(prefix, held, c)
                    released = True
            if not released and held:
                yield self._release(prefix, held, Chunk(finish_reason="stop"))
        except ProviderError:
            seg2.cut = True  # one resume per request: a second death ends the stream
            raise
        finally:
            await up2.aclose()

    def _release(self, prefix: str, held: str, last: Chunk) -> Chunk:
        self.trimmed = overlap(prefix, held)
        return Chunk(content=held[self.trimmed:], finish_reason=last.finish_reason,
                     usage=last.usage)

    def bills(self, estimate: Callable[[Segment], object]) -> list[tuple[Target, object]]:
        return [(s.trail.target, s.usage or estimate(s)) for s in self.segments if s.trail.target]


async def _chain(first: Chunk | None, rest: AsyncIterator[Chunk]) -> AsyncIterator[Chunk]:
    if first:
        yield first
    async for c in rest:
        yield c
