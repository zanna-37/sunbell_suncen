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

# --- send_group service ----------------------------------------------------
SERVICE_SEND_GROUP: Final = "send_group"
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

REMOTES: Final = tuple(str(i) for i in range(13))   # "0".."12"

# --- tilt model ------------------------------------------------------------
# Linear map: tilt_position (0..100) -> tilt_level (1..7).
#   0   -> 1  (closed one way)
#   50  -> 4  (open / straight)
#   100 -> 7  (closed the other way)
TILT_LEVELS: Final = 7
TILT_LEVEL_DOWN_ANCHOR: Final = 1   # post-fast-DOWN resting tilt
TILT_LEVEL_UP_ANCHOR: Final = 7     # post-fast-UP resting tilt
TILT_EXTREMES: Final = frozenset({1, TILT_LEVELS})

# --- restored state attributes ---------------------------------------------
ATTR_LAST_DIRECTION: Final = "last_direction"   # "UP" | "DOWN" | None
ATTR_TILT_LEVEL: Final = "tilt_level"           # 1..7
ATTR_POSITION_INTERNAL: Final = "position_internal"  # 0 | 100 | None

# --- transmit pacing -------------------------------------------------------
# Minimum dead-air time between consecutive bursts, in seconds. The SUNCEN
# centralina requires this gap to commit each command; the integration paces
# its outgoing bursts so the ESPHome on-board queue stays effectively empty.
BURST_GAP_SECONDS: Final = 0.5
