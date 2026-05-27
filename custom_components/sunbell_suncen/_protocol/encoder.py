"""LED-XOR effective-set + closed-form value-byte formula.

The wire format is fully derived from (remote, channels, action): no
per-code lookup table is needed. Both functions below were derived by
reverse-engineering captured bursts across every reachable LED pattern.
"""
from __future__ import annotations


def led_to_channel(led_n: int) -> int:
    """LED N -> diversification channel ((N+3) mod 8) + 1, period-8 cycle."""
    return ((led_n + 3) % 8) + 1


def effective_set(remote: str, pressed: list[int]) -> list[int]:
    """LED-XOR'd effective channel set for a (remote, pressed) press.

      pressed_mask = OR over chM in pressed of (1 << ((M-1) mod 8))
      led_mask     = 1 << (led_to_channel(N) - 1)  if remote == 'N' (N>0) else 0
      eff = pressed_mask XOR led_mask
      if pressed_mask & led_mask:    # full or partial overlap
          eff |= (led_mask << 1)     # shift in a marker at the next channel up

    `remote` is the LED-pattern identifier as a digit string '0'..'12'.
    '0' means all LEDs off (no diversification); 'N' means only LED N lit.
    """
    pressed_mask = 0
    for c in pressed:
        c_mod = ((c - 1) % 8) + 1
        pressed_mask |= (1 << (c_mod - 1))
    n = int(remote)
    if n == 0:
        led_mask = 0
    else:
        led_mask = 1 << (led_to_channel(n) - 1)
    eff = pressed_mask ^ led_mask
    if pressed_mask & led_mask:
        eff |= (led_mask << 1)
    return sorted(i + 1 for i in range(8) if eff & (1 << i))


def formula_value_byte(channels: list[int], action: str) -> int:
    """8-bit value byte at symbol positions 43..50 of the 53-symbol frame.

    value_byte = (hi_nibble << 4) | lo_nibble
    hi_nibble  = (10 - lo_bitmap) & 0xF, minus 1 if hi non-empty and set != {1..8}
    lo_nibble  = closed form over hi-channel set
    DOWN = (UP + 2) & 0xFF;  STOP = (UP + 5) & 0xFF
    LONG_UP = (UP + 4) & 0xFF;  LONG_DOWN = (UP + 6) & 0xFF
    """
    folded = [((c - 1) % 8) + 1 for c in channels]
    lo = tuple(sorted(c for c in set(folded) if c <= 4))
    hi = tuple(sorted(c for c in set(folded) if c >= 5))
    lo_bitmap = sum(1 << (c - 1) for c in lo)
    base = (10 - lo_bitmap) & 0xF
    all_eight = lo == (1, 2, 3, 4) and hi == (5, 6, 7, 8)
    partial = bool(hi) and not all_eight
    hi_nib = base - (1 if partial else 0)
    if not hi:
        lo_nib = 0
    else:
        min_hi = min(hi)
        v = (0xF << (min_hi - 5)) & 0xF
        for c in hi:
            if c != min_hi:
                v &= ~(1 << (c - 5)) & 0xF
        lo_nib = v
    up = (hi_nib << 4) | lo_nib
    return {"UP": up,
            "DOWN": (up + 2) & 0xFF,
            "STOP": (up + 5) & 0xFF,
            "LONG_UP": (up + 4) & 0xFF,
            "LONG_DOWN": (up + 6) & 0xFF}[action]


def compute_code_from_formula(channels: list[int], action: str) -> tuple[int, str]:
    """(tail16, trailing_bits) derived purely from the closed form.

    The wire bits AFTER the anchor encode the 12-symbol stream:
        "10" + value_byte (8 syms) + "11"
    via the 2-bit prefix code (sym '1' -> "00", sym '0' -> "1").
    Sliced into a 16-bit tail + remaining trailing; '0'-padded if needed.
    """
    vb = formula_value_byte(channels, action)
    syms = "10" + format(vb, "08b") + "11"
    wire = "".join("00" if s == "1" else "1" for s in syms)
    if len(wire) < 16:
        wire += "0" * (16 - len(wire))
    return int(wire[:16], 2), wire[16:]


def lookup_code(remote: str, channels: list[int], action: str) -> tuple[int, str]:
    """(tail16, trailing_bits) for (remote, channels, action)."""
    return compute_code_from_formula(effective_set(remote, channels), action)
