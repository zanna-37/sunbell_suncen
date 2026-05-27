"""SUNCEN protocol implementation.

LED-XOR diversification + closed-form value-byte formula + 53-symbol
frame synthesis. Only the TX-side surface is included (no RX decoder,
no CLI), since the integration only needs to emit bursts.

Surfaces used by the integration:
- encoder.effective_set, formula_value_byte, lookup_code
- synth.build_symbol_burst_signed
- channels.parse_channels, parse_action (for the send_group service)
"""
from .encoder import (
    compute_code_from_formula,
    effective_set,
    formula_value_byte,
    led_to_channel,
    lookup_code,
)
from .synth import build_symbol_burst_signed

__all__ = [
    "build_symbol_burst_signed",
    "compute_code_from_formula",
    "effective_set",
    "formula_value_byte",
    "led_to_channel",
    "lookup_code",
]
