# The Marksman

A passive hunter for one Wi-Fi SSID or MAC, or one Bluetooth name or MAC.

The program file is **`marksman.py`**.

Named after Weber’s *Der Freischütz*. [The Magic Flute](https://github.com/scjeff/The-Magic-Flute) walks an area and writes down every radio it hears. The Marksman picks one mark and follows it. The sight picture rises, and shifts from indigo toward cyan, as the signal gets stronger.

Python 3.9+ stdlib only. No `pip` packages. Licensed under **GPL-3.0-or-later**.

## Disclaimer — authorized use only

**The Marksman reads radio emissions:** MAC addresses, SSIDs, advertised names, and signal levels. It turns signal level into a rough distance.

Use it **only** where you have legal authority, for example:

- a signed Rules of Engagement (ROE)
- written permission from the network or property owner
- your own equipment and premises
- another lawful basis that applies where you are

Unauthorized interception of communications can be a crime. You are responsible for local law. The authors assume **no liability** for misuse.

What the radios do:

- The Wi-Fi **sight** is monitor mode, receive only. It does not inject frames, deauthenticate clients, or associate.
- The Wi-Fi **list** uses a normal `iw scan`, which sends probe requests.
- Bluetooth uses BlueZ discovery (standard inquiry and LE scan, including scan requests). It does **not** pair, connect, or read GATT.

Collection starts only after you type `YES` (interactive) or pass `--i-have-roe`. That acknowledgement is stored with the session.

## What it is for

You already know which transmitter you are allowed to find. The Marksman keeps that one in view while you walk.

| You have | What you lock |
| --- | --- |
| A network name | Wi-Fi SSID. The sight follows the strongest access point using that name. |
| A radio address | Wi-Fi or Bluetooth MAC. The sight uses frames that radio transmitted. |
| A speaker, watch, phone, or tag name | Bluetooth advertised name. |

## Hardware

Any Linux host with `iw`, `ip`, `rfkill`, and BlueZ (`busctl`, which ships with systemd).

| Role | What works | Notes |
| --- | --- | --- |
| Wi-Fi | USB adapter with **monitor mode** | Preferred. MediaTek mt76 / Panda-class (`mt7921u` and similar) are marked preferred. A built-in radio is listed too. |
| Bluetooth | BlueZ `hci` controller | The built-in adapter is enough. Discovery runs through `bluetoothd`. |

The script lists whatever is plugged in and lets you pick. A soft block on the chosen radio is lifted for the hunt and put back on exit. A hardware kill switch cannot be overridden.

`sudo` is required for the Wi-Fi scan and for monitor mode.

## Setup

`--setup` downloads and installs the system packages the hunter runs. It does not install Kismet. The Marksman talks to `iw` and BlueZ directly.

| Distro | Installer | Packages |
| --- | --- | --- |
| Debian, Ubuntu, Pop!_OS, Mint, Kali | `apt` | `iw`, `iproute2`, `rfkill`, `bluez`, `network-manager`, `systemd`, `python3` |
| Fedora, RHEL, Rocky, Alma | `dnf` | `iw`, `iproute`, `util-linux`, `bluez`, `NetworkManager`, `systemd`, `python3` |
| Arch, Manjaro, EndeavourOS | `pacman` | `iw`, `iproute2`, `util-linux`, `bluez`, `bluez-utils`, `networkmanager`, `systemd`, `python` |

It also enables `bluetooth.service` and adds the login user to the `bluetooth` group when that group exists. Log out and back in after a group change. Wi-Fi monitor mode still needs `sudo`.

```bash
sudo python3 marksman.py --setup
```

## Quick start

```bash
git clone https://github.com/scjeff/The-Marksman.git
cd The-Marksman

sudo python3 marksman.py --setup
python3 marksman.py --check
sudo python3 marksman.py
```

At start the script:

1. Prints the authorized-use notice. Type `YES` only if you have lawful authorization.
2. Asks for **operator name**, **UserID or employee ID**, and **scan name**.
3. Asks whether this hunt is **Wi-Fi** or **Bluetooth**.
4. Lists controllers for that radio and asks you to pick one. USB monitor-capable Wi-Fi adapters are marked preferred.
5. Listens, then shows the target page.
6. Opens the sight picture for the mark you chose. Press **Ctrl-C**, **q**, or **b** to leave it.

Non-interactive:

```bash
sudo python3 marksman.py \
  --i-have-roe \
  --operator-name "Jane Doe" \
  --userid jsmith \
  --scan-name "Warehouse 4 north wing" \
  --phy wifi \
  --iface wlx9cefd5f63b21 \
  --ssid "Warehouse sensors" \
  --duration 600
```

Bluetooth by name or MAC:

```bash
sudo python3 marksman.py \
  --i-have-roe \
  --operator-name "Jane Doe" \
  --userid jsmith \
  --scan-name "Loading dock tag" \
  --phy bluetooth \
  --iface hci0 \
  --bt-name "Dock tag" \
  --scan-window 8
```

```bash
python3 marksman.py --list-only
python3 marksman.py --self-test
```

Dry run (authorization is still required; the radio is not changed and nothing is transmitted):

```bash
sudo python3 marksman.py \
  --i-have-roe \
  --operator-name "Jane Doe" \
  --userid jsmith \
  --scan-name "Lab check" \
  --phy wifi \
  --iface wlx9cefd5f63b21 \
  --ssid "Lab" \
  --dry-run
```

## Target pages

Numbers stay put when the air is quiet. Empty slots are shown as `—`. **6** and **7** do not slide up.

Wi-Fi, after you pick a controller:

| Key | Action |
| --- | --- |
| `0` | Rescan |
| `1`–`5` | The five strongest named SSIDs |
| `6` | Type an SSID |
| `7` | Type a MAC address |
| `q` | Quit |

A hidden SSID is not one of the five. Hunt it by MAC. If several access points share a name, the list keeps the strongest one, and the sight follows whichever AP with that name is loudest.

Bluetooth:

| Key | Action |
| --- | --- |
| `0` | Listen again (default 8 seconds) and redraw the list |
| `1`–`5` | The five strongest **named** devices |
| `6` | Type a device name |
| `7` | Type a MAC address |
| `q` | Quit |

A device with no name is not in the five. Hunt it by MAC. A typed name matches exactly, ignoring case, or as a substring of one advertised name. The sight then stays on that MAC.

`0` is also on the sight page. There it leaves the sight, listens again, and redraws the list.

## The sight

Locking either a Wi-Fi mark or a Bluetooth mark opens the same full-screen picture.

- The column fills **upward** as the smoothed signal gets stronger.
- Its color runs from indigo at the bottom (far) to cyan at the top (close).
- The sparkline’s right edge is now. A rising line means the mark is getting stronger. A falling line means it is getting weaker.
- **CLOSING** means the smoothed signal rose by 2 dB or more over about 3 seconds. **FALLING BACK** means it dropped by 2 dB or more. **HOLDING** means it did not. **LOST** means nothing has been heard for 3 seconds.
- The range in meters is the log-distance estimate below. The words IN THE SIGHTS / CLOSE / MID / FAR are bands on that estimate (under 2 m, under 8 m, under 20 m, and beyond).

| Key | Action |
| --- | --- |
| `0` | Rescan and return to the list |
| `b` | Back to the last list |
| `q` | Quit |

On a terminal that is not a TTY, the sight prints one status line per second instead of the full screen.

Wi-Fi with a known channel stays on that channel in monitor mode. A MAC or SSID that was not in the scan is searched by walking 2.4 GHz and, when the adapter has it, 5 GHz, then the radio stays on the channel where the mark was heard. If monitor mode is not available, the sight falls back to a slow `iw scan` about every 4 seconds.

Bluetooth keeps BlueZ discovery running and reads RSSI from the adapter. `DuplicateData` is turned off so the level can change while you walk. Discovery is stopped on exit when The Marksman was the thing that started it.

## Distance estimate

This is the same log-distance model as The Magic Flute. Signal level and distance are **not** linear. Received power falls off with the logarithm of distance:

```
RSSI = TX - FSPL(1 m) - 10 * n * log10(d)
d    = 10 ^ ((TX - FSPL(1 m) - RSSI) / (10 * n))
FSPL(1 m) = 20 * log10(f_MHz) - 27.55
```

`n` is the path loss exponent. `n = 2` is free space. Indoors, `n` is often somewhere from 2.7 to 4. Every distance in this tool uses **`n = 2.7`**. It is not a calibrated range.

Assumed transmit power, used only when the device does not advertise one between 1 and 30 dBm:

| Radio | Assumed TX |
| --- | ---: |
| Wi-Fi AP, and any SSID hunt | 20 dBm |
| Other Wi-Fi (a client MAC, until it beacons) | 15 dBm |
| Bluetooth classic (BR/EDR) | 4 dBm |
| BLE (an address type was present) | 0 dBm |

Bluetooth frequency is taken as 2402 MHz, the same stand-in The Magic Flute uses when a Bluetooth radio has no channel of its own. Wi-Fi uses the channel the frame or the scan reported.

Worked examples at `n = 2.7`:

| Case | About |
| --- | ---: |
| Wi-Fi AP, 2437 MHz, 20 dBm, −50 dBm | 12.7 m |
| BLE, 2402 MHz, 0 dBm, −60 dBm | 5.5 m |

A 10 dB error (ordinary multipath, or a phone that is not actually at the assumed power) scales the distance by about 2.3×. Read the number as near / mid / far. The column and the CLOSING / FALLING BACK line are the part to walk by. Moving the antenna a few centimeters can change the reading. The bar uses a smoothed signal so it does not flicker on every frame; the dBm figure on the left is the latest one.

## Options

| Flag | Meaning |
| --- | --- |
| `--setup` | Download and install `iw`, BlueZ, `rfkill`, and NetworkManager (needs sudo) |
| `--i-have-roe` | Confirm signed ROE / written authorization (required when not a TTY) |
| `--operator-name NAME` | Operator full name |
| `--userid ID` | UserID or employee ID |
| `--scan-name NAME` | Name of this scan |
| `--phy wifi` / `--phy bluetooth` | Skip the radio question |
| `--iface IFACE` | Skip the controller question (`wlx…` or `hci0`) |
| `--ssid NAME` | Lock this SSID (Wi-Fi) |
| `--mac AA:BB:CC:DD:EE:FF` | Lock this MAC |
| `--bt-name NAME` | Lock this Bluetooth name |
| `--duration N` | Leave the sight after N seconds |
| `--scan-window N` | Seconds to listen before the Bluetooth list (default 8) |
| `-o DIR` | Session directory (default `./marksman_<timestamp>/`) |
| `--dry-run` | Print the plan and do not touch the radio |
| `--list-only` | Print controllers and exit |
| `--check` | Print tools, controllers, and rfkill state |
| `--self-test` | Run the offline tests (distance, parsers, menu numbers, sight) |

Pass only one of `--ssid`, `--mac`, and `--bt-name`.

## Outputs

Default directory: `marksman_YYYYMMDDTHHMMSSZ/`

| File | Contents |
| --- | --- |
| `operator_session.json` | Name, UserID, scan name, authorization, adapter, target |
| `marksman_track.csv` | Sight samples while the mark is being heard. Appended and synced as the hunt runs, so a crash keeps completed rows. |
| `marksman_report.txt` | Short text report, including closest and furthest estimates |
| `marksman_report.md` | The same summary in Markdown |

`marksman_track.csv` columns include `signal_dbm`, `signal_smooth_dbm`, `distance_m`, `trend`, `tx_dbm`, `frequency_mhz`, and `path_loss_exponent` (always `2.7`). Rows are written about twice a second, and only while the mark has been heard in the last second. Quiet gaps are not filled in.

Do not commit capture output. The session directory is in `.gitignore`.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `iw`, `bluetoothctl`, or `busctl` missing | `sudo python3 marksman.py --setup` |
| `iw scan` says operation not permitted | `sudo python3 marksman.py` |
| No Wi-Fi controllers | USB adapter seated; `python3 marksman.py --check` |
| Soft-blocked | The hunt unblocks the chosen radio and blocks it again on exit |
| Hard-blocked | Flip the airplane / wireless switch |
| Monitor mode failed, slow scan instead | The driver did not expose radiotap. The sight still works, a few seconds per update |
| Bluetooth list stays empty | `rfkill`; confirm `bluetoothd` is running; press `0` to listen again; use `7` if you know the MAC |
| Sight says LOST | The mark went quiet or you left its channel. `0` rescans. `b` returns to the list |
| Distance looks absurd | The assumed TX power is probably wrong. Trust CLOSING / FALLING BACK over the meter number |

## License

The Marksman is free software under the **GNU General Public License v3.0 or later** (GPL-3.0-or-later). See [LICENSE](LICENSE) for the full terms.

You may run, share, and modify it. If you distribute this program or a modified version, you must keep it under the GPL and provide the corresponding source. There is **no warranty**.

The GPL covers copyright of the software. It does **not** authorize wireless collection. You still need lawful authority (ROE or equivalent) before you hunt.

## Repository contents

```
marksman.py    # hunter, sight picture, reports (GPL-3.0-or-later)
README.md
LICENSE        # GNU GPL v3
.gitignore
```
