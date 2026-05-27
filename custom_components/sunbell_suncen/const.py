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

ACTIONS: Final = ("UP", "DOWN", "STOP", "LONG_UP", "LONG_DOWN")
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
