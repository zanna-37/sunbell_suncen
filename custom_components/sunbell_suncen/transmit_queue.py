"""Per-entry serialized RF burst queue.

The SUNCEN centralina needs ~500 ms of dead air after each burst to commit
the command. The ESPHome side enforces this via a queued script with a
trailing delay, but the on-board queue is bounded and HA can easily out-pace
it (multiple covers, group operations, simultaneous automations). The
integration takes the queue role on itself so the ESP queue stays effectively
empty -- the script-side delay becomes a defence-in-depth safety net rather
than the primary flow control.

The pacing is "start of one dispatch to start of the next" measured as
`burst_on_air_duration + BURST_GAP_SECONDS`. The on-air duration is computed
from the signed-microsecond pulse list, which already reflects every wake
mark, lead, symbol and inter-frame gap synthesised by `_protocol.synth`.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant

from .const import BURST_GAP_SECONDS

_LOGGER = logging.getLogger(__name__)

ESPHOME_DOMAIN = "esphome"


class TransmitQueue:
    """FIFO worker that serializes RF burst dispatches to one ESPHome service."""

    def __init__(
        self,
        hass: HomeAssistant,
        service_name: str,
        *,
        gap_seconds: float = BURST_GAP_SECONDS,
    ) -> None:
        self._hass = hass
        self._service_name = service_name
        self._gap = gap_seconds
        self._queue: asyncio.Queue[tuple[list[int], asyncio.Future[None]]] = (
            asyncio.Queue()
        )
        self._worker: asyncio.Task[None] | None = None
        # Loop time at which the ESP is expected to be ready for the next
        # burst. Starts at 0 so the first send goes through immediately.
        self._earliest_next: float = 0.0

    @property
    def pending(self) -> int:
        """Bursts currently queued (does not count the one being dispatched)."""
        return self._queue.qsize()

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = self._hass.loop.create_task(
                self._run(),
                name=f"sunbell_suncen.transmit_queue[{self._service_name}]",
            )

    async def stop(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        self._worker = None
        # Drain anything still queued so awaiting callers don't hang.
        while not self._queue.empty():
            _, future = self._queue.get_nowait()
            if not future.done():
                future.cancel()
            self._queue.task_done()

    async def send(self, pulses: list[int]) -> None:
        """Enqueue a burst; return once the ESPHome service has accepted it."""
        if self._worker is None or self._worker.done():
            raise RuntimeError(
                "TransmitQueue.send called before start() (or after stop())."
            )
        future: asyncio.Future[None] = self._hass.loop.create_future()
        await self._queue.put((pulses, future))
        await future

    async def _run(self) -> None:
        while True:
            pulses, future = await self._queue.get()
            try:
                now = self._hass.loop.time()
                wait = self._earliest_next - now
                if wait > 0:
                    if self._queue.qsize() > 4:
                        _LOGGER.debug(
                            "TransmitQueue %s: %d bursts queued behind a %.2fs wait",
                            self._service_name,
                            self._queue.qsize(),
                            wait,
                        )
                    await asyncio.sleep(wait)
                try:
                    await self._hass.services.async_call(
                        ESPHOME_DOMAIN,
                        self._service_name,
                        {"code": pulses},
                        blocking=True,
                    )
                except Exception as exc:  # noqa: BLE001 - propagate to caller
                    if not future.done():
                        future.set_exception(exc)
                    _LOGGER.exception(
                        "TransmitQueue dispatch via esphome.%s failed",
                        self._service_name,
                    )
                    # Hold back briefly even on failure to avoid hammering a
                    # disconnected device.
                    self._earliest_next = self._hass.loop.time() + self._gap
                else:
                    if not future.done():
                        future.set_result(None)
                    duration_s = sum(abs(p) for p in pulses) / 1_000_000
                    self._earliest_next = (
                        self._hass.loop.time() + duration_s + self._gap
                    )
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            finally:
                self._queue.task_done()
