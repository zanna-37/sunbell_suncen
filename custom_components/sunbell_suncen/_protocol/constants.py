"""Wire-format and protocol constants (verified by direct CC1101 RX)."""

ANCHOR = "00100001"
ACTION_TAG = {"UP": "000011", "DOWN": "00111", "STOP": "11001",
              "LONG_UP": "10011", "LONG_DOWN": "1111"}
ACTION_TAG_REMNANT = {"STOP": "001", "LONG_UP": "0011", "LONG_DOWN": "111"}
ACTION_BY_TAG = {tag: name for name, tag in ACTION_TAG.items()}

SHORT_MARK_US = 455
LONG_MARK_US = 960
SHORT_SPACE_US = 540
LONG_SPACE_US = 1040

# Burst envelope -- frame[0] is a 20ms wake mark (AGC), [1] a 1.5ms space,
# [2] a 1.5ms mark; remaining pulses are short/long marks/spaces per wire
# bit. Inter-frame gap is a 20ms space.
WAKE_MARK_US = 20000
LEAD_SPACE_US = 1500
LEAD_MARK_US = 1500
INTER_FRAME_GAP_US = 20000

# Note: the SUNCEN centralinas need ~500 ms of dead air after every burst
# to commit. That release-silence window MUST be enforced device-side by
# the firmware (e.g. a trailing `delay: 500ms` in the transmit script);
# the plugin assumes this is in place and fires bursts back-to-back.

# Frame structure (53-symbol frame; see synth.build_symbol_frame).
PHYSICAL_BURST_PULSES = 384
# Long-press codes (LONG_UP / LONG_DOWN) need a sustained burst for the
# centralina to enter slow-movement / slat-tilt mode.
LONG_PRESS_BURST_PULSES = PHYSICAL_BURST_PULSES * 8
FRAME_SYMBOLS = 53
ACTION_SYMBOLS = {"UP": "110", "DOWN": "100", "STOP": "001",
                  "LONG_UP": "010", "LONG_DOWN": "000"}
ANCHOR_SYMBOLS = "01011010"
