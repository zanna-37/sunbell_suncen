"""Reactive coalescing burst scheduler.

Sits between cover entities / service handlers and the RF transmitter. Per blind
(keyed by `(remote, channel)`) it maintains a FIFO queue of `BurstStep` entries
that callers pre-decompose at submit time (e.g. a tilt walk expands into
`[DOWN, LONG_DOWN, LONG_DOWN, ...]`). At every dispatch opportunity the worker
sweeps every blind whose head step is ready, bins by `(remote, action)`, and
emits one multi-channel merged burst per worker iteration. Settle waits overlap
across blinds because the worker doesn't sleep on them -- it tracks `busy_until`
per blind and excludes the blind from the sweep until `busy_until <= now`.

Preemption (`submit()` on a blind that already has pending state) replaces
everything: the queue is cleared, `busy_until` is reset to 0, any pending
settled callback is dropped. No action is special at the sweep -- STOP, UP,
DOWN, LONG_UP, LONG_DOWN are all peers; the asymmetry between "fast" (UP/DOWN)
and "slow" (LONG_*) actions is encoded in `motion_time` only.

The scheduler is decoupled from Home Assistant: the constructor takes a loop
and a duck-typed transmit object (with `async def send(pulses: list[int])`).
In production we wire in `TransmitQueue`; in tests we wire in a fake recorder.
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .cover import SunbellBlind

_LOGGER = logging.getLogger(__name__)

BlindKey = tuple[str, int]   # (remote, channel)


class TransmitProtocol(Protocol):
    """Anything with an async `send(pulses)` method (TransmitQueue, fakes)."""

    async def send(self, pulses: list[int]) -> None: ...


@dataclass(slots=True, frozen=True)
class BurstStep:
    """One queued RF burst belonging to a single (remote, channel) blind.

    `motion_time` is the wait after dispatch before the blind is free again.
    Use > 0 for UP/DOWN (the motor physically moves), 0 for STOP/LONG_*
    (no physical motion or instantaneous). The scheduler records the step's
    `on_complete` to fire at `dispatched_at + motion_time` for motion_time > 0,
    or immediately after `on_dispatch` for motion_time == 0.
    """

    blind_key: BlindKey
    action: str
    motion_time: float
    on_dispatch: Callable[[], None] | None
    on_complete: Callable[[], None] | None
    enqueued_at: float


@dataclass(slots=True)
class BlindState:
    """Scheduler-owned per-blind state. Only the worker mutates `busy_until`
    and `pending_settled`; `submit()` resets them on preempt."""

    key: BlindKey
    queue: deque[BurstStep] = field(default_factory=deque)
    busy_until: float = 0.0
    pending_settled: tuple[float, Callable[[], None]] | None = None
    entity_ref: weakref.ReferenceType["SunbellBlind"] | None = None


BurstBuilder = Callable[[str, list[int], str], list[int]]


class BurstScheduler:
    """Per-config-entry intent-level burst scheduler."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        transmit: TransmitProtocol,
        burst_builder: BurstBuilder,
    ) -> None:
        self._loop = loop
        self._transmit = transmit
        self._build_burst = burst_builder
        self._blinds: dict[BlindKey, BlindState] = {}
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ chains
    @staticmethod
    def fast_step(
        blind_key: BlindKey,
        action: str,
        *,
        motion_time: float,
        on_dispatch: Callable[[], None] | None = None,
        on_complete: Callable[[], None] | None = None,
    ) -> BurstStep:
        """Build a UP/DOWN/STOP step. STOP typically passes motion_time=0."""
        return BurstStep(
            blind_key=blind_key,
            action=action,
            motion_time=motion_time,
            on_dispatch=on_dispatch,
            on_complete=on_complete,
            enqueued_at=0.0,
        )

    @staticmethod
    def tilt_step(
        blind_key: BlindKey,
        action: str = "LONG_DOWN",
        *,
        on_dispatch: Callable[[], None] | None = None,
        on_complete: Callable[[], None] | None = None,
    ) -> BurstStep:
        """Build a LONG_DOWN or LONG_UP slat-tilt step (motion_time = 0)."""
        return BurstStep(
            blind_key=blind_key,
            action=action,
            motion_time=0.0,
            on_dispatch=on_dispatch,
            on_complete=on_complete,
            enqueued_at=0.0,
        )

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = self._loop.create_task(
                self._run(), name="sunbell_suncen.scheduler"
            )

    async def async_close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        for st in self._blinds.values():
            st.queue.clear()
            st.pending_settled = None
            st.busy_until = 0.0

    # ----------------------------------------------------------------- submit
    def submit(
        self,
        blind_key: BlindKey,
        steps: Sequence[BurstStep],
        *,
        entity: "SunbellBlind | None" = None,
    ) -> None:
        """Replace this key's pending state with `steps`.

        Drops the entire queue, resets `busy_until` to 0, cancels any pending
        settled callback. Any burst already in flight for this key (mid-await
        on `transmit.send`) completes on the wire but its callbacks are
        skipped via the worker's post-dispatch identity check. Sync -- no
        awaits. Wakes the worker.
        """
        st = self._get_or_create(blind_key, entity)
        st.queue.clear()
        st.busy_until = 0.0
        st.pending_settled = None
        now = self._loop.time()
        for step in steps:
            st.queue.append(
                BurstStep(
                    blind_key=step.blind_key,
                    action=step.action,
                    motion_time=step.motion_time,
                    on_dispatch=step.on_dispatch,
                    on_complete=step.on_complete,
                    enqueued_at=now,
                )
            )
        self._wake.set()

    # ------------------------------------------------------------- internals
    def _get_or_create(
        self,
        key: BlindKey,
        entity: "SunbellBlind | None",
    ) -> BlindState:
        st = self._blinds.get(key)
        if st is None:
            st = BlindState(key=key)
            self._blinds[key] = st
        if entity is not None:
            st.entity_ref = weakref.ref(entity)
        return st

    async def _run(self) -> None:
        try:
            while True:
                await self._tick()
        except asyncio.CancelledError:
            return

    async def _tick(self) -> None:
        self._wake.clear()
        now = self._loop.time()

        self._fire_settled(now)

        ready: list[tuple[BlindState, BurstStep]] = [
            (st, st.queue[0])
            for st in self._blinds.values()
            if st.queue and st.busy_until <= now
        ]

        if not ready:
            self._gc_anonymous()
            deadline = self._earliest_future_event(now)
            await self._wait(deadline)
            return

        bins: dict[tuple[str, str], list[tuple[BlindState, BurstStep]]] = {}
        for st, step in ready:
            bins.setdefault((st.key[0], step.action), []).append((st, step))

        chosen_key = min(
            bins,
            key=lambda k: (
                min(s.enqueued_at for _, s in bins[k]),
                -len(bins[k]),
            ),
        )
        chosen = bins[chosen_key]
        remote, action = chosen_key
        channels = sorted({st.key[1] for st, _ in chosen})

        pulses = self._build_burst(remote, channels, action)
        try:
            await self._transmit.send(pulses)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(
                "burst dispatch failed: remote=%s action=%s channels=%s",
                remote, action, channels,
            )
            for st, step in chosen:
                if st.queue and st.queue[0] is step:
                    st.queue.popleft()
            return

        dispatched_at = self._loop.time()
        for st, step in chosen:
            if not (st.queue and st.queue[0] is step):
                continue
            st.queue.popleft()
            if step.on_dispatch is not None:
                _safe_call(step.on_dispatch, "on_dispatch")
            if step.motion_time > 0:
                st.busy_until = dispatched_at + step.motion_time
                if step.on_complete is not None:
                    st.pending_settled = (st.busy_until, step.on_complete)
            elif step.on_complete is not None:
                _safe_call(step.on_complete, "on_complete")

    def _fire_settled(self, now: float) -> None:
        for st in self._blinds.values():
            ps = st.pending_settled
            if ps is not None and ps[0] <= now:
                st.pending_settled = None
                _safe_call(ps[1], "on_complete")

    async def _wait(self, deadline: float | None) -> None:
        if deadline is None:
            await self._wake.wait()
            return
        timeout = deadline - self._loop.time()
        if timeout <= 0:
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return

    def _earliest_future_event(self, now: float) -> float | None:
        out: float | None = None
        for st in self._blinds.values():
            if st.queue and st.busy_until > now:
                if out is None or st.busy_until < out:
                    out = st.busy_until
            ps = st.pending_settled
            if ps is not None and ps[0] > now:
                if out is None or ps[0] < out:
                    out = ps[0]
        return out

    def _gc_anonymous(self) -> None:
        dead: list[BlindKey] = []
        for key, st in self._blinds.items():
            if st.queue or st.pending_settled is not None:
                continue
            if st.entity_ref is None or st.entity_ref() is None:
                dead.append(key)
        for k in dead:
            del self._blinds[k]


def _safe_call(fn: Callable[[], None], label: str) -> None:
    try:
        fn()
    except Exception:   # noqa: BLE001 -- caller-supplied callback
        _LOGGER.exception("scheduler %s callback raised", label)
