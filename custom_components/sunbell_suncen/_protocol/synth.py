"""Pulse-level synthesis: 53-symbol frame -> wire bits -> signed pulses.

Wire encoding uses a 2-bit prefix code (sym '1' -> wire "00", sym '0' ->
wire "1"). The long envelope is 20 ms wake mark, 1.5 ms lead space, 1.5
ms lead mark, with a 20 ms inter-frame gap between frames. Short actions
pack into a 384-pulse burst (5 full + 1 truncated frame); LONG_UP /
LONG_DOWN use an 8x budget to clear the centralina's long-press dead
zone for slow-mode / slat-tilt activation.
"""
from __future__ import annotations

from .constants import (
    ACTION_SYMBOLS,
    ANCHOR_SYMBOLS,
    FRAME_SYMBOLS,
    INTER_FRAME_GAP_US,
    LEAD_MARK_US,
    LEAD_SPACE_US,
    LONG_MARK_US,
    LONG_PRESS_BURST_PULSES,
    LONG_SPACE_US,
    PHYSICAL_BURST_PULSES,
    SHORT_MARK_US,
    SHORT_SPACE_US,
    WAKE_MARK_US,
)
from .encoder import effective_set, formula_value_byte


def bits_to_pulses(bits: str) -> list[int]:
    """One pulse per bit. Even index = mark, odd = space."""
    out = []
    for i, b in enumerate(bits):
        if i % 2 == 0:
            out.append(LONG_MARK_US if b == "1" else SHORT_MARK_US)
        else:
            out.append(LONG_SPACE_US if b == "1" else SHORT_SPACE_US)
    return out


def build_symbol_frame(remote: str, channels: list[int], action: str) -> str:
    """53-symbol frame for (remote, channels, action).

    `remote` is the LED-pattern identifier as a digit string '0'..'12'.

    Layout (verified against every captured frame across 0..12):
      0..6    padding (all '0')
      7..18   LED indicator zone -- '1' at position (19-N) if N>=1
      19..30  12-bit channel bitmap of PRESSED channels (MSB first)
      31      filler '0'
      32..34  action one-hot (UP=110 / DOWN=100 / STOP=001 / LONG_UP=010 / LONG_DOWN=000)
      35..42  anchor signature "01011010"
      43..50  value byte (closed form over the EFFECTIVE set, MSB first)
      51      end-of-frame '1'
      52      parity pad '1'
    """
    syms = ["0"] * FRAME_SYMBOLS
    n = int(remote)
    if n != 0:
        syms[19 - n] = "1"
    for c in channels:
        if 1 <= c <= 12:
            syms[31 - c] = "1"
    for i, s in enumerate(ACTION_SYMBOLS[action]):
        syms[32 + i] = s
    for i, s in enumerate(ANCHOR_SYMBOLS):
        syms[35 + i] = s
    # Value byte uses the effective (LED-XOR'd) set, not the pressed channels.
    vb = formula_value_byte(effective_set(remote, channels), action)
    for i in range(8):
        syms[43 + i] = str((vb >> (7 - i)) & 1)
    syms[51] = "1"
    syms[52] = "1"
    return "".join(syms)


def symbols_to_wire(syms: str) -> str:
    """Encode symbols to wire bits: sym '1' -> "00", sym '0' -> "1"."""
    return "".join("00" if s == "1" else "1" for s in syms)


def build_symbol_burst_signed(remote: str, channels: list[int],
                              action: str) -> list[int]:
    """Long-envelope burst from the 53-symbol frame, as signed-us pulses.

    Short actions use PHYSICAL_BURST_PULSES (5-6 frames). LONG_UP / LONG_DOWN
    use LONG_PRESS_BURST_PULSES (8x) to clear the centralina's long-press
    dead zone (slow-mode / tilt activation).
    """
    wire = symbols_to_wire(build_symbol_frame(remote, channels, action))
    one_pulses = bits_to_pulses(wire)
    one_pulses[0] = WAKE_MARK_US
    one_pulses[1] = LEAD_SPACE_US
    one_pulses[2] = LEAD_MARK_US
    frame_signed = [p if (i % 2 == 0) else -p for i, p in enumerate(one_pulses)]
    flen = len(frame_signed)
    budget = (LONG_PRESS_BURST_PULSES if action.startswith("LONG_") else PHYSICAL_BURST_PULSES)
    burst: list[int] = []
    while True:
        remaining = budget - len(burst)
        if remaining <= 0:
            break
        if remaining >= flen:
            burst.extend(frame_signed)
            if budget - len(burst) > 0:
                burst.append(-INTER_FRAME_GAP_US)
        else:
            burst.extend(frame_signed[:remaining])
            break
    return burst
