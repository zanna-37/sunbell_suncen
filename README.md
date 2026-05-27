# Sunbell SUNCEN — Home Assistant integration

Control [Sunbell SUNCEN](https://www.sunbell.it/) motorized venetian blinds
from Home Assistant via an ESPHome + CC1101 RF bridge.

Each configured `(remote, channel)` pair becomes one `cover` entity that
supports open / close / stop / tilt. Channels on the same remote can be
moved together in a single multi-channel RF burst via the
`sunbell_suncen.send_group` service — the centralina sees one command, not
one-per-blind.

## Requirements

- Home Assistant **2026.5** or newer.
- An ESPHome device exposing a `transmit_raw(code: int[])` user service
  that forwards a signed-µs OOK pulse list to a CC1101 (or compatible)
  sub-GHz transmitter at 433.85 MHz. The service must serialize
  back-to-back calls with at least ~500 ms of dead air between bursts —
  the SUNCEN centralina requires this gap to commit a command. A
  `mode: queued` script with a trailing `delay: 500ms` after the
  transmit step is the simplest way to guarantee this.
- The ESPHome device added to Home Assistant via the standard ESPHome
  integration. This plugin calls the device's user service through HA's
  service registry — no separate API key or host configuration needed.

## Installation (HACS)

1. Add this repository as a custom HACS integration repository.
2. Install **Sunbell SUNCEN**.
3. Restart Home Assistant.
4. Settings → Devices & Services → Add Integration → **Sunbell SUNCEN**.

## Configuration

The config flow walks through:

1. **Transmit service.** Pick the ESPHome user service that emits an RF
   burst (e.g. `esphome.esp_blinds_transmit_raw`).
2. **Remotes.** For each Sunbell remote (`0`..`12`, where `N` corresponds
   to the LED pattern with only LED N lit, and `0` means all LEDs off),
   enter the channels (1..12) you want to expose.

Add more remotes any time from the integration's options menu.

## Tilt control

Sunbell venetian lamellas have **7 discrete tilt levels**, mapped linearly
to Home Assistant's `tilt_position` (0..100):

| `tilt_position` | level | physical                |
| --------------- | ----- | ----------------------- |
| 0               | 1     | closed (one way)        |
| 17              | 2     |                         |
| 33              | 3     |                         |
| 50              | 4     | open / straight         |
| 67              | 5     |                         |
| 83              | 6     |                         |
| 100             | 7     | closed (the other way)  |

Tilt is driven by `LONG_UP` / `LONG_DOWN` bursts in the *opposite* direction
of the last fast movement. The integration tracks `last_direction` and
`tilt_level` across restarts via `RestoreEntity`. The very first tilt after
a fresh install auto-issues a short DOWN to anchor at level 1.

When the target is an extreme (level 1 or 7), the integration sends one
extra LONG_ step beyond the computed delta to clear any motor desync
against the mechanical limit.

## Service: `sunbell_suncen.send_group`

```yaml
service: sunbell_suncen.send_group
data:
  remote: "2"
  channels: [1, 2, 6]
  action: DOWN     # UP | DOWN | STOP | LONG_UP | LONG_DOWN
```

The integration synthesizes a single RF burst that addresses all listed
channels at once.

## License

MIT.
