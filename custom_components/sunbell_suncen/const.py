"""Constants for the Sunbell SUNCEN integration."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "sunbell_suncen"

MANUFACTURER: Final = "Sunbell"
MODEL: Final = "SUNCEN"

# --- config entry data / options keys --------------------------------------
CONF_TRANSMIT_SERVICE: Final = "transmit_service"   # "<device>_transmit_raw"
CONF_REMOTES: Final = "remotes"
CONF_REMOTE_ID: Final = "remote_id"                 # "0".."12"
CONF_BLINDS: Final = "blinds"
CONF_CHANNEL: Final = "channel"                     # 1..12
CONF_NAME: Final = "name"
CONF_FULL_MOVEMENT_TIME: Final = "full_movement_time"   # seconds; entry default or per-blind override

# --- services --------------------------------------------------------------
SERVICE_SEND_GROUP: Final = "send_group"
SERVICE_SEND_GROUP_RAW: Final = "send_group_raw"
CONF_REMOTE: Final = "remote"
CONF_CHANNELS: Final = "channels"
CONF_ACTION: Final = "action"
CONF_TILT_POSITION: Final = "tilt_position"

# HA-level commands exposed by send_group. The integration translates each
# into the most efficient SUNCEN burst plan for the targeted group.
ACTION_OPEN: Final = "open"
ACTION_CLOSE: Final = "close"
ACTION_STOP: Final = "stop"
ACTION_SET_TILT_POSITION: Final = "set_tilt_position"
ACTIONS: Final = (ACTION_OPEN, ACTION_CLOSE, ACTION_STOP, ACTION_SET_TILT_POSITION)

# Raw SUNCEN burst names accepted by send_group_raw. These are passed straight
# through to the encoder; the integration emits the burst and invalidates the
# tilt (and position, for UP/DOWN/STOP) on the targeted blinds without any
# further state tracking.
RAW_UP: Final = "UP"
RAW_DOWN: Final = "DOWN"
RAW_STOP: Final = "STOP"
RAW_LONG_UP: Final = "LONG_UP"
RAW_LONG_DOWN: Final = "LONG_DOWN"
RAW_ACTIONS: Final = (RAW_UP, RAW_DOWN, RAW_STOP, RAW_LONG_UP, RAW_LONG_DOWN)
RAW_INVALIDATES_POSITION: Final = frozenset({RAW_UP, RAW_DOWN, RAW_STOP})

REMOTES: Final = tuple(str(i) for i in range(13))   # "0".."12"

# --- tilt model ------------------------------------------------------------
# Linear map: tilt_position (0..100) -> tilt_level (1..7).
#   0   -> 1  (closed one way)
#   50  -> 4  (open / straight)
#   100 -> 7  (closed the other way)
#
# Hardware quirk: when the motor reaches a mechanical limit it reverses by a
# small amount, which inverts the slats from their during-motion orientation.
# So the resting tilt after a fast UP/DOWN is the OPPOSITE extreme from the
# direction of travel — captured by the anchor constants below.
#
# Tilt strategy: HA tilt commands always anchor at full-down first (tilt_level
# == TILT_LEVEL_DOWN_ANCHOR after the motor's reversal), then walk LONG_DOWN
# toward the target. The blind must complete a full DOWN cycle (waiting
# `full_movement_time` seconds) before the tilt level is considered known and
# LONG_DOWN deltas are safe to apply.
TILT_LEVELS: Final = 7
TILT_LEVEL_DOWN_ANCHOR: Final = 7   # post-fast-DOWN resting tilt (reversal inverts slats)
TILT_LEVEL_UP_ANCHOR: Final = 1     # post-fast-UP resting tilt   (reversal inverts slats)

# --- restored state attributes ---------------------------------------------
ATTR_TILT_LEVEL: Final = "tilt_level"           # 1..7 or None (unknown)
ATTR_POSITION_INTERNAL: Final = "position_internal"  # 0 | 100 | None

# --- timing ----------------------------------------------------------------
# Default seconds to wait after a fast UP/DOWN before treating the blind as
# settled at its end-state anchor. Each blind can override via per-blind
# config; the integration entry has its own default that supersedes this
# module-level fallback.
DEFAULT_FULL_MOVEMENT_TIME: Final = 30

# Minimum dead-air time between consecutive bursts, in seconds. The SUNCEN
# centralina requires this gap to commit each command; the integration paces
# its outgoing bursts so the ESPHome on-board queue stays effectively empty.
BURST_GAP_SECONDS: Final = 0.5
