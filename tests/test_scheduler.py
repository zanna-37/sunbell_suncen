"""Unit tests for the reactive coalescing burst scheduler.

The scheduler is exercised against a fake transmit recorder and a fake burst
builder, so no Home Assistant runtime or RF pulses are required. Tests use
sub-second `motion_time` values to keep the suite fast.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

import sys

_sched_mod = sys.modules["sunbell_scheduler_under_test"]
BurstScheduler = _sched_mod.BurstScheduler
BurstStep = _sched_mod.BurstStep


# A burst builder that records (remote, channels, action) and returns a tagged
# pulse list so we can verify the FakeTransmit got what we expected without
# decoding signed-microsecond pulses.
@dataclass
class FakeBuilder:
    calls: list[tuple[str, tuple[int, ...], str]] = field(default_factory=list)

    def __call__(self, remote: str, channels: list[int], action: str) -> list[int]:
        self.calls.append((remote, tuple(channels), action))
        return [len(self.calls)]   # sentinel; never decoded


@dataclass
class FakeTransmit:
    loop: asyncio.AbstractEventLoop
    sends: list[tuple[float, list[int]]] = field(default_factory=list)
    on_air: float = 0.0
    raise_on_call: int | None = None
    pause: asyncio.Event | None = None
    _calls: int = 0

    async def send(self, pulses: list[int]) -> None:
        self._calls += 1
        if self.raise_on_call == self._calls:
            raise RuntimeError("simulated transmit failure")
        if self.pause is not None:
            await self.pause.wait()
        ts = self.loop.time()
        if self.on_air > 0:
            await asyncio.sleep(self.on_air)
        self.sends.append((ts, list(pulses)))


@pytest.fixture
async def env():
    loop = asyncio.get_running_loop()
    builder = FakeBuilder()
    transmit = FakeTransmit(loop=loop)
    sched = BurstScheduler(loop, transmit, builder)
    sched.start()
    try:
        yield sched, transmit, builder
    finally:
        await sched.async_close()


async def _drain(loop: asyncio.AbstractEventLoop, ticks: int = 5) -> None:
    """Let the scheduler run a few event-loop iterations."""
    for _ in range(ticks):
        await asyncio.sleep(0)


# --- helpers ---------------------------------------------------------------

def step(
    blind: tuple[str, int],
    action: str,
    *,
    motion_time: float = 0.0,
    on_dispatch=None,
    on_complete=None,
) -> BurstStep:
    return BurstStep(
        blind_key=blind,
        action=action,
        motion_time=motion_time,
        on_dispatch=on_dispatch,
        on_complete=on_complete,
        enqueued_at=0.0,
    )


def by_action(builder: FakeBuilder) -> list[tuple[str, tuple[int, ...], str]]:
    return list(builder.calls)


# --- tests -----------------------------------------------------------------

async def test_merge_same_action_heads(env):
    """Two blinds same remote, same action -> one merged burst on channel union."""
    sched, transmit, builder = env
    fired = []
    sched.submit(("0", 1), [step(("0", 1), "LONG_DOWN", on_complete=lambda: fired.append(1))])
    sched.submit(("0", 2), [step(("0", 2), "LONG_DOWN", on_complete=lambda: fired.append(2))])
    await asyncio.sleep(0.05)
    assert builder.calls == [("0", (1, 2), "LONG_DOWN")]
    assert sorted(fired) == [1, 2]
    assert len(transmit.sends) == 1


async def test_bin_by_remote_and_action(env):
    """Same action on different remotes -> two bursts; same remote merges."""
    sched, transmit, builder = env
    sched.submit(("0", 1), [step(("0", 1), "LONG_DOWN")])
    sched.submit(("0", 2), [step(("0", 2), "LONG_DOWN")])
    sched.submit(("1", 3), [step(("1", 3), "LONG_DOWN")])
    await asyncio.sleep(0.05)
    actions = {(remote, action) for remote, _, action in builder.calls}
    assert actions == {("0", "LONG_DOWN"), ("1", "LONG_DOWN")}
    by_remote = {remote: channels for remote, channels, _ in builder.calls}
    assert by_remote["0"] == (1, 2)
    assert by_remote["1"] == (3,)


async def test_opposite_directions_do_not_merge(env):
    """LONG_UP on A and LONG_DOWN on B on the same remote -> two separate bursts."""
    sched, transmit, builder = env
    sched.submit(("0", 1), [step(("0", 1), "LONG_UP")])
    sched.submit(("0", 2), [step(("0", 2), "LONG_DOWN")])
    await asyncio.sleep(0.05)
    actions = {(remote, action) for remote, _, action in builder.calls}
    assert actions == {("0", "LONG_UP"), ("0", "LONG_DOWN")}


async def test_chain_pacing(env):
    """[DOWN(motion=0.1), LONG_DOWN, LONG_DOWN] -> LONG_DOWNs only after busy_until."""
    sched, transmit, builder = env
    loop = asyncio.get_running_loop()
    sched.submit(
        ("0", 1),
        [
            step(("0", 1), "DOWN", motion_time=0.1),
            step(("0", 1), "LONG_DOWN"),
            step(("0", 1), "LONG_DOWN"),
        ],
    )
    t0 = loop.time()
    await asyncio.sleep(0.2)
    assert len(builder.calls) == 3
    assert builder.calls[0][2] == "DOWN"
    assert builder.calls[1][2] == "LONG_DOWN"
    assert builder.calls[2][2] == "LONG_DOWN"
    # the two LONG_DOWN bursts must have happened >= 0.1s after t0
    assert transmit.sends[1][0] - t0 >= 0.09  # small tolerance for clock granularity
    assert transmit.sends[2][0] - t0 >= 0.09


async def test_preempt_mid_chain_drops_remaining():
    """Preempting a tilt walk drops the rest; new chain takes over."""
    loop = asyncio.get_running_loop()
    builder = FakeBuilder()
    # Use on_air so each send takes meaningful time -- gives the test a window
    # to inject a preempt before the whole chain drains.
    transmit = FakeTransmit(loop=loop, on_air=0.04)
    sched = BurstScheduler(loop, transmit, builder)
    sched.start()
    try:
        walked = []
        sched.submit(
            ("0", 1),
            [
                step(("0", 1), "LONG_DOWN", on_complete=lambda: walked.append("L1")),
                step(("0", 1), "LONG_DOWN", on_complete=lambda: walked.append("L2")),
                step(("0", 1), "LONG_DOWN", on_complete=lambda: walked.append("L3")),
            ],
        )
        # Let ~one LONG_DOWN dispatch.
        await asyncio.sleep(0.05)
        # Preempt: queue [UP].
        sched.submit(("0", 1), [step(("0", 1), "UP", motion_time=0.05)])
        await asyncio.sleep(0.2)
        actions = [a for _, _, a in builder.calls]
        assert actions[-1] == "UP"
        # At most one LONG_DOWN should have committed its on_complete; the rest
        # were dropped before they could dispatch.
        assert len(walked) <= 1
    finally:
        await sched.async_close()


async def test_preempt_during_wire_gap_drops_next_step_before_wire():
    """A preempt landing during the scheduler-side wire gap drops the next
    tilt step before it reaches the wire. Without scheduler-owned pacing
    (gap inside TransmitQueue), the next LONG_DOWN would already be
    committed when the preempt arrives, the wire would emit one more tilt
    step, and the physical blind would advance one extra notch before
    moving up."""
    loop = asyncio.get_running_loop()
    builder = FakeBuilder()
    transmit = FakeTransmit(loop=loop)
    # Realistic-ish gap, scaled down so the test stays fast.
    sched = BurstScheduler(loop, transmit, builder, wire_gap_seconds=0.1)
    sched.start()
    try:
        walked = []
        sched.submit(
            ("0", 1),
            [
                step(("0", 1), "LONG_DOWN", on_complete=lambda: walked.append("L1")),
                step(("0", 1), "LONG_DOWN", on_complete=lambda: walked.append("L2")),
                step(("0", 1), "LONG_DOWN", on_complete=lambda: walked.append("L3")),
            ],
        )
        # Let the first LONG_DOWN dispatch; the second is now held in the
        # scheduler queue, paced behind the 0.1s wire gap.
        await asyncio.sleep(0.03)
        sched.submit(("0", 1), [step(("0", 1), "UP", motion_time=0.05)])
        # Wait long enough for the wire-gap window to pass and UP to fire.
        await asyncio.sleep(0.3)
        actions = [a for _, _, a in builder.calls]
        # Exactly one LONG_DOWN reaches the wire (the one that had already
        # dispatched), then UP. No second LONG_DOWN snuck through the gap.
        assert actions == ["LONG_DOWN", "UP"]
        assert walked == ["L1"]
    finally:
        await sched.async_close()


async def test_stop_submit_mid_motion_is_legitimate_override(env):
    """STOP is the only command the centralina honors mid-fast-motion, so a
    STOP submit dispatches directly without an auto-prepended STOP prefix.
    Pending settled callbacks from the preempted motion are dropped."""
    sched, transmit, builder = env
    settled = []
    sched.submit(
        ("0", 1),
        [step(("0", 1), "DOWN", motion_time=0.2,
              on_complete=lambda: settled.append("anchor"))],
    )
    await asyncio.sleep(0.02)
    assert builder.calls[-1][2] == "DOWN"
    sched.submit(("0", 1), [step(("0", 1), "STOP")])
    await asyncio.sleep(0.05)
    actions = [a for _, _, a in builder.calls]
    assert actions == ["DOWN", "STOP"]
    # The pending anchor commit was cancelled.
    await asyncio.sleep(0.3)   # long enough that the original 0.2 settle would have fired
    assert settled == []


async def test_preempt_during_transmit_await_skips_callbacks_and_locks_out():
    """Two behaviors composed: a submit landing while transmit.send is in
    flight must (a) skip the in-flight step's callbacks via the queue-head
    identity check, and (b) trigger the hardware lockout -- because the
    in-flight step was a fast UP and the new chain is non-STOP, the
    scheduler must auto-prepend STOP. Wire ends up as [UP, STOP, DOWN].

    This also covers the pre-await busy_until set: without it the lockout
    check in submit() would see busy_until=0 (the old code only set
    busy_until after the await returned) and slip the new chain through."""
    loop = asyncio.get_running_loop()
    builder = FakeBuilder()
    transmit = FakeTransmit(loop=loop, pause=asyncio.Event())
    sched = BurstScheduler(loop, transmit, builder)
    sched.start()
    try:
        dispatched = []
        completed = []
        sched.submit(
            ("0", 1),
            [step(("0", 1), "UP", motion_time=0.05,
                  on_dispatch=lambda: dispatched.append("old"),
                  on_complete=lambda: completed.append("old"))],
        )
        # Let the worker pick the step into a sweep and enter the await.
        await _drain(loop, ticks=3)
        # The worker is now paused inside transmit.send. busy_until for this
        # blind has already been set (pre-await), so submit() will see the
        # lockout.
        sched.submit(
            ("0", 1),
            [step(("0", 1), "DOWN", motion_time=0.05,
                  on_dispatch=lambda: dispatched.append("new"),
                  on_complete=lambda: completed.append("new"))],
        )
        # Release the transmit. The old step's callbacks should be skipped.
        transmit.pause.set()
        await asyncio.sleep(0.2)
        assert "old" not in dispatched
        assert "old" not in completed
        assert "new" in dispatched
        assert "new" in completed
        assert [a for _, _, a in builder.calls] == ["UP", "STOP", "DOWN"]
    finally:
        await sched.async_close()


async def test_fairness_oldest_head_wins(env):
    """Oldest enqueued_at wins. Submit A first; later submit a larger bin B.
    A should still dispatch first."""
    sched, transmit, builder = env
    sched.submit(("0", 1), [step(("0", 1), "LONG_DOWN")])
    # Force a tiny gap so enqueued_at differs.
    await asyncio.sleep(0.005)
    sched.submit(("0", 2), [step(("0", 2), "LONG_UP")])
    sched.submit(("0", 3), [step(("0", 3), "LONG_UP")])
    await asyncio.sleep(0.05)
    # A's LONG_DOWN must dispatch before the LONG_UP bin.
    actions = [a for _, _, a in builder.calls]
    assert actions.index("LONG_DOWN") < actions.index("LONG_UP")


async def test_fairness_tiebreak_larger_bin(env):
    """Identical enqueued_at -> larger bin wins."""
    sched, transmit, builder = env
    # Submit all in the same tick so enqueued_at is identical.
    sched.submit(("0", 1), [step(("0", 1), "LONG_UP")])
    sched.submit(("0", 2), [step(("0", 2), "LONG_DOWN")])
    sched.submit(("0", 3), [step(("0", 3), "LONG_DOWN")])
    await asyncio.sleep(0.05)
    actions = [a for _, _, a in builder.calls]
    assert actions[0] == "LONG_DOWN"   # larger bin first


async def test_deferred_on_complete_fires_after_motion_time(env):
    """on_complete for UP/DOWN fires after busy_until expires."""
    sched, transmit, builder = env
    loop = asyncio.get_running_loop()
    when = []
    sched.submit(
        ("0", 1),
        [step(("0", 1), "DOWN", motion_time=0.1,
              on_complete=lambda: when.append(loop.time()))],
    )
    t0 = loop.time()
    await asyncio.sleep(0.05)
    assert when == []   # not yet
    await asyncio.sleep(0.1)
    assert len(when) == 1
    assert when[0] - t0 >= 0.09


async def test_pending_settled_replaced_by_next_chain(env):
    """A second fresh chain on the same blind cancels the first's settled cb."""
    sched, transmit, builder = env
    fired = []
    sched.submit(
        ("0", 1),
        [step(("0", 1), "DOWN", motion_time=0.1,
              on_complete=lambda: fired.append("first"))],
    )
    await asyncio.sleep(0.02)
    sched.submit(
        ("0", 1),
        [step(("0", 1), "DOWN", motion_time=0.1,
              on_complete=lambda: fired.append("second"))],
    )
    await asyncio.sleep(0.3)
    assert fired == ["second"]


async def test_dispatch_failure_drops_step(env):
    """If transmit.send raises, the step is dropped without firing callbacks."""
    sched, transmit, builder = env
    transmit.raise_on_call = 1
    callbacks = []
    sched.submit(
        ("0", 1),
        [step(("0", 1), "DOWN", motion_time=0.05,
              on_dispatch=lambda: callbacks.append("d"),
              on_complete=lambda: callbacks.append("c"))],
    )
    await asyncio.sleep(0.1)
    # Builder was called (the step was selected), but callbacks didn't fire.
    assert builder.calls == [("0", (1,), "DOWN")]
    assert callbacks == []
    # Next submit on the same blind still works.
    transmit.raise_on_call = None
    sched.submit(("0", 1), [step(("0", 1), "STOP", on_complete=lambda: callbacks.append("stop"))])
    await asyncio.sleep(0.05)
    assert callbacks == ["stop"]


async def test_anonymous_state_gc(env):
    """Anonymous (entity=None) BlindStates are GC'd once the queue empties."""
    sched, transmit, builder = env
    sched.submit(("0", 9), [step(("0", 9), "DOWN", motion_time=0.05)], entity=None)
    await asyncio.sleep(0.15)   # wait long enough for the settle + GC pass
    assert ("0", 9) not in sched._blinds   # noqa: SLF001 -- internal check


async def test_shutdown_before_dispatch():
    """Closing before the first burst dispatches must not fire callbacks
    and must not raise."""
    loop = asyncio.get_running_loop()
    builder = FakeBuilder()
    transmit = FakeTransmit(loop=loop, pause=asyncio.Event())   # holds first send
    sched = BurstScheduler(loop, transmit, builder)
    sched.start()
    fired = []
    sched.submit(
        ("0", 1),
        [step(("0", 1), "UP", motion_time=0.05,
              on_complete=lambda: fired.append("up"))],
    )
    await _drain(loop, ticks=2)   # let the worker enter transmit.send
    await sched.async_close()
    assert fired == []


async def test_lockout_injects_stop_for_non_stop_submit_mid_motion(env):
    """Submitting a non-STOP chain while a fast UP/DOWN is mid-motion makes
    the scheduler inject a STOP ahead of the new chain. Hardware constraint:
    the centralina silently drops any non-STOP command received while a
    motor cycle is in progress."""
    sched, transmit, builder = env
    loop = asyncio.get_running_loop()

    sched.submit(("0", 1), [step(("0", 1), "UP", motion_time=0.2)])
    await _drain(loop, ticks=3)
    # UP has dispatched; busy_until is set. Blind is locked.
    assert [a for _, _, a in builder.calls] == ["UP"]

    sched.submit(("0", 1), [step(("0", 1), "DOWN", motion_time=0.05)])
    await asyncio.sleep(0.1)

    assert [a for _, _, a in builder.calls] == ["UP", "STOP", "DOWN"]


async def test_opportunistic_merge_when_walk_catches_up(env):
    """r0ch0 at level 4 (known), r0ch1 unknown.

    Walk plan (mirrors cover.build_tilt_chain):
      - r0ch1 unknown -> 3: [DOWN(anchor->7), LONG_DOWN(->6/5/4/3)]
      - r0ch0      4 -> 3: [LONG_DOWN(->3)]

    Submitting r0ch0 the moment r0ch1 commits to level 4 (via piggybacking on
    that step's on_complete -- avoids an event-loop race that would let the
    final LONG_DOWN dispatch solo before our submit lands) makes both blinds'
    queue heads LONG_DOWN at the next tick, so the scheduler emits a single
    merged burst on channels (0, 1) for the 4->3 step.
    """
    sched, transmit, builder = env

    r0, r1 = ("0", 0), ("0", 1)
    state: dict[tuple[str, int], int | None] = {r0: 4, r1: None}

    def commit(key, level):
        state[key] = level

    def commit_r1_to_4_and_submit_r0():
        commit(r1, 4)
        sched.submit(
            r0,
            [step(r0, "LONG_DOWN", on_complete=lambda: commit(r0, 3))],
        )

    sched.submit(
        r1,
        [
            step(r1, "DOWN", motion_time=0.05, on_complete=lambda: commit(r1, 7)),
            step(r1, "LONG_DOWN", on_complete=lambda: commit(r1, 6)),
            step(r1, "LONG_DOWN", on_complete=lambda: commit(r1, 5)),
            step(r1, "LONG_DOWN", on_complete=commit_r1_to_4_and_submit_r0),
            step(r1, "LONG_DOWN", on_complete=lambda: commit(r1, 3)),
        ],
    )

    await asyncio.sleep(0.2)

    assert state[r0] == 3
    assert state[r1] == 3

    seq = [(c, a) for _, c, a in builder.calls]
    assert seq == [
        ((1,), "DOWN"),
        ((1,), "LONG_DOWN"),
        ((1,), "LONG_DOWN"),
        ((1,), "LONG_DOWN"),
        ((0, 1), "LONG_DOWN"),
    ]


async def test_two_unknown_same_down_time_merges_full_chain(env):
    """Both blinds unknown, both tilting to level 3, identical DOWN motion_time.

    Each blind's chain is [DOWN(motion_time=T), LONG_DOWN x4] (7 -> 3).
    Because the anchor DOWN takes the same time on both, their busy_until
    expire together, so every step -- the anchor DOWN and all four LONG_DOWNs
    -- coalesces into one merged burst on channels (0, 1).
    """
    sched, transmit, builder = env
    motion_time = 0.05

    def chain(key):
        return [
            step(key, "DOWN", motion_time=motion_time),
            step(key, "LONG_DOWN"),
            step(key, "LONG_DOWN"),
            step(key, "LONG_DOWN"),
            step(key, "LONG_DOWN"),
        ]

    sched.submit(("0", 0), chain(("0", 0)))
    sched.submit(("0", 1), chain(("0", 1)))

    await asyncio.sleep(0.2)

    seq = [(c, a) for _, c, a in builder.calls]
    assert seq == [
        ((0, 1), "DOWN"),
        ((0, 1), "LONG_DOWN"),
        ((0, 1), "LONG_DOWN"),
        ((0, 1), "LONG_DOWN"),
        ((0, 1), "LONG_DOWN"),
    ]


async def test_two_unknown_different_down_time_only_anchor_merges(env):
    """Both blinds unknown, both tilting to level 3, DIFFERENT DOWN motion_times.

    The anchor DOWN still merges onto (0, 1) at submit time, but each blind's
    busy_until tracks its own motion_time. The faster blind leaves settle
    first and drains its four LONG_DOWNs solo before the slower blind is
    ready, so the LONG_DOWN chain never re-merges.
    """
    sched, transmit, builder = env

    def chain(key, motion_time):
        return [
            step(key, "DOWN", motion_time=motion_time),
            step(key, "LONG_DOWN"),
            step(key, "LONG_DOWN"),
            step(key, "LONG_DOWN"),
            step(key, "LONG_DOWN"),
        ]

    sched.submit(("0", 0), chain(("0", 0), motion_time=0.02))
    sched.submit(("0", 1), chain(("0", 1), motion_time=0.15))

    await asyncio.sleep(0.4)

    seq = [(c, a) for _, c, a in builder.calls]

    # Anchor DOWN is the one merged burst on both channels.
    assert seq[0] == ((0, 1), "DOWN")

    # Everything after is per-channel LONG_DOWN: ch0's four first, then ch1's four.
    long_downs = seq[1:]
    assert len(long_downs) == 8
    assert all(a == "LONG_DOWN" for _, a in long_downs)
    assert [c for c, _ in long_downs] == [(0,)] * 4 + [(1,)] * 4


async def test_stop_is_peer_not_special(env):
    """STOP follows the same busy_until rule as everything else. A STOP step
    that's NOT preceded by submit() must wait for prior motion's busy_until."""
    sched, transmit, builder = env
    loop = asyncio.get_running_loop()
    sched.submit(
        ("0", 1),
        [
            step(("0", 1), "DOWN", motion_time=0.1),
            step(("0", 1), "STOP"),
        ],
    )
    t0 = loop.time()
    await asyncio.sleep(0.2)
    assert [a for _, _, a in builder.calls] == ["DOWN", "STOP"]
    # The STOP must have been dispatched at least motion_time later than the DOWN.
    assert transmit.sends[1][0] - transmit.sends[0][0] >= 0.09
