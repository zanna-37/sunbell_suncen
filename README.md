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

`send_group` accepts the same Home-Assistant cover commands you'd use on a
single blind — `open`, `close`, `stop`, `set_tilt_position` — and applies
them to a group in one shot. The integration translates the high-level
request into the most efficient SUNCEN burst sequence for the targeted
channels, then updates each grouped entity's optimistic state to match.

| Action               | Bursts                                                                                 |
| -------------------- | -------------------------------------------------------------------------------------- |
| `open`               | 1 (`UP`), per remote                                                                   |
| `close`              | 1 (`DOWN`), per remote                                                                 |
| `stop`               | 1 (`STOP`), per remote                                                                 |
| `set_tilt_position`  | `max(deltas_up)` `LONG_UP` bursts + `max(deltas_down)` `LONG_DOWN` bursts, per remote |

Tilt details: `tilt_position` is mapped to a discrete level 1..7. The
scheduler walks the lowest start level toward the target, merging in each
other channel as it reaches its starting level — channels at deeper start
levels receive bursts only once the wave reaches them.

Example: four blinds on remote 2 selected, target level 6 — starts 3, 4,
7, 5 on channels 1, 5, 8, 9:

| Step | Burst                  | After                          |
| ---- | ---------------------- | ------------------------------ |
| 1    | `LONG_UP`   → `[1]`    | ch 1 → 4                       |
| 2    | `LONG_UP`   → `[1, 5]` | ch 1, 5 → 5                    |
| 3    | `LONG_UP`   → `[1, 5, 9]` | ch 1, 5, 9 → 6              |
| 4    | `LONG_DOWN` → `[8]`    | ch 8 → 6                       |

Four bursts total. Channels at the target already emit nothing. No
anchoring — the integration trusts each blind's recorded tilt level.
Because the centralina only tilts when `LONG_` bursts match the motor's
last fast direction, do an `open` or `close` first if a blind's tilt has
drifted out of sync.

Pick the cover entities — or whole remote devices — you want to move
together; the integration groups them by remote and emits one burst sequence
per group.

```yaml
action: sunbell_suncen.send_group
target:
  entity_id:
    - cover.sunbell_suncen_remote_2_ch1
    - cover.sunbell_suncen_remote_2_ch2
    - cover.sunbell_suncen_remote_2_ch6
data:
  action: set_tilt_position
  tilt_position: 50
```

```yaml
action: sunbell_suncen.send_group
target:
  device_id: <sunbell-remote-2-device>
data:
  action: close
```

## Editing a remote's channels

To add or remove channels on an existing remote, go to **Settings → Devices &
Services → Sunbell SUNCEN → Configure → Edit a remote's channels**, pick the
remote, and enter the full channel list you want (e.g. `1,3,5-8`). Channels you
keep retain their name and any per-blind travel-time override; newly added
channels get a default name; channels you drop have their cover entities removed
from the registry on the next reload.

## Removing a remote

Delete a configured remote either from **Settings → Devices & Services →
Sunbell SUNCEN → Configure → Remove a remote**, or directly from the
device card's Delete button. Both paths drop the remote from the entry's
options and clean up the device + its cover entities from the registries.

## License

MIT.
