#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
The Marksman — lock onto one Wi-Fi SSID or MAC, or one Bluetooth name or MAC,
and watch the range estimate move as you walk.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

Authorized use only. No injection, deauthentication, association,
Bluetooth pairing, connect, or GATT. Distance uses the same log-distance
model as The Magic Flute (n = 2.7).

    sudo python3 marksman.py --setup
    sudo python3 marksman.py
"""

from __future__ import annotations

import argparse
import csv
import grp
import json
import math
import os
import pwd
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import termios
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


TOOL_NAME = "The Marksman"
VERSION = "1.0.0"
# Binaries the hunt calls. nmcli is used when NetworkManager is present.
REQUIRED_TOOLS = ("iw", "ip", "rfkill", "busctl", "bluetoothctl")
OPTIONAL_TOOLS = ("nmcli",)
# Package names that provide those binaries. systemd provides busctl.
DEB_PACKAGES = ["iw", "iproute2", "rfkill", "bluez", "network-manager", "systemd", "python3"]
FEDORA_PACKAGES = ["iw", "iproute", "util-linux", "bluez", "NetworkManager", "systemd", "python3"]
ARCH_PACKAGES = ["iw", "iproute2", "util-linux", "bluez", "bluez-utils", "networkmanager", "systemd", "python"]
PATH_LOSS_EXPONENT = 2.7
WIFI_AP_TX_DBM = 20.0
WIFI_CLIENT_TX_DBM = 15.0
BT_CLASSIC_TX_DBM = 4.0
BLE_TX_DBM = 0.0
BT_FREQ_MHZ = 2402.0
RSSI_FLOOR = -95.0
RSSI_CEIL = -30.0
MIN_PYTHON = (3, 9)

# Radiotap fields that can sit before DBM_ANTSIGNAL (bit 5).
# (size, alignment from the start of the radiotap header).
RADIOTAP_HEAD = {
    0: (8, 8),
    1: (1, 1),
    2: (1, 1),
    3: (4, 2),
    4: (2, 1),
    5: (1, 1),
}

HOP_24 = [1, 6, 11, 2, 3, 4, 5, 7, 8, 9, 10, 12, 13]
HOP_5 = [36, 40, 44, 48, 149, 153, 157, 161, 165, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140]
ARPHRD_IEEE80211_RADIOTAP = 803

RESET = "\033[0m"
BOLD = "\033[1m"
PINK = "\033[38;5;213m"
HOT = "\033[38;5;201m"
CYAN = "\033[38;5;51m"
PURPLE = "\033[38;5;141m"
INDIGO = "\033[38;5;63m"
GOLD = "\033[38;5;229m"
CREAM = "\033[38;5;224m"
MUTED = "\033[38;5;60m"
DIMC = "\033[38;5;245m"
HEAT = [63, 97, 133, 169, 201, 207, 213, 51]

ALT_ON = "\033[?1049h"
ALT_OFF = "\033[?1049l"
CURSOR_HIDE = "\033[?25l"
CURSOR_SHOW = "\033[?25h"

DISCLAIMER = """\
  AUTHORIZED USE ONLY
  The Marksman locks onto a Wi-Fi SSID or MAC, or a Bluetooth name or MAC,
  and estimates range from the signal so you can walk toward it or away.

  Run it only where you have legal authority — a signed Rules of Engagement,
  written permission from the owner, your own equipment and premises, or
  another lawful basis. Unauthorized interception can be a crime. You are
  responsible for local law. The authors assume no liability for misuse.

  Wi-Fi sight: monitor mode, receive only. No injection, deauthentication,
  or association. Building the SSID list uses a normal scan (probe requests).
  Bluetooth: BlueZ discovery, the standard inquiry and LE scan. That scan
  transmits scan requests. It does not pair, connect, or read GATT.
"""

TRACK_FIELDS = [
    "timestamp",
    "target",
    "mac",
    "name",
    "phy",
    "channel",
    "frequency_mhz",
    "signal_dbm",
    "signal_smooth_dbm",
    "distance_m",
    "trend",
    "delta_db",
    "tx_dbm",
    "path_loss_exponent",
]


class RadioError(RuntimeError):
    pass


class Colors:
    enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    @classmethod
    def wrap(cls, color: str, text: str) -> str:
        if not cls.enabled:
            return text
        return f"{color}{text}{RESET}"


def log(msg: str, level: str = "info") -> None:
    palette = {
        "info": CYAN,
        "ok": PINK,
        "warn": GOLD,
        "err": HOT,
        "hdr": BOLD + PINK,
    }
    print(Colors.wrap(palette.get(level, CYAN), msg))


def which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for base in ("/usr/sbin", "/sbin", "/usr/bin", "/bin"):
        cand = os.path.join(base, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def run_cmd(
    args: list[str],
    timeout: float = 10,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        err = exc.stderr if isinstance(exc.stderr, str) else ""
        return subprocess.CompletedProcess(args, 124, out or "", err or "timed out")
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", f"not found: {args[0]}")


def valid_iface(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", name or ""))


def clip(text: str, width: int) -> str:
    text = (text or "").replace("\n", " ").replace("\r", "")
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def normalize_mac(text: str) -> str | None:
    hexes = re.sub(r"[^0-9a-fA-F]", "", text or "")
    if len(hexes) != 12:
        return None
    return ":".join(hexes[i : i + 2] for i in range(0, 12, 2)).upper()


def usable_name(name: str, mac: str = "") -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        return ""
    compact = re.sub(r"[^0-9A-Fa-f]", "", cleaned).upper()
    mac_compact = re.sub(r"[^0-9A-Fa-f]", "", mac or "").upper()
    if mac_compact and compact == mac_compact:
        return ""
    if normalize_mac(cleaned) and not re.search(r"[A-Za-z]", cleaned):
        return ""
    return cleaned


def iso_utc(when: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when if when is not None else time.time()))


def fspl_1m_db(freq_mhz: float) -> float:
    """Free-space loss at 1 meter. FSPL(dB) = 20*log10(f_MHz) - 27.55."""
    return 20.0 * math.log10(freq_mhz) - 27.55


def estimate_distance_m(
    signal_dbm: float | None,
    freq_mhz: float | None,
    tx_dbm: float,
    exponent: float = PATH_LOSS_EXPONENT,
) -> float | None:
    """Rough range in meters. Same log-distance model as The Magic Flute.

    RSSI = TX - FSPL(1 m) - 10 * n * log10(d)
    d    = 10 ** ((TX - FSPL(1 m) - RSSI) / (10 * n))

    n = 2.7 for every distance in this tool. A frequency above 10000 is
    treated as kHz, which is how Kismet stores it.
    """
    try:
        rssi = float(signal_dbm)  # type: ignore[arg-type]
        freq = float(freq_mhz)  # type: ignore[arg-type]
        tx = float(tx_dbm)
        path_n = float(exponent)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (rssi, freq, tx, path_n)):
        return None
    if freq > 10000.0:
        freq = freq / 1000.0
    if rssi >= 0.0 or rssi < -120.0 or freq < 100.0 or freq > 10000.0 or path_n <= 0.0:
        return None
    distance = 10 ** ((tx - fspl_1m_db(freq) - rssi) / (10.0 * path_n))
    if not math.isfinite(distance) or distance <= 0.0:
        return None
    return distance


def format_distance_human(meters: float | None) -> str:
    if meters is None:
        return "— m"
    if meters >= 1000:
        return ">999 m"
    if meters >= 100:
        return f"{meters:.0f} m"
    if meters >= 10:
        return f"{meters:.1f} m"
    return f"{meters:.2f} m"


def format_distance_csv(meters: float | None) -> str:
    if meters is None:
        return ""
    return f"{meters:.2f}"


def assumed_tx(phy_hint: str) -> float:
    if phy_hint == "wifi-ap":
        return WIFI_AP_TX_DBM
    if phy_hint == "wifi-client":
        return WIFI_CLIENT_TX_DBM
    if phy_hint == "bt-classic":
        return BT_CLASSIC_TX_DBM
    return BLE_TX_DBM


def resolve_tx(phy_hint: str, advertised: float | None) -> tuple[float, bool]:
    """Prefer an advertised power in 1..30 dBm. Otherwise use the assumption."""
    if advertised is not None and math.isfinite(advertised) and 1.0 <= advertised <= 30.0:
        return float(advertised), False
    return assumed_tx(phy_hint), True


def mhz_for_channel(channel: int | None) -> float | None:
    if channel is None:
        return None
    if 1 <= channel <= 13:
        return 2412.0 + (channel - 1) * 5.0
    if channel == 14:
        return 2484.0
    if 32 <= channel <= 196:
        return 5000.0 + channel * 5.0
    return None


def channel_for_mhz(freq: float | None) -> int | None:
    if freq is None:
        return None
    f = int(round(freq))
    if f == 2484:
        return 14
    if 2412 <= f <= 2472 and (f - 2412) % 5 == 0:
        return (f - 2412) // 5 + 1
    if 5000 <= f <= 5900 and (f - 5000) % 5 == 0:
        channel = (f - 5000) // 5
        if 32 <= channel <= 177:
            return channel
    return None


def proximity_fraction(rssi: float | None) -> float | None:
    if rssi is None:
        return None
    span = RSSI_CEIL - RSSI_FLOOR
    return max(0.0, min(1.0, (rssi - RSSI_FLOOR) / span))


def zone_for(distance_m: float | None, rssi: float | None) -> str:
    if distance_m is not None:
        if distance_m < 2:
            return "sights"
        if distance_m < 8:
            return "close"
        if distance_m < 20:
            return "mid"
        return "far"
    if rssi is None:
        return "search"
    if rssi >= -45:
        return "sights"
    if rssi >= -60:
        return "close"
    if rssi >= -75:
        return "mid"
    return "far"


def zone_word(zone: str) -> str:
    return {
        "sights": "IN THE SIGHTS",
        "close": "CLOSE",
        "mid": "MID",
        "far": "FAR",
        "search": "SEARCHING",
    }.get(zone, zone.upper())


def zone_paint(zone: str) -> str:
    return {
        "sights": CYAN,
        "close": PINK,
        "mid": PURPLE,
        "far": INDIGO,
        "search": GOLD,
    }.get(zone, CREAM)


def heat_paint(fraction: float) -> str:
    idx = int(round(max(0.0, min(1.0, fraction)) * (len(HEAT) - 1)))
    return f"\033[38;5;{HEAT[idx]}m"


def print_banner() -> None:
    print()
    print(Colors.wrap(BOLD + PINK, f"  {TOOL_NAME}  {VERSION}"))
    print(Colors.wrap(PURPLE, "  Der Freischütz  ·  one mark, one sight"))
    print(
        Colors.wrap(
            DIMC,
            "  Copyright (C) 2026 Jeff Hutto. GPL-3.0-or-later; no warranty. See LICENSE.",
        )
    )
    print(Colors.wrap(DIMC, "  This is free software; you can redistribute it under the GNU GPL v3."))
    print()
    print(Colors.wrap(GOLD, DISCLAIMER))


def confirm_roe(assume_yes: bool) -> str:
    if assume_yes:
        log("Authorization acknowledgement provided (--i-have-roe).", "ok")
        return "--i-have-roe"
    if not sys.stdin.isatty():
        log("No TTY. Re-run interactively, or pass --i-have-roe plus the operator flags.", "err")
        raise SystemExit(2)
    print("Type YES to confirm you have a signed Rules of Engagement or other")
    print("lawful written authorization for this collection.")
    answer = input("Authorization: ").strip()
    if answer.upper() != "YES":
        log("Aborted. No collection started.", "err")
        raise SystemExit(2)
    log("Authorization recorded.", "ok")
    return "interactive YES"


def ask_text(prompt: str, default: str = "", required: bool = True) -> str:
    if not sys.stdin.isatty():
        if default or not required:
            return default
        log(f"No TTY and no value for: {prompt}", "err")
        raise SystemExit(2)
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    while True:
        value = input(shown).strip()
        if not value:
            value = default
        if value or not required:
            return value
        print("  A value is required.")


def ask_choice(title: str, options: list[dict], default_index: int = 0) -> dict:
    if not options:
        raise RadioError("Nothing to choose.")
    print()
    log(title, "hdr")
    for idx, opt in enumerate(options, start=1):
        mark = "  (preferred)" if opt.get("recommended") else ""
        print(f"  {idx}) {opt['label']}{mark}")
        for line in opt.get("detail") or []:
            print(f"      {line}")
    if not sys.stdin.isatty():
        return options[default_index]
    prompt = f"Select [1-{len(options)}] (default {default_index + 1}): "
    while True:
        raw = input(prompt).strip().lower()
        if not raw:
            return options[default_index]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print("  Enter a listed number.")


def interpret_choice(raw: str) -> tuple[str, int | None]:
    """Menu numbers stay put: 0 rescan, 1-5 hits, 6 typed name/SSID, 7 MAC."""
    text = (raw or "").strip().lower()
    if text in {"q", "quit", "exit"}:
        return "quit", None
    if text in {"b", "back"}:
        return "back", None
    if not text.isdigit():
        return "invalid", None
    number = int(text)
    if number == 0:
        return "rescan", None
    if 1 <= number <= 5:
        return "slot", number - 1
    if number == 6:
        return "manual-name", None
    if number == 7:
        return "manual-mac", None
    return "invalid", None


@dataclass
class Hit:
    mac: str
    name: str
    rssi: float | None
    freq_mhz: float | None = None
    channel: int | None = None
    phy_hint: str = "wifi-ap"
    advertised_tx: float | None = None
    address_type: str = ""

    def tx(self) -> tuple[float, bool]:
        return resolve_tx(self.phy_hint, self.advertised_tx)

    def distance(self) -> float | None:
        tx, _assumed = self.tx()
        freq = self.freq_mhz
        if freq is None and self.phy_hint.startswith("bt"):
            freq = BT_FREQ_MHZ
        if freq is None and self.phy_hint == "ble":
            freq = BT_FREQ_MHZ
        return estimate_distance_m(self.rssi, freq, tx)


@dataclass
class Target:
    mode: str
    value: str
    mac: str = ""
    channel: int | None = None
    freq_mhz: float | None = None
    phy_hint: str = ""
    display: str = ""
    advertised_tx: float | None = None

    def key(self) -> str:
        return f"{self.mode}:{self.value}"


@dataclass
class Sample:
    rssi: float
    freq_mhz: float | None
    mac: str
    label: str
    tx_dbm: float
    tx_assumed: bool
    channel: int | None
    phy_hint: str


@dataclass
class View:
    scan_name: str
    operator: str
    target: str
    mac: str
    kind: str
    iface: str
    detail: str
    signal_dbm: float | None
    smooth_dbm: float | None
    distance_m: float | None
    trend: str
    delta_db: float | None
    history: list[float]
    frequency_mhz: float | None
    tx_dbm: float
    tx_assumed: bool
    age_s: float | None
    note: str
    frames: int
    zone: str
    channel: int | None = None


@dataclass
class HuntStats:
    target: str
    frames: int = 0
    min_rssi: float | None = None
    max_rssi: float | None = None
    closest_m: float | None = None
    furthest_m: float | None = None
    first_heard: str = ""
    last_heard: str = ""


class SightState:
    def __init__(self, scan_name: str, operator: str, kind: str, iface: str, target: Target) -> None:
        self.scan_name = scan_name
        self.operator = operator
        self.kind = kind
        self.iface = iface
        self.target = target
        self.mac = target.mac
        self.label = target.display or target.value
        self.freq = target.freq_mhz
        self.channel = target.channel
        self.phy_hint = target.phy_hint
        self.tx_dbm, self.tx_assumed = resolve_tx(target.phy_hint, target.advertised_tx)
        self.raw: float | None = None
        self.smooth: float | None = None
        self.last_heard = 0.0
        self.last_ema = 0.0
        self.last_spark = 0.0
        self.frames = 0
        self.spark: deque[float] = deque(maxlen=40)
        self.timed: deque[tuple[float, float]] = deque(maxlen=400)
        self.detail = "starting"
        self.note = ""

    def fold(self, sample: Sample, now: float) -> None:
        self.frames += 1
        self.raw = sample.rssi
        self.last_heard = now
        if sample.mac:
            self.mac = sample.mac
        if sample.label:
            self.label = sample.label
        if sample.freq_mhz:
            self.freq = sample.freq_mhz
        if sample.channel:
            self.channel = sample.channel
        if sample.phy_hint:
            self.phy_hint = sample.phy_hint
        self.tx_dbm = sample.tx_dbm
        self.tx_assumed = sample.tx_assumed
        if self.smooth is None or now - self.last_ema >= 0.05:
            if self.smooth is None:
                self.smooth = sample.rssi
            else:
                self.smooth = 0.35 * sample.rssi + 0.65 * self.smooth
            self.last_ema = now
            self.timed.append((now, self.smooth))
        if now - self.last_spark >= 0.25:
            if self.smooth is not None:
                self.spark.append(self.smooth)
            self.last_spark = now

    def trend(self, now: float) -> tuple[str, float | None]:
        if self.smooth is None or not self.last_heard:
            return "SEARCHING", None
        age = now - self.last_heard
        if age > 3.0:
            return "LOST", None
        if len(self.timed) < 2 or now - self.timed[0][0] < 1.0:
            return "HOLDING", None
        target_t = now - 3.0
        past = self.timed[0][1]
        for stamp, value in self.timed:
            if stamp <= target_t:
                past = value
            else:
                break
        delta = self.smooth - past
        if delta >= 2.0:
            return "CLOSING", delta
        if delta <= -2.0:
            return "FALLING BACK", delta
        return "HOLDING", delta

    def snapshot(self, now: float) -> View:
        trend, delta = self.trend(now)
        freq = self.freq
        if freq is None and self.kind == "Bluetooth":
            freq = BT_FREQ_MHZ
        distance = estimate_distance_m(self.smooth, freq, self.tx_dbm)
        zone = "search" if self.smooth is None else zone_for(distance, self.smooth)
        if trend == "LOST":
            zone = "far"
        age = None if not self.last_heard else max(0.0, now - self.last_heard)
        return View(
            scan_name=self.scan_name,
            operator=self.operator,
            target=self.label,
            mac=self.mac,
            kind=self.kind,
            iface=self.iface,
            detail=self.detail,
            signal_dbm=self.raw,
            smooth_dbm=self.smooth,
            distance_m=distance,
            trend=trend,
            delta_db=delta,
            history=list(self.spark),
            frequency_mhz=freq,
            tx_dbm=self.tx_dbm,
            tx_assumed=self.tx_assumed,
            age_s=age,
            note=self.note,
            frames=self.frames,
            zone=zone,
            channel=self.channel,
        )


def observe(stats: HuntStats, view: View, when: str) -> None:
    if view.smooth_dbm is None:
        return
    stats.frames += 1
    if stats.min_rssi is None or view.smooth_dbm < stats.min_rssi:
        stats.min_rssi = view.smooth_dbm
    if stats.max_rssi is None or view.smooth_dbm > stats.max_rssi:
        stats.max_rssi = view.smooth_dbm
    if view.distance_m is not None:
        if stats.closest_m is None or view.distance_m < stats.closest_m:
            stats.closest_m = view.distance_m
        if stats.furthest_m is None or view.distance_m > stats.furthest_m:
            stats.furthest_m = view.distance_m
    if not stats.first_heard:
        stats.first_heard = when
    stats.last_heard = when


def fit_pieces(pieces: list[tuple[str, str | None]], width: int, color: bool) -> str:
    kept: list[tuple[str, str | None]] = []
    left = width
    for text, paint in pieces:
        if left <= 0:
            break
        if len(text) > left:
            text = text[:left]
        kept.append((text, paint))
        left -= len(text)
    if left > 0:
        kept.append((" " * left, None))
    out: list[str] = []
    for text, paint in kept:
        if color and paint:
            out.append(f"{paint}{text}{RESET}")
        else:
            out.append(text)
    return "".join(out)


def sparkline(values: list[float], width: int) -> str:
    if width <= 0:
        return ""
    if not values:
        return "·" * width
    window = values[-width:]
    glyphs = "▁▂▃▄▅▆▇█"
    chars: list[str] = []
    for value in window:
        frac = proximity_fraction(value) or 0.0
        chars.append(glyphs[min(len(glyphs) - 1, int(round(frac * (len(glyphs) - 1))))])
    return ("·" * (width - len(chars))) + "".join(chars)


def bar_lines(fraction: float | None, rows: int, color: bool) -> list[list[tuple[str, str | None]]]:
    frac = 0.0 if fraction is None else max(0.0, min(1.0, fraction))
    filled = int(round(frac * rows))
    lines: list[list[tuple[str, str | None]]] = []
    for i in range(rows):
        from_bottom = rows - 1 - i
        if filled > 0 and from_bottom < filled:
            height = from_bottom / max(1, rows - 1)
            paint = heat_paint(height) if color else None
            lines.append([("████", paint)])
        else:
            lines.append([("░░░░", MUTED if color else None)])
    return lines


def render_sight(view: View, width: int = 78, height: int = 24, color: bool = False) -> str:
    inner = max(48, min(width - 2, 76))
    bar_n = max(6, min(12, height - 13))
    paint = zone_paint(view.zone)
    arrow = {"CLOSING": "▲", "FALLING BACK": "▼", "HOLDING": "◆", "LOST": "×", "SEARCHING": "◌"}.get(
        view.trend, "◆"
    )
    signal = "— dBm" if view.signal_dbm is None else f"{view.signal_dbm:.0f} dBm"
    smooth = "—" if view.smooth_dbm is None else f"{view.smooth_dbm:.1f} dBm"
    distance = format_distance_human(view.distance_m)
    delta = ""
    if view.delta_db is not None:
        delta = f"{view.delta_db:+.1f} dB / 3s"
    age = "no fix yet" if view.age_s is None else f"heard {view.age_s:.1f}s ago"
    freq = f"{view.frequency_mhz:.0f} MHz" if view.frequency_mhz else "freq —"
    tx_word = "assumed" if view.tx_assumed else "advertised"
    note = (
        f"n={PATH_LOSS_EXPONENT:.1f}  tx {view.tx_dbm:.0f} dBm {tx_word}  {freq}"
        "  ·  near / mid / far, not a tape measure"
    )

    def row(pieces: list[tuple[str, str | None]]) -> str:
        body = fit_pieces(pieces, inner, color)
        if not color:
            return "║" + body + "║"
        return f"{PURPLE}║{RESET}" + body + f"{PURPLE}║{RESET}"

    def rule(left: str, right: str) -> str:
        raw = left + ("═" * inner) + right
        if not color:
            return raw
        return f"{PURPLE}{raw}{RESET}"

    title_left = " ◆ THE MARKSMAN"
    title_right = "DER FREISCHÜTZ ◆ "
    gap = max(1, inner - len(title_left) - len(title_right))
    sub_left = "  " + clip(view.scan_name or "untitled scan", 28)
    sub_right = clip(view.operator or "", 24) + " "
    sub_gap = max(1, inner - len(sub_left) - len(sub_right))
    mac = view.mac or "MAC not seen yet"
    target_left = "  TARGET  " + clip(view.target or "—", 28)
    target_right = view.kind
    t_gap = max(1, inner - len(target_left) - len(target_right))
    mac_left = "  " + clip(mac, 22) + "   " + clip(view.iface, 16)
    mac_right = clip(view.detail, 24)
    m_gap = max(1, inner - len(mac_left) - len(mac_right))
    num_left = f"  {signal:<10}   {distance:>8}"
    num_right = f"{arrow} {view.trend}"
    n_gap = max(1, inner - len(num_left) - len(num_right))
    meta_left = f"  smooth {smooth:<10}  {delta}"
    meta_right = age
    meta_gap = max(1, inner - len(meta_left) - len(meta_right))

    header = [
        row([(title_left, PINK), (" " * gap, None), (title_right, CYAN)]),
        row([(sub_left, GOLD), (" " * sub_gap, None), (sub_right, CREAM)]),
        row([(target_left, paint), (" " * t_gap, None), (target_right, CYAN)]),
        row([(mac_left, CREAM), (" " * m_gap, None), (mac_right, DIMC)]),
        row([(num_left, paint), (" " * n_gap, None), (num_right, paint)]),
        row([(meta_left, DIMC), (" " * meta_gap, None), (meta_right, DIMC)]),
    ]
    spark_w = max(8, inner - 16)
    history = sparkline(view.history, spark_w)
    bars = bar_lines(proximity_fraction(view.smooth_dbm), bar_n, color)
    graph: list[str] = []
    for i, glyph in enumerate(bars):
        extra: list[tuple[str, str | None]] = [("  ", None)]
        if i == 0:
            extra.append(("near", CYAN))
        elif i == 1:
            extra.append(("up means closer", DIMC))
        elif i == 3:
            extra.append((history, paint if not color else None))
            if color:
                extra = [("  ", None)] + _spark_pieces(view.history, spark_w)
        elif i == 4:
            extra.append(("older" + " " * max(0, spark_w - 10) + "now", DIMC))
        elif i == bar_n - 4:
            extra.append((zone_word(view.zone), paint))
        elif i == bar_n - 2:
            extra.append((clip(view.note, inner - 8), GOLD))
        elif i == bar_n - 1:
            extra.append(("far", INDIGO))
        graph.append(row([("  ", None)] + glyph + extra))
    footer = [
        row([("  0 rescan    b back to the list    q quit", GOLD)]),
        row([("  " + clip(note, inner - 2), DIMC)]),
    ]
    lines = [
        rule("╔", "╗"),
        *header[:2],
        rule("╠", "╣"),
        *header[2:],
        rule("╠", "╣"),
        *graph,
        rule("╠", "╣"),
        *footer,
        rule("╚", "╝"),
    ]
    # Trim or pad so the sight occupies the terminal and does not scroll.
    if len(lines) > height:
        lines = lines[: height - 1] + [lines[-1]]
    return "\n".join(lines)


def _spark_pieces(values: list[float], width: int) -> list[tuple[str, str | None]]:
    text = sparkline(values, width)
    pieces: list[tuple[str, str | None]] = []
    glyphs = "▁▂▃▄▅▆▇█"
    for char in text:
        if char in glyphs:
            pieces.append((char, heat_paint(glyphs.index(char) / (len(glyphs) - 1))))
        else:
            pieces.append((char, MUTED))
    return _merge_pieces(pieces)


def _merge_pieces(pieces: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    merged: list[tuple[str, str | None]] = []
    for text, paint in pieces:
        if merged and merged[-1][1] == paint:
            merged[-1] = (merged[-1][0] + text, paint)
        else:
            merged.append((text, paint))
    return merged


def draw_frame(text: str) -> None:
    parts = ["\033[H"]
    for line in text.splitlines():
        parts.append(line + "\033[K\n")
    parts.append("\033[J")
    sys.stdout.write("".join(parts))
    sys.stdout.flush()


def enter_cbreak() -> list:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] = new[3] & ~(termios.ECHO | termios.ICANON)
    new[6][termios.VMIN] = 0
    new[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSADRAIN, new)
    return old


def restore_term(old: list | None) -> None:
    if old is None:
        return
    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)


def read_key() -> str:
    if not sys.stdin.isatty():
        return ""
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    if not ready:
        return ""
    try:
        return sys.stdin.read(1)
    except OSError:
        return ""


def run_sight(source, state: SightState, stats: HuntStats, track: "TrackLog | None", duration: float) -> str:
    tty = sys.stdout.isatty()
    old = None
    if tty:
        old = enter_cbreak()
        sys.stdout.write(ALT_ON + CURSOR_HIDE)
        sys.stdout.flush()
    started = time.monotonic()
    last_draw = 0.0
    last_log = 0.0
    reason = "quit"
    try:
        while True:
            now = time.monotonic()
            if duration and now - started >= duration:
                reason = "quit"
                break
            try:
                sample = source.poll()
            except RadioError as exc:
                state.note = str(exc)
                sample = None
            fresh = sample is not None
            if fresh:
                state.fold(sample, now)
            state.detail = getattr(source, "status", state.detail)
            state.note = getattr(source, "note", state.note) or state.note
            key = ""
            if tty:
                try:
                    key = read_key()
                except InterruptedError:
                    reason = "quit"
                    break
            if key in {"q", "Q", "\x03"}:
                reason = "quit"
                break
            if key in {"b", "B"}:
                reason = "back"
                break
            if key == "0":
                reason = "rescan"
                break
            view = state.snapshot(now)
            if fresh and view.smooth_dbm is not None:
                observe(stats, view, iso_utc())
            if tty and now - last_draw >= 0.1:
                size = shutil.get_terminal_size(fallback=(80, 24))
                frame = render_sight(view, width=size.columns, height=size.lines, color=Colors.enabled)
                draw_frame(frame)
                last_draw = now
            elif not tty and now - last_draw >= 1.0:
                dist = format_distance_human(view.distance_m)
                sig = "—" if view.smooth_dbm is None else f"{view.smooth_dbm:.1f} dBm"
                print(f"{iso_utc()}  {sig}  {dist}  {view.trend}  {state.detail}", flush=True)
                last_draw = now
            heard_recently = view.age_s is not None and view.age_s <= 1.0
            if track is not None and view.smooth_dbm is not None and heard_recently and now - last_log >= 0.5:
                track.write(view, state.target.key())
                last_log = now
            time.sleep(0.05)
    finally:
        if tty:
            sys.stdout.write(CURSOR_SHOW + ALT_OFF)
            sys.stdout.flush()
            restore_term(old)
    return reason


class TrackLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        new = not path.exists() or path.stat().st_size == 0
        self.fh = path.open("a", encoding="utf-8", newline="")
        self.writer = csv.DictWriter(self.fh, fieldnames=TRACK_FIELDS)
        if new:
            self.writer.writeheader()
            self._sync()

    def _sync(self) -> None:
        self.fh.flush()
        os.fsync(self.fh.fileno())

    def write(self, view: View, target_key: str) -> None:
        self.writer.writerow(
            {
                "timestamp": iso_utc(),
                "target": target_key,
                "mac": view.mac,
                "name": view.target,
                "phy": view.kind,
                "channel": "" if view.channel is None else str(view.channel),
                "frequency_mhz": "" if view.frequency_mhz is None else f"{view.frequency_mhz:.0f}",
                "signal_dbm": "" if view.signal_dbm is None else f"{view.signal_dbm:.1f}",
                "signal_smooth_dbm": "" if view.smooth_dbm is None else f"{view.smooth_dbm:.1f}",
                "distance_m": format_distance_csv(view.distance_m),
                "trend": view.trend,
                "delta_db": "" if view.delta_db is None else f"{view.delta_db:.2f}",
                "tx_dbm": f"{view.tx_dbm:.1f}",
                "path_loss_exponent": f"{PATH_LOSS_EXPONENT:.1f}",
            }
        )
        self._sync()

    def close(self) -> None:
        self.fh.close()


def parse_iw_scan(text: str) -> list[Hit]:
    hits: list[Hit] = []
    for chunk in re.split(r"(?m)^BSS\s+", text):
        mac_m = re.match(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})", chunk)
        if not mac_m:
            continue
        freq_m = re.search(r"(?m)^\s*freq:\s*([0-9]+(?:\.[0-9]+)?)", chunk)
        sig_m = re.search(r"(?m)^\s*signal:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*dBm", chunk)
        ssid_m = re.search(r"(?m)^\s*SSID:\s*(.*)$", chunk)
        ch_m = re.search(r"DS Parameter set:\s*channel\s+(\d+)", chunk)
        freq = float(freq_m.group(1)) if freq_m else None
        rssi = float(sig_m.group(1)) if sig_m else None
        if rssi is not None and (rssi >= 0.0 or rssi < -120.0):
            rssi = None
        ssid = ssid_m.group(1).strip() if ssid_m else ""
        channel = int(ch_m.group(1)) if ch_m else channel_for_mhz(freq)
        if freq is None and channel:
            freq = mhz_for_channel(channel)
        hits.append(
            Hit(
                mac=mac_m.group(1).upper(),
                name=ssid,
                rssi=rssi,
                freq_mhz=freq,
                channel=channel,
                phy_hint="wifi-ap",
            )
        )
    return hits


def top_ssids(hits: list[Hit], limit: int = 5) -> list[Hit]:
    best: dict[str, Hit] = {}
    for hit in hits:
        if not hit.name:
            continue
        cur = best.get(hit.name)
        if cur is None or (hit.rssi or -999) > (cur.rssi or -999):
            best[hit.name] = hit
    rows = list(best.values())
    rows.sort(key=lambda row: row.rssi if row.rssi is not None else -999, reverse=True)
    return rows[:limit]


def top_named(hits: list[Hit], limit: int = 5) -> list[Hit]:
    named = [hit for hit in hits if usable_name(hit.name, hit.mac)]
    named.sort(key=lambda row: row.rssi if row.rssi is not None else -999, reverse=True)
    return named[:limit]


def parse_radiotap(packet: bytes) -> tuple[int, float, float | None] | None:
    """Return (dot11 offset, dBm, freq MHz). Alignment is from byte 0 of the header."""
    if len(packet) < 8:
        return None
    version, _pad, it_len = struct.unpack_from("<BBH", packet, 0)
    if version != 0 or not (8 <= it_len <= len(packet)):
        return None
    offset = 4
    presents: list[int] = []
    while True:
        if offset + 4 > it_len:
            return None
        word = struct.unpack_from("<I", packet, offset)[0]
        presents.append(word)
        offset += 4
        if not word & (1 << 31):
            break
        if len(presents) > 8:
            return None
    signal = None
    freq = None
    flags = 0
    for bit in range(0, 6):
        if not presents[0] & (1 << bit):
            continue
        size, align = RADIOTAP_HEAD[bit]
        offset = (offset + align - 1) & ~(align - 1)
        if offset + size > it_len:
            return None
        if bit == 1:
            flags = packet[offset]
        elif bit == 3:
            freq = float(struct.unpack_from("<H", packet, offset)[0])
        elif bit == 5:
            signal = float(struct.unpack_from("<b", packet, offset)[0])
        offset += size
    if flags & 0x40 or signal is None:
        return None
    if signal >= 0.0 or signal < -120.0:
        return None
    if freq is not None and not (2000.0 <= freq <= 7125.0):
        freq = None
    return it_len, signal, freq


def mac_str(raw: bytes) -> str:
    return ":".join(f"{b:02X}" for b in raw)


def read_ssid(tagged: bytes) -> str | None:
    index = 0
    while index + 2 <= len(tagged):
        eid = tagged[index]
        elen = tagged[index + 1]
        index += 2
        if index + elen > len(tagged):
            return None
        if eid == 0:
            return tagged[index : index + elen].decode("utf-8", "replace")
        index += elen
    return None


def parse_dot11(frame: bytes) -> dict | None:
    if len(frame) < 24:
        return None
    fc = struct.unpack_from("<H", frame, 0)[0]
    ftype = (fc >> 2) & 0x3
    subtype = (fc >> 4) & 0xF
    transmitter = mac_str(frame[10:16])
    ssid = None
    beacon = False
    if ftype == 0 and subtype in {5, 8} and len(frame) >= 36:
        beacon = True
        ssid = read_ssid(frame[36:])
    return {"transmitter": transmitter, "ssid": ssid, "beacon": beacon}


def parse_wifi_packet(packet: bytes) -> dict | None:
    radio = parse_radiotap(packet)
    if radio is None:
        return None
    offset, rssi, freq = radio
    body = parse_dot11(packet[offset:])
    if body is None:
        return None
    body["rssi"] = rssi
    body["freq"] = freq
    return body


def frame_matches(frame: dict, target: Target) -> bool:
    if target.mode == "ssid":
        return bool(frame.get("beacon") and frame.get("ssid") == target.value and target.value)
    if target.mode == "mac":
        return frame.get("transmitter") == target.mac
    return False


def read_sysfs(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def usb_ids(start: Path) -> tuple[str, str, str]:
    try:
        cur = start.resolve()
    except OSError:
        return "", "", ""
    for _ in range(12):
        vendor = read_sysfs(cur / "idVendor").lower()
        product = read_sysfs(cur / "idProduct").lower()
        if vendor and product and vendor != "1d6b":
            label = " ".join(x for x in (read_sysfs(cur / "manufacturer"), read_sysfs(cur / "product")) if x)
            return vendor, product, label
        if cur.parent == cur:
            break
        cur = cur.parent
    return "", "", ""


def is_usb_net(iface: str) -> bool:
    try:
        return "/usb" in str(Path(f"/sys/class/net/{iface}/device").resolve())
    except OSError:
        return False


def net_driver(iface: str) -> str:
    link = Path(f"/sys/class/net/{iface}/device/driver")
    try:
        return link.resolve().name
    except OSError:
        return ""


def parse_iw_dev() -> list[dict]:
    iw = which("iw")
    if not iw:
        return []
    proc = run_cmd([iw, "dev"])
    ifaces: list[dict] = []
    phy = ""
    current: dict | None = None
    for line in (proc.stdout or "").splitlines():
        phy_m = re.match(r"^phy#(\d+)", line)
        if phy_m:
            phy = f"phy{phy_m.group(1)}"
            continue
        if_m = re.search(r"Interface\s+(\S+)", line)
        if if_m:
            if current:
                ifaces.append(current)
            current = {"iface": if_m.group(1), "phy": phy, "mac": "", "type": ""}
            continue
        if current is None:
            continue
        addr = re.search(r"addr\s+([0-9A-Fa-f:]{17})", line)
        if addr:
            current["mac"] = addr.group(1).upper()
        typ = re.search(r"^\s*type\s+(\S+)", line)
        if typ:
            current["type"] = typ.group(1)
    if current:
        ifaces.append(current)
    return ifaces


def phy_info(phy: str) -> dict:
    iw = which("iw")
    if not iw or not phy:
        return {"monitor": False, "bands": []}
    proc = run_cmd([iw, "phy", phy, "info"], timeout=8)
    modes: list[str] = []
    in_modes = False
    freqs: list[float] = []
    for line in (proc.stdout or "").splitlines():
        if "Supported interface modes:" in line:
            in_modes = True
            continue
        if in_modes:
            stripped = line.strip()
            if stripped.startswith("* "):
                mode = stripped[2:].strip()
                if ":" not in mode and "#{" not in mode:
                    modes.append(mode)
            elif stripped:
                in_modes = False
        match = re.search(r"\*\s+(\d+(?:\.\d+)?)\s+MHz", line)
        if match:
            freqs.append(float(match.group(1)))
    bands = []
    if any(freq < 3000 for freq in freqs):
        bands.append("2.4 GHz")
    if any(5000 <= freq < 5900 for freq in freqs):
        bands.append("5 GHz")
    if any(freq >= 5900 for freq in freqs):
        bands.append("6 GHz")
    return {"monitor": "monitor" in modes, "bands": bands}


def rfkill_entries() -> list[dict]:
    rfkill = which("rfkill")
    if not rfkill:
        return []
    proc = run_cmd([rfkill, "list"])
    entries: list[dict] = []
    current: dict | None = None
    for line in (proc.stdout or "").splitlines():
        header = re.match(r"^(\d+):\s+(\S+):\s+(.+)$", line.strip())
        if header:
            current = {
                "index": int(header.group(1)),
                "name": header.group(2),
                "kind": header.group(3).strip(),
                "soft": False,
                "hard": False,
            }
            entries.append(current)
            continue
        if current is None:
            continue
        if "Soft blocked: yes" in line:
            current["soft"] = True
        if "Hard blocked: yes" in line:
            current["hard"] = True
    return entries


def rfkill_for(names: list[str]) -> dict | None:
    wanted = {name for name in names if name}
    for entry in rfkill_entries():
        if entry["name"] in wanted:
            return entry
    return None


class RfkillHold:
    """Unblock the chosen radio for the hunt, and put a soft block back if it had one."""

    def __init__(self) -> None:
        self._restore: list[int] = []

    def open(self, names: list[str]) -> None:
        entry = rfkill_for(names)
        if entry is None:
            return
        if entry["hard"]:
            raise RadioError(
                f"{entry['name']} is hard-blocked. Flip the wireless switch, then try again."
            )
        if entry["soft"]:
            rfkill = which("rfkill")
            if not rfkill:
                raise RadioError("rfkill is not installed, and the radio is soft-blocked.")
            proc = run_cmd([rfkill, "unblock", str(entry["index"])])
            if proc.returncode != 0:
                raise RadioError((proc.stderr or proc.stdout or "rfkill unblock failed").strip())
            self._restore.append(entry["index"])
            log(f"Unblocked {entry['name']} for this hunt. It will be blocked again on exit.")

    def close(self) -> None:
        rfkill = which("rfkill")
        for index in self._restore:
            if rfkill:
                run_cmd([rfkill, "block", str(index)])
        self._restore.clear()


def classify_wifi(iface: str, driver: str) -> tuple[str, bool]:
    if driver in {"mt7921u", "mt76x2u", "mt7921e", "rt2800usb"} or iface.startswith("wlx"):
        return "USB Wi-Fi (preferred hunter)", True
    if is_usb_net(iface):
        return "USB Wi-Fi", True
    if driver == "iwlwifi":
        return "Built-in Intel Wi-Fi", False
    if driver.startswith(("rtw", "rtl")):
        return f"Built-in Wi-Fi ({driver})", False
    return driver or "Wi-Fi adapter", False


def list_wifi_controllers() -> list[dict]:
    items = []
    for row in parse_iw_dev():
        iface = row["iface"]
        if iface.startswith(("lo", "docker", "veth", "br-", "tun", "kismon")):
            continue
        if row.get("type") == "monitor":
            continue
        driver = net_driver(iface)
        hint, recommended = classify_wifi(iface, driver)
        info = phy_info(row.get("phy") or "")
        kill = rfkill_for([row.get("phy") or "", iface])
        kill_s = "unknown"
        if kill:
            if kill["hard"]:
                kill_s = "hard-blocked"
            elif kill["soft"]:
                kill_s = "soft-blocked"
            else:
                kill_s = "unblocked"
        vendor, product, _label = usb_ids(Path(f"/sys/class/net/{iface}/device"))
        bus = f"{vendor}:{product}" if vendor else ("usb" if is_usb_net(iface) else "pci/built-in")
        items.append(
            {
                "iface": iface,
                "phy": row.get("phy") or "",
                "mac": row.get("mac") or "",
                "driver": driver,
                "monitor": bool(info.get("monitor")),
                "bands": info.get("bands") or [],
                "recommended": recommended and bool(info.get("monitor")),
                "label": f"{iface}  {hint}",
                "detail": [
                    f"phy={row.get('phy') or '-'}  mac={row.get('mac') or '-'}  driver={driver or '-'}  bus={bus}",
                    f"monitor={'yes' if info.get('monitor') else 'NO'}  "
                    f"bands={', '.join(info.get('bands') or ['unknown'])}  rfkill={kill_s}",
                ],
            }
        )
    items.sort(key=lambda item: (not item.get("recommended"), item["iface"]))
    return items


def list_bt_controllers() -> list[dict]:
    listed: dict[str, str] = {}
    btctl = which("bluetoothctl")
    if btctl:
        proc = run_cmd([btctl, "list"])
        for line in (proc.stdout or "").splitlines():
            match = re.search(r"Controller\s+([0-9A-Fa-f:]{17})\s+(\S+)", line)
            if match:
                listed[match.group(1).upper()] = match.group(2)
    items = []
    root = Path("/sys/class/bluetooth")
    names = sorted(p.name for p in root.glob("hci*")) if root.is_dir() else []
    for iface in names:
        mac = ""
        show = ""
        address = read_sysfs(Path(f"/sys/class/bluetooth/{iface}/address")).upper()
        mac = address
        name = listed.get(mac, "")
        manuf = ""
        hci = which("hciconfig")
        if hci:
            proc = run_cmd([hci, "-a", iface])
            for line in (proc.stdout or "").splitlines():
                addr = re.search(r"BD Address:\s+([0-9A-Fa-f:]{17})", line)
                if addr:
                    mac = addr.group(1).upper()
                if "Manufacturer:" in line:
                    manuf = line.split(":", 1)[-1].strip()
                if "Name:" in line and not name:
                    name = line.split(":", 1)[-1].strip().strip("'\"")
        powered = "unknown"
        busctl = which("busctl")
        if busctl:
            proc = run_cmd(
                ["busctl", "--json=short", "get-property", "org.bluez", f"/org/bluez/{iface}", "org.bluez.Adapter1", "Powered"]
            )
            if proc.returncode == 0 and "true" in (proc.stdout or ""):
                powered = "yes"
            elif proc.returncode == 0 and "false" in (proc.stdout or ""):
                powered = "no"
        kill = rfkill_for([iface])
        kill_s = "unknown"
        if kill:
            kill_s = "hard-blocked" if kill["hard"] else ("soft-blocked" if kill["soft"] else "unblocked")
        _vendor, _product, usb_label = usb_ids(Path(f"/sys/class/bluetooth/{iface}/device"))
        hint = manuf or usb_label or "Bluetooth controller"
        items.append(
            {
                "iface": iface,
                "mac": mac,
                "name": name,
                "recommended": powered == "yes" and kill_s != "hard-blocked",
                "label": f"{iface}  {hint}" + (f"  ({name})" if name else ""),
                "detail": [
                    f"bdaddr={mac or '-'}  manufacturer={manuf or '-'}  powered={powered}  rfkill={kill_s}",
                    "BlueZ discovery on this controller. No pairing.",
                ],
            }
        )
    if items:
        # Prefer a single powered controller when several exist; keep the first powered one marked.
        powered = [item for item in items if item.get("recommended")]
        if len(powered) > 1:
            for item in powered[1:]:
                item["recommended"] = False
    items.sort(key=lambda item: (not item.get("recommended"), item["iface"]))
    return items


def iface_arphrd(iface: str) -> int:
    try:
        return int(read_sysfs(Path(f"/sys/class/net/{iface}/type")) or "0")
    except ValueError:
        return 0


def nm_set_managed(iface: str, managed: bool) -> None:
    nmcli = which("nmcli")
    if not nmcli:
        return
    run_cmd([nmcli, "device", "set", iface, "managed", "yes" if managed else "no"], timeout=8)


class WifiPort:
    def __init__(self, iface: str, bands: list[str]) -> None:
        self.iface = iface
        self.bands = bands
        self.sock: socket.socket | None = None
        self.monitor = False
        self.touched = False
        self.channel: int | None = None
        self.freq: float | None = None
        self.note = ""

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if not self.touched:
            self.monitor = False
            return
        ip = which("ip") or "ip"
        iw = which("iw") or "iw"
        run_cmd([ip, "link", "set", self.iface, "down"], timeout=5)
        run_cmd([iw, "dev", self.iface, "set", "type", "managed"], timeout=5)
        run_cmd([ip, "link", "set", self.iface, "up"], timeout=5)
        nm_set_managed(self.iface, True)
        self.monitor = False
        self.touched = False

    def open_monitor(self, channel: int | None) -> None:
        if not valid_iface(self.iface):
            raise RadioError(f"Refusing interface name {self.iface!r}.")
        if self.monitor and self.sock is not None:
            if channel:
                self.tune(channel)
            return
        iw = which("iw")
        ip = which("ip")
        if not iw or not ip:
            raise RadioError("iw and ip are required for monitor mode.")
        nm_set_managed(self.iface, False)
        self.touched = True
        down = run_cmd([ip, "link", "set", self.iface, "down"], timeout=5)
        if down.returncode != 0:
            raise RadioError((down.stderr or "could not set the interface down").strip())
        typed = run_cmd([iw, "dev", self.iface, "set", "type", "monitor"], timeout=5)
        up = run_cmd([ip, "link", "set", self.iface, "up"], timeout=5)
        if typed.returncode != 0 or up.returncode != 0:
            err = (typed.stderr or up.stderr or "monitor mode failed").strip()
            self.close()
            raise RadioError(err)
        time.sleep(0.15)
        if iface_arphrd(self.iface) != ARPHRD_IEEE80211_RADIOTAP:
            self.close()
            raise RadioError("The driver did not expose radiotap frames.")
        try:
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
            sock.bind((self.iface, 0))
            sock.setblocking(False)
        except OSError as exc:
            self.close()
            raise RadioError(f"Could not read packets: {exc}") from exc
        self.sock = sock
        self.monitor = True
        if channel:
            self.tune(channel)

    def tune(self, channel: int) -> bool:
        freq = mhz_for_channel(channel)
        if freq is None:
            return False
        iw = which("iw") or "iw"
        proc = run_cmd([iw, "dev", self.iface, "set", "freq", str(int(freq))], timeout=4)
        if proc.returncode != 0:
            self.note = (proc.stderr or proc.stdout or "channel change failed").strip().splitlines()[-1][:80]
            return False
        self.channel = channel
        self.freq = freq
        self.note = ""
        return True

    def drain(self, limit: int = 64) -> list[dict]:
        frames: list[dict] = []
        if self.sock is None:
            return frames
        for _ in range(limit):
            try:
                packet = self.sock.recv(65535)
            except BlockingIOError:
                break
            except OSError:
                break
            parsed = parse_wifi_packet(packet)
            if parsed:
                frames.append(parsed)
        return frames


def hop_channels(bands: list[str]) -> list[int]:
    channels = list(HOP_24)
    if not bands or any(band.startswith("5") or band.startswith("6") for band in bands):
        channels.extend(HOP_5)
    return channels


class WifiSource:
    def __init__(self, port: WifiPort, target: Target, mode: str) -> None:
        self.port = port
        self.target = target
        self.mode = mode
        self.status = "starting"
        self.note = ""
        self.hops = hop_channels(port.bands)
        self.hop_i = 0
        self.last_tune = 0.0
        self.fixed = target.channel is not None
        self.locked = target.channel is not None
        if self.mode == "monitor" and target.channel:
            self.status = f"watching ch {target.channel}"
        elif self.mode == "monitor":
            self.status = "searching"

    def poll(self) -> Sample | None:
        if self.mode != "monitor":
            return self._scan_once()
        best = None
        for frame in self.port.drain():
            if not frame_matches(frame, self.target):
                continue
            if best is None or frame["rssi"] > best["rssi"]:
                best = frame
        if best is not None:
            self.locked = True
        elif not self.fixed and not self.locked and time.monotonic() - self.last_tune >= 0.25:
            self._hop()
        if self.locked and self.port.channel:
            self.status = f"locked ch {self.port.channel}"
        elif self.port.channel:
            self.status = f"searching ch {self.port.channel}"
        else:
            self.status = "searching"
        self.note = self.port.note
        if best is None:
            return None
        beacon = bool(best.get("beacon"))
        hint = "wifi-ap" if beacon or self.target.phy_hint == "wifi-ap" else "wifi-client"
        if self.target.mode == "ssid":
            hint = "wifi-ap"
        tx, assumed = resolve_tx(hint, self.target.advertised_tx)
        freq = best.get("freq") or self.port.freq or self.target.freq_mhz
        channel = channel_for_mhz(freq) or self.port.channel
        label = self.target.display or self.target.value
        if self.target.mode == "ssid":
            label = self.target.value
        return Sample(
            rssi=float(best["rssi"]),
            freq_mhz=freq,
            mac=str(best.get("transmitter") or self.target.mac),
            label=label,
            tx_dbm=tx,
            tx_assumed=assumed,
            channel=channel,
            phy_hint=hint,
        )

    def _hop(self) -> None:
        if not self.hops:
            return
        channel = self.hops[self.hop_i % len(self.hops)]
        self.hop_i += 1
        if self.port.tune(channel):
            self.last_tune = time.monotonic()
        else:
            self.last_tune = time.monotonic()

    def _scan_once(self) -> Sample | None:
        now = time.monotonic()
        if now < self.last_tune:
            self.status = "scan pause"
            return None
        self.last_tune = now + 4.0
        self.status = "managed scan"
        try:
            hits = wifi_scan(self.port.iface)
        except RadioError as exc:
            self.note = str(exc)
            return None
        best: Hit | None = None
        for hit in hits:
            frame = {
                "beacon": True,
                "ssid": hit.name,
                "transmitter": hit.mac,
                "rssi": hit.rssi if hit.rssi is not None else -999,
            }
            if hit.rssi is None or not frame_matches(frame, self.target):
                continue
            if best is None or (hit.rssi or -999) > (best.rssi or -999):
                best = hit
        if best is None or best.rssi is None:
            self.note = "not heard on this scan"
            return None
        self.note = ""
        tx, assumed = best.tx()
        return Sample(
            rssi=best.rssi,
            freq_mhz=best.freq_mhz,
            mac=best.mac,
            label=best.name or self.target.display or self.target.value,
            tx_dbm=tx,
            tx_assumed=assumed,
            channel=best.channel,
            phy_hint=best.phy_hint,
        )


def wifi_scan(iface: str) -> list[Hit]:
    if not valid_iface(iface):
        raise RadioError(f"Refusing interface name {iface!r}.")
    ip = which("ip")
    iw = which("iw")
    if not iw:
        raise RadioError("iw is not installed.")
    if ip:
        run_cmd([ip, "link", "set", iface, "up"], timeout=5)
    proc = run_cmd([iw, "dev", iface, "scan"], timeout=25)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "scan failed").strip().splitlines()
        msg = err[-1] if err else "scan failed"
        if "not permitted" in msg.lower() or "operation not permitted" in msg.lower():
            msg += " — run: sudo python3 marksman.py"
        raise RadioError(msg)
    return parse_iw_scan(proc.stdout or "")


def _busctl_json(args: list[str], timeout: float = 5) -> tuple[int, object, str]:
    proc = run_cmd(args, timeout=timeout)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "busctl failed").strip()
        return proc.returncode, None, err
    try:
        return 0, json.loads(proc.stdout or "null"), ""
    except json.JSONDecodeError:
        return 1, None, "busctl did not return JSON"


def parse_managed_objects(payload: object, iface: str) -> list[Hit]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    paths: dict = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                paths.update(item)
    elif isinstance(data, dict):
        paths = data
    prefix = f"/org/bluez/{iface}/dev_"
    hits: list[Hit] = []
    for path, ifaces in paths.items():
        if not isinstance(path, str) or not path.startswith(prefix) or not isinstance(ifaces, dict):
            continue
        dev = ifaces.get("org.bluez.Device1")
        if not isinstance(dev, dict):
            continue
        hits.append(_hit_from_device(dev, path))
    return hits


def _variant(dev: dict, key: str):
    raw = dev.get(key)
    if isinstance(raw, dict) and "data" in raw:
        return raw.get("data")
    return None


def _hit_from_device(dev: dict, path: str = "") -> Hit:
    mac = str(_variant(dev, "Address") or "")
    if not mac and path:
        tail = path.rsplit("/", 1)[-1]
        if tail.startswith("dev_"):
            mac = tail[4:].replace("_", ":")
    mac = (normalize_mac(mac) or mac).upper()
    name = usable_name(str(_variant(dev, "Name") or ""), mac)
    if not name:
        name = usable_name(str(_variant(dev, "Alias") or ""), mac)
    rssi_raw = _variant(dev, "RSSI")
    rssi = None
    try:
        if rssi_raw is not None:
            rssi = float(rssi_raw)
    except (TypeError, ValueError):
        rssi = None
    if rssi is not None and (rssi >= 0.0 or rssi < -120.0):
        rssi = None
    tx_raw = _variant(dev, "TxPower")
    advertised = None
    try:
        if tx_raw is not None:
            advertised = float(tx_raw)
    except (TypeError, ValueError):
        advertised = None
    addr_type = str(_variant(dev, "AddressType") or "")
    hint = "ble" if addr_type else "bt-classic"
    return Hit(
        mac=mac,
        name=name,
        rssi=rssi,
        freq_mhz=BT_FREQ_MHZ,
        phy_hint=hint,
        advertised_tx=advertised,
        address_type=addr_type,
    )


class Bluez:
    def __init__(self, iface: str) -> None:
        self.iface = iface
        self.path = f"/org/bluez/{iface}"
        self.busctl = which("busctl") or "busctl"
        self.was_discovering = False
        self.started = False
        self.note = ""

    def _adapter_prop(self, prop: str):
        code, payload, _err = _busctl_json(
            [self.busctl, "--json=short", "get-property", "org.bluez", self.path, "org.bluez.Adapter1", prop]
        )
        if code != 0 or not isinstance(payload, dict):
            return None
        return payload.get("data")

    def open(self) -> None:
        if not which("busctl"):
            raise RadioError("busctl is not installed (it comes with systemd / BlueZ).")
        powered = self._adapter_prop("Powered")
        if powered is not True:
            proc = run_cmd(
                [self.busctl, "set-property", "org.bluez", self.path, "org.bluez.Adapter1", "Powered", "b", "true"]
            )
            if proc.returncode != 0:
                raise RadioError((proc.stderr or "could not power on the Bluetooth controller").strip())
        self.was_discovering = self._adapter_prop("Discovering") is True
        if self.was_discovering:
            run_cmd([self.busctl, "call", "org.bluez", self.path, "org.bluez.Adapter1", "StopDiscovery"], timeout=4)
        filt = run_cmd(
            [
                self.busctl,
                "call",
                "org.bluez",
                self.path,
                "org.bluez.Adapter1",
                "SetDiscoveryFilter",
                "a{sv}",
                "2",
                "Transport",
                "s",
                "auto",
                "DuplicateData",
                "b",
                "false",
            ]
        )
        if filt.returncode != 0:
            self.note = (filt.stderr or "discovery filter was not accepted").strip().splitlines()[-1][:120]
        start = run_cmd([self.busctl, "call", "org.bluez", self.path, "org.bluez.Adapter1", "StartDiscovery"])
        if start.returncode != 0:
            err = (start.stderr or start.stdout or "StartDiscovery failed").strip()
            if "InProgress" not in err and "Already" not in err:
                raise RadioError(err.splitlines()[-1][:200])
        self.started = True

    def close(self) -> None:
        if not self.started:
            return
        # Leave the controller as we found it when something else was already discovering.
        if not self.was_discovering:
            run_cmd([self.busctl, "call", "org.bluez", self.path, "org.bluez.Adapter1", "StopDiscovery"], timeout=4)
        self.started = False

    def devices(self) -> list[Hit]:
        code, payload, err = _busctl_json(
            [
                self.busctl,
                "--json=short",
                "call",
                "org.bluez",
                "/",
                "org.freedesktop.DBus.ObjectManager",
                "GetManagedObjects",
            ]
        )
        if code != 0:
            self.note = err.splitlines()[-1][:120] if err else "no device list"
            return []
        self.note = ""
        return parse_managed_objects(payload, self.iface)

    def rssi(self, mac: str) -> float | None:
        path = f"{self.path}/dev_{mac.replace(':', '_')}"
        code, payload, _err = _busctl_json(
            [self.busctl, "--json=short", "get-property", "org.bluez", path, "org.bluez.Device1", "RSSI"],
            timeout=3,
        )
        if code != 0 or not isinstance(payload, dict):
            return None
        try:
            value = float(payload.get("data"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if value >= 0.0 or value < -120.0:
            return None
        return value


def bt_match(hit: Hit, target: Target) -> bool:
    if target.mac and hit.mac == target.mac:
        return True
    if target.mode == "name" and not target.mac:
        have = hit.name.casefold()
        want = target.value.casefold()
        if not have or not want:
            return False
        return have == want or (len(want) >= 2 and want in have)
    return False


class BtSource:
    def __init__(self, bluez: Bluez, target: Target) -> None:
        self.bluez = bluez
        self.target = target
        self.status = "discovering"
        self.note = ""
        self.last_poll = 0.0
        self.last_full = 0.0
        self._cached: Hit | None = None

    def poll(self) -> Sample | None:
        now = time.monotonic()
        if now - self.last_poll < 0.12:
            return None
        self.last_poll = now
        if self.target.mac and now - self.last_full < 1.0:
            rssi = self.bluez.rssi(self.target.mac)
            if rssi is not None and self._cached is not None:
                self.status = "locked"
                self.note = self.bluez.note
                return self._sample(self._cached, rssi)
        devices = self.bluez.devices()
        self.last_full = now
        self.note = self.bluez.note
        matches = [hit for hit in devices if bt_match(hit, self.target) and hit.rssi is not None]
        if not matches:
            self.status = "searching" if not self.target.mac else "waiting for RSSI"
            return None
        best = max(matches, key=lambda hit: hit.rssi or -999)
        if self.target.mode == "name" and not self.target.mac:
            self.target.mac = best.mac
        self._cached = best
        self.status = "locked"
        return self._sample(best, float(best.rssi or 0))

    def _sample(self, hit: Hit, rssi: float) -> Sample:
        tx, assumed = hit.tx()
        label = self.target.display or hit.name or hit.mac
        return Sample(
            rssi=rssi,
            freq_mhz=BT_FREQ_MHZ,
            mac=hit.mac,
            label=label,
            tx_dbm=tx,
            tx_assumed=assumed,
            channel=None,
            phy_hint=hit.phy_hint,
        )


def format_hit(hit: Hit) -> str:
    rssi = f"{hit.rssi:.0f} dBm" if hit.rssi is not None else "   — dBm"
    dist = format_distance_human(hit.distance())
    name = clip(hit.name or "(hidden)", 22)
    channel = f"ch {hit.channel}" if hit.channel else ""
    return f"{name:<22} {rssi:>8}  {dist:>8}  {hit.mac}  {channel}".rstrip()


def print_target_page(kind: str, rows: list[Hit], heard: int) -> None:
    title = "Wi-Fi SSIDs" if kind == "wifi" else "Bluetooth names"
    manual = "enter an SSID" if kind == "wifi" else "enter a device name"
    print()
    print(Colors.wrap(PINK, "┌" + "─" * 72 + "┐"))
    print(Colors.wrap(PINK, "│ ") + Colors.wrap(BOLD + CREAM, clip(f"THE MARKSMAN   {title}", 70).ljust(70)) + Colors.wrap(PINK, " │"))
    print(Colors.wrap(PINK, "└" + "─" * 72 + "┘"))
    if kind == "wifi":
        print(f"  Heard {heard} named SSIDs. The list is the five strongest.")
    else:
        print(f"  Heard {heard} named devices. The list is the five strongest.")
    print(Colors.wrap(GOLD, "  0) rescan"))
    for idx in range(1, 6):
        if idx <= len(rows):
            print(Colors.wrap(CYAN, f"  {idx}) {format_hit(rows[idx - 1])}"))
        else:
            print(Colors.wrap(DIMC, f"  {idx}) —"))
    print(Colors.wrap(PINK, f"  6) {manual}"))
    print(Colors.wrap(PINK, "  7) enter a MAC address"))
    print(Colors.wrap(DIMC, "  q) quit"))


def prompt_menu(kind: str, rows: list[Hit], heard: int) -> tuple[str, int | None]:
    while True:
        print_target_page(kind, rows, heard)
        if not sys.stdin.isatty():
            return "quit", None
        raw = input("Select [0 rescan, 1-5, 6 manual, 7 MAC, q quit]: ").strip()
        action, slot = interpret_choice(raw)
        if action == "invalid":
            print("  Enter 0, 1-5, 6, 7, or q.")
            continue
        if action == "slot" and (slot is None or slot >= len(rows)):
            print("  Nothing in that slot. Rescan, or use 6 or 7.")
            continue
        return action, slot


def find_ssid(hits: list[Hit], ssid: str) -> Hit | None:
    best = None
    for hit in hits:
        if hit.name != ssid:
            continue
        if best is None or (hit.rssi or -999) > (best.rssi or -999):
            best = hit
    return best


def find_mac(hits: list[Hit], mac: str) -> Hit | None:
    for hit in hits:
        if hit.mac == mac:
            return hit
    return None


def find_bt_name(hits: list[Hit], name: str) -> Hit | None:
    want = name.casefold()
    exact = [hit for hit in hits if hit.name.casefold() == want]
    pool = exact or [hit for hit in hits if want in hit.name.casefold()]
    if not pool:
        return None
    pool.sort(key=lambda hit: hit.rssi if hit.rssi is not None else -999, reverse=True)
    return pool[0]


def target_from_hit(hit: Hit, mode: str) -> Target:
    display = hit.name or hit.mac
    value = hit.mac if mode == "mac" else hit.name
    return Target(
        mode=mode,
        value=value,
        mac=hit.mac,
        channel=hit.channel,
        freq_mhz=hit.freq_mhz,
        phy_hint=hit.phy_hint,
        display=display,
        advertised_tx=hit.advertised_tx,
    )


def ask_ssid(hits: list[Hit]) -> Target | None:
    while True:
        text = ask_text("SSID")
        if len(text.encode("utf-8")) > 32:
            print("  An SSID is at most 32 bytes.")
            continue
        found = find_ssid(hits, text)
        if found:
            return target_from_hit(found, "ssid")
        return Target(mode="ssid", value=text, display=text, phy_hint="wifi-ap")


def ask_mac(hits: list[Hit], kind: str) -> Target | None:
    while True:
        text = ask_text("MAC address")
        mac = normalize_mac(text)
        if not mac:
            print("  Enter six octets, for example AA:BB:CC:DD:EE:FF.")
            continue
        found = find_mac(hits, mac)
        if found:
            mode_hit = target_from_hit(found, "mac")
            mode_hit.display = found.name or mac
            return mode_hit
        hint = "ble" if kind == "bluetooth" else "wifi-client"
        freq = BT_FREQ_MHZ if kind == "bluetooth" else None
        return Target(mode="mac", value=mac, mac=mac, display=mac, phy_hint=hint, freq_mhz=freq)


def ask_bt_name(hits: list[Hit]) -> Target:
    while True:
        text = ask_text("Device name")
        if len(text) > 128:
            print("  That name is too long.")
            continue
        found = find_bt_name(hits, text)
        if found:
            target = target_from_hit(found, "name")
            target.value = text
            target.display = found.name
            return target
        return Target(mode="name", value=text, display=text, phy_hint="ble", freq_mhz=BT_FREQ_MHZ)


def give_to_user(path: Path) -> None:
    user = os.environ.get("SUDO_USER")
    if not user or os.geteuid() != 0:
        return
    try:
        info = pwd.getpwnam(user)
    except KeyError:
        return
    for root, dirs, files in os.walk(path):
        os.chown(root, info.pw_uid, info.pw_gid)
        for name in list(dirs) + list(files):
            try:
                os.chown(os.path.join(root, name), info.pw_uid, info.pw_gid)
            except OSError:
                pass


def write_reports(outdir: Path, session: dict, stats_book: dict[str, HuntStats]) -> None:
    lines = [
        f"{TOOL_NAME} {VERSION}",
        f"Scan: {session.get('scan_name', '')}",
        f"Operator: {session.get('operator_name', '')}  ({session.get('userid', '')})",
        f"Authorization: {session.get('roe_method', '')}",
        f"Radio: {session.get('phy', '')}  {session.get('iface', '')}",
        f"Started: {session.get('generated_start', '')}",
        f"Ended: {session.get('generated_end', '')}",
        "",
        f"Distance model: log-distance, n={PATH_LOSS_EXPONENT}. Same formula as The Magic Flute.",
        "Read closest / furthest as near, mid, or far. A 10 dB error scales the range by about 2.3×.",
        "",
    ]
    if not stats_book:
        lines.append("No target was locked.")
    for key, stats in stats_book.items():
        lines.append(f"Target {key}")
        lines.append(f"  samples: {stats.frames}")
        if stats.min_rssi is None:
            lines.append("  the sight never heard this target")
        else:
            lines.append(
                f"  smooth signal {stats.min_rssi:.1f} .. {stats.max_rssi:.1f} dBm"
            )
            lines.append(
                f"  closest {format_distance_human(stats.closest_m)}   "
                f"furthest {format_distance_human(stats.furthest_m)}"
            )
            lines.append(f"  first {stats.first_heard}   last {stats.last_heard}")
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    (outdir / "marksman_report.txt").write_text(text, encoding="utf-8")
    md = ["# The Marksman", ""]
    md.append(f"- Scan: {session.get('scan_name', '')}")
    md.append(f"- Operator: {session.get('operator_name', '')} ({session.get('userid', '')})")
    md.append(f"- Authorization: {session.get('roe_method', '')}")
    md.append(f"- Radio: {session.get('phy', '')} `{session.get('iface', '')}`")
    md.append("")
    md.append("Distance is the log-distance model from The Magic Flute, `n = 2.7`.")
    md.append("")
    if stats_book:
        md.append("| Target | Samples | Strongest dBm | Weakest dBm | Closest | Furthest |")
        md.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for key, stats in stats_book.items():
            strong = "" if stats.max_rssi is None else f"{stats.max_rssi:.1f}"
            weak = "" if stats.min_rssi is None else f"{stats.min_rssi:.1f}"
            md.append(
                f"| {key} | {stats.frames} | {strong} | {weak} | "
                f"{format_distance_human(stats.closest_m)} | {format_distance_human(stats.furthest_m)} |"
            )
        md.append("")
    else:
        md.append("No target was locked.")
        md.append("")
    (outdir / "marksman_report.md").write_text("\n".join(md), encoding="utf-8")


def collect_bt(bluez: Bluez, window: float) -> list[Hit]:
    table: dict[str, Hit] = {}
    deadline = time.monotonic() + window
    while True:
        for hit in bluez.devices():
            if hit.mac:
                table[hit.mac] = hit
        remaining = deadline - time.monotonic()
        named = len(top_named(list(table.values()), limit=1000))
        print(
            f"\r  discovering… {len(table)} devices, {named} named, {max(0, remaining):.0f}s left   ",
            end="",
            flush=True,
        )
        if remaining <= 0:
            break
        time.sleep(min(0.8, remaining))
    print()
    return list(table.values())


def open_session(path: Path, session: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "operator_session.json").write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")


def os_release() -> dict[str, str]:
    info: dict[str, str] = {}
    path = Path("/etc/os-release")
    if not path.exists():
        return info
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, val = line.split("=", 1)
        info[key] = val.strip().strip('"')
    return info


def package_manager_for(info: dict[str, str]) -> str:
    os_id = (info.get("ID") or "").lower()
    like = (info.get("ID_LIKE") or "").lower()
    if os_id in {"debian", "ubuntu", "linuxmint", "pop", "raspbian", "kali"} or "debian" in like:
        return "apt"
    if os_id in {"fedora", "rhel", "centos", "rocky", "almalinux"} or "fedora" in like or "rhel" in like:
        return "dnf"
    if os_id in {"arch", "manjaro", "endeavouros"} or "arch" in like:
        return "pacman"
    return ""


def packages_for(manager: str) -> list[str]:
    if manager == "apt":
        return list(DEB_PACKAGES)
    if manager == "dnf":
        return list(FEDORA_PACKAGES)
    if manager == "pacman":
        return list(ARCH_PACKAGES)
    return []


def real_username() -> str:
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name


def user_group_names(username: str) -> set[str]:
    names: set[str] = set()
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        return names
    try:
        names.add(grp.getgrgid(pw.pw_gid).gr_name)
    except KeyError:
        pass
    try:
        extra = os.getgrouplist(username, pw.pw_gid)
    except (AttributeError, OSError):
        extra = []
        for entry in grp.getgrall():
            if username in entry.gr_mem:
                extra.append(entry.gr_gid)
    for gid in extra:
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            pass
    return names


def group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def apt_package_known(name: str) -> bool:
    if not which("apt-cache"):
        return True
    proc = run_cmd(["apt-cache", "show", name], timeout=30)
    return proc.returncode == 0 and "Package:" in (proc.stdout or "")


def _apt_env() -> dict[str, str]:
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["NEEDRESTART_MODE"] = "a"
    return env


def install_packages() -> bool:
    info = os_release()
    manager = package_manager_for(info)
    packages = packages_for(manager)
    if not manager:
        distro = info.get("PRETTY_NAME") or info.get("ID") or "this distro"
        log(
            f"No installer for {distro}. Install iw, iproute2, rfkill, bluez, and NetworkManager yourself.",
            "err",
        )
        return False
    if os.geteuid() != 0:
        log(f"Package install needs root. Re-run: sudo python3 {Path(sys.argv[0]).name} --setup", "err")
        return False
    if manager == "apt":
        env = _apt_env()
        log("Downloading package lists (apt-get update)…")
        upd = run_cmd(["apt-get", "update"], timeout=300, env=env)
        if upd.returncode != 0:
            log((upd.stderr or upd.stdout or "apt-get update failed").strip()[:1500], "warn")
        known = [pkg for pkg in packages if apt_package_known(pkg)]
        missing = [pkg for pkg in packages if pkg not in known]
        if missing:
            log(f"Not in the apt cache, skipped: {' '.join(missing)}", "warn")
        packages = known
        if not packages:
            log("apt does not know any of the required packages.", "err")
            return False
        log(f"Installing with apt: {' '.join(packages)}")
        proc = run_cmd(
            [
                "apt-get",
                "install",
                "-y",
                "-o",
                "Dpkg::Options::=--force-confdef",
                "-o",
                "Dpkg::Options::=--force-confold",
                *packages,
            ],
            timeout=600,
            env=env,
        )
    elif manager == "dnf":
        log(f"Installing with dnf: {' '.join(packages)}")
        proc = run_cmd(["dnf", "install", "-y", *packages], timeout=600)
    else:
        log(f"Installing with pacman: {' '.join(packages)}")
        proc = run_cmd(["pacman", "-Sy", "--noconfirm", *packages], timeout=600)
    if proc.returncode != 0:
        log((proc.stderr or proc.stdout or "package install failed").strip()[:2000], "err")
        return all(which(name) for name in REQUIRED_TOOLS)
    log("Packages installed.", "ok")
    return True


def enable_bluetooth_service() -> None:
    systemctl = which("systemctl")
    if not systemctl:
        log("systemctl is not available. Start bluetoothd yourself if Bluetooth hunts fail.", "warn")
        return
    proc = run_cmd([systemctl, "enable", "--now", "bluetooth.service"], timeout=40)
    if proc.returncode == 0:
        log("bluetooth.service is enabled and running.", "ok")
        return
    detail = (proc.stderr or proc.stdout or "could not start bluetooth.service").strip()
    log(detail[:500], "warn")


def ensure_bluetooth_group(username: str) -> None:
    if not username or username == "root":
        log("No login user to add to the bluetooth group.", "warn")
        return
    if not group_exists("bluetooth"):
        log("No bluetooth group on this system. BlueZ may still allow your user through polkit.", "warn")
        return
    if "bluetooth" in user_group_names(username):
        log(f"{username} is already in the bluetooth group.", "ok")
        return
    proc = run_cmd(["usermod", "-aG", "bluetooth", username])
    if proc.returncode == 0:
        log(f"Added {username} to bluetooth. Log out and back in so that group applies.", "ok")
    else:
        log((proc.stderr or "usermod bluetooth failed").strip()[:400], "warn")


def tools_missing() -> list[str]:
    return [name for name in REQUIRED_TOOLS if not which(name)]


def run_setup() -> int:
    print()
    log(
        "Setup downloads and installs iw, iproute2, rfkill, BlueZ (bluetoothctl), "
        "and NetworkManager (nmcli). busctl comes with systemd.",
        "hdr",
    )
    if os.geteuid() != 0:
        log(
            f"Re-run as root: sudo python3 {Path(sys.argv[0]).name} --setup",
            "err",
        )
        missing = tools_missing()
        if missing:
            log("Missing now: " + ", ".join(missing), "warn")
        else:
            log("The required tools are already on PATH. Setup still needs root to refresh them.", "warn")
        return 2
    ok = install_packages()
    enable_bluetooth_service()
    ensure_bluetooth_group(real_username())
    print()
    code = run_check()
    if not ok or tools_missing():
        log("Setup did not leave every required tool on PATH.", "err")
        return 1
    log("Setup finished.", "ok")
    log(f"Then run: sudo python3 {Path(sys.argv[0]).name}")
    return 0 if code == 0 else code


def run_check(list_only: bool = False) -> int:
    print(f"Python {sys.version.split()[0]}")
    ok = True
    for name in (*REQUIRED_TOOLS, *OPTIONAL_TOOLS):
        path = which(name)
        mark = path or "missing"
        if name in REQUIRED_TOOLS and not path:
            ok = False
        print(f"  {name:<12} {mark}")
    print()
    log("Wi-Fi controllers", "hdr")
    wifi = list_wifi_controllers()
    if not wifi:
        print("  none found")
    for item in wifi:
        print(f"  {item['label']}" + ("  (preferred)" if item.get("recommended") else ""))
        for line in item["detail"]:
            print(f"      {line}")
    print()
    log("Bluetooth controllers", "hdr")
    bluetooth = list_bt_controllers()
    if not bluetooth:
        print("  none found")
    for item in bluetooth:
        print(f"  {item['label']}" + ("  (preferred)" if item.get("recommended") else ""))
        for line in item["detail"]:
            print(f"      {line}")
    print()
    if list_only:
        return 0
    if os.geteuid() != 0:
        log("Wi-Fi monitor mode and iw scan need root. Run: sudo python3 marksman.py", "warn")
    if ok:
        log("Tools are present. Authorization is still required before a hunt.", "ok")
        return 0
    log(
        f"A required tool is missing. Run: sudo python3 {Path(sys.argv[0]).name} --setup",
        "err",
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="The Marksman — hunt one Wi-Fi SSID/MAC or one Bluetooth name/MAC."
    )
    parser.add_argument("--i-have-roe", action="store_true", help="confirm written authorization")
    parser.add_argument("--operator-name", default="", help="operator full name")
    parser.add_argument("--userid", default="", help="user id or employee id")
    parser.add_argument("--scan-name", default="", help="name of this scan")
    parser.add_argument("--phy", choices=["wifi", "bluetooth"], help="which radio to hunt")
    parser.add_argument("--iface", default="", help="wi-fi iface or hci name")
    parser.add_argument("--ssid", default="", help="Wi-Fi SSID to lock")
    parser.add_argument("--mac", default="", help="MAC address to lock")
    parser.add_argument("--bt-name", default="", help="Bluetooth device name to lock")
    parser.add_argument("--duration", type=float, default=0, help="stop the sight after N seconds")
    parser.add_argument("--scan-window", type=float, default=8, help="seconds to listen before the Bluetooth list")
    parser.add_argument("--dry-run", action="store_true", help="record the plan and do not touch the radio")
    parser.add_argument(
        "--setup",
        action="store_true",
        help="download and install iw, BlueZ, rfkill, and NetworkManager (needs sudo)",
    )
    parser.add_argument("--list-only", action="store_true", help="list controllers and exit")
    parser.add_argument("--check", action="store_true", help="check tools and controllers")
    parser.add_argument("--self-test", action="store_true", help="run the offline self-test")
    parser.add_argument("-o", "--output", default="", help="session directory")
    return parser


def gather_operator(args: argparse.Namespace) -> dict:
    name = args.operator_name or ask_text("Operator name")
    userid = args.userid or ask_text("UserID or employee ID")
    scan = args.scan_name or ask_text("Scan name")
    return {"operator_name": name, "userid": userid, "scan_name": scan}


def pick_controller(kind: str, requested: str) -> dict:
    items = list_wifi_controllers() if kind == "wifi" else list_bt_controllers()
    if requested:
        for item in items:
            if item["iface"] == requested or item.get("mac", "").upper() == requested.upper():
                return item
        raise RadioError(f"No {kind} controller named {requested}.")
    if not items:
        noun = "Wi-Fi" if kind == "wifi" else "Bluetooth"
        raise RadioError(f"No {noun} controller found.")
    if not sys.stdin.isatty():
        return items[0]
    title = "Choose a Wi-Fi controller" if kind == "wifi" else "Choose a Bluetooth controller"
    return ask_choice(title, items)


def preset_target(args: argparse.Namespace, kind: str) -> Target | None:
    chosen = [bool(args.ssid), bool(args.mac), bool(args.bt_name)]
    if sum(chosen) > 1:
        raise RadioError("Pass only one of --ssid, --mac, or --bt-name.")
    if args.ssid:
        if kind != "wifi":
            raise RadioError("--ssid is a Wi-Fi target.")
        return Target(mode="ssid", value=args.ssid, display=args.ssid, phy_hint="wifi-ap")
    if args.bt_name:
        if kind != "bluetooth":
            raise RadioError("--bt-name is a Bluetooth target.")
        return Target(
            mode="name",
            value=args.bt_name,
            display=args.bt_name,
            phy_hint="ble",
            freq_mhz=BT_FREQ_MHZ,
        )
    if args.mac:
        mac = normalize_mac(args.mac)
        if not mac:
            raise RadioError("--mac needs six octets.")
        hint = "ble" if kind == "bluetooth" else "wifi-client"
        freq = BT_FREQ_MHZ if kind == "bluetooth" else None
        return Target(mode="mac", value=mac, mac=mac, display=mac, phy_hint=hint, freq_mhz=freq)
    return None


def hunt_one(source, state: SightState, stats: HuntStats, track: TrackLog | None, duration: float) -> str:
    print()
    log(f"Sight up. {state.label} on {state.iface}. 0 rescans, b goes back, q quits.", "ok")
    if not sys.stdout.isatty():
        log("No TTY, so the sight prints a line per second instead of the full screen.", "warn")
    return run_sight(source, state, stats, track, duration)


def wifi_flow(ctrl: dict, session: dict, outdir: Path, args: argparse.Namespace, preset: Target | None) -> dict[str, HuntStats]:
    book: dict[str, HuntStats] = {}
    track = TrackLog(outdir / "marksman_track.csv")
    port = WifiPort(ctrl["iface"], list(ctrl.get("bands") or []))
    hits: list[Hit] = []
    need_scan = preset is None
    try:
        while True:
            target = preset
            if target is None:
                if need_scan:
                    log(f"Scanning Wi-Fi on {ctrl['iface']}…", "info")
                    try:
                        hits = wifi_scan(ctrl["iface"])
                    except RadioError as exc:
                        log(str(exc), "err")
                        hits = []
                    need_scan = False
                rows = top_ssids(hits)
                named = len({hit.name for hit in hits if hit.name})
                action, slot = prompt_menu("wifi", rows, named)
                if action in {"quit", "back"}:
                    break
                if action == "rescan":
                    need_scan = True
                    continue
                if action == "slot" and slot is not None:
                    target = target_from_hit(rows[slot], "ssid")
                elif action == "manual-name":
                    target = ask_ssid(hits)
                elif action == "manual-mac":
                    target = ask_mac(hits, "wifi")
                else:
                    break
            if target is None:
                break
            session["target_mode"] = target.mode
            session["target"] = target.value
            session["target_mac"] = target.mac
            mode = "monitor"
            try:
                port.open_monitor(target.channel)
                if target.channel is None and port.monitor:
                    port.tune(port.channel or HOP_24[0])
            except RadioError as exc:
                log(f"Monitor mode unavailable ({exc}). Falling back to a slow iw scan.", "warn")
                port.close()
                mode = "scan"
            source = WifiSource(port, target, mode)
            if mode == "monitor":
                if target.channel is None and not port.channel:
                    source._hop()
                source.last_tune = time.monotonic()
            stats = book.setdefault(target.key(), HuntStats(target.key()))
            state = SightState(session["scan_name"], session["operator_name"], "Wi-Fi", ctrl["iface"], target)
            reason = hunt_one(source, state, stats, track, args.duration)
            port.close()
            if preset is not None or reason == "quit" or args.duration:
                break
            need_scan = reason == "rescan"
    finally:
        port.close()
        track.close()
    return book


def bluetooth_flow(ctrl: dict, session: dict, outdir: Path, args: argparse.Namespace, preset: Target | None) -> dict[str, HuntStats]:
    book: dict[str, HuntStats] = {}
    track = TrackLog(outdir / "marksman_track.csv")
    bluez = Bluez(ctrl["iface"])
    window = min(60.0, max(1.0, float(args.scan_window)))
    hits: list[Hit] = []
    need_scan = preset is None
    try:
        bluez.open()
        while True:
            target = preset
            if target is None:
                if need_scan:
                    log(f"Discovering Bluetooth on {ctrl['iface']} for {window:.0f}s…", "info")
                    hits = collect_bt(bluez, window)
                    need_scan = False
                else:
                    fresh = bluez.devices()
                    if fresh:
                        hits = fresh
                rows = top_named(hits)
                named = len([hit for hit in hits if usable_name(hit.name, hit.mac)])
                action, slot = prompt_menu("bluetooth", rows, named)
                if action in {"quit", "back"}:
                    break
                if action == "rescan":
                    need_scan = True
                    continue
                if action == "slot" and slot is not None:
                    target = target_from_hit(rows[slot], "name")
                elif action == "manual-name":
                    target = ask_bt_name(hits)
                elif action == "manual-mac":
                    target = ask_mac(hits, "bluetooth")
                else:
                    break
            if target is None:
                break
            session["target_mode"] = target.mode
            session["target"] = target.value
            session["target_mac"] = target.mac
            stats = book.setdefault(target.key(), HuntStats(target.key()))
            state = SightState(
                session["scan_name"], session["operator_name"], "Bluetooth", ctrl["iface"], target
            )
            source = BtSource(bluez, target)
            reason = hunt_one(source, state, stats, track, args.duration)
            if preset is not None or reason == "quit" or args.duration:
                break
            need_scan = reason == "rescan"
    finally:
        bluez.close()
        track.close()
    return book


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return self_test()
    if not sys.platform.startswith("linux"):
        log("The Marksman runs on Linux.", "err")
        return 1
    if sys.version_info < MIN_PYTHON:
        log("Python 3.9 or newer is required.", "err")
        return 1
    print_banner()
    if args.setup:
        return run_setup()
    if args.check or args.list_only:
        return run_check(list_only=args.list_only)
    try:
        method = confirm_roe(args.i_have_roe)
        operator = gather_operator(args)
        kind = args.phy
        if not kind:
            print()
            log("Hunt which radio?", "hdr")
            print("  1) Wi-Fi")
            print("  2) Bluetooth")
            while True:
                raw = ask_text("Select", default="1")
                if raw in {"1", "wifi", "wi-fi", "w"}:
                    kind = "wifi"
                    break
                if raw in {"2", "bluetooth", "bt", "b"}:
                    kind = "bluetooth"
                    break
                print("  Enter 1 or 2.")
        ctrl = pick_controller(kind, args.iface)
        preset = preset_target(args, kind)
    except RadioError as exc:
        log(str(exc), "err")
        return 1
    session = {
        "tool": "marksman",
        "tool_name": TOOL_NAME,
        "version": VERSION,
        "generated_start": iso_utc(),
        "operator_name": operator["operator_name"],
        "userid": operator["userid"],
        "scan_name": operator["scan_name"],
        "roe_acknowledged": True,
        "roe_method": method,
        "sudo_user": os.environ.get("SUDO_USER") or os.environ.get("USER") or "",
        "host": socket.gethostname(),
        "phy": kind,
        "iface": ctrl["iface"],
        "iface_mac": ctrl.get("mac") or "",
        "distance_model": "log-distance",
        "path_loss_exponent": PATH_LOSS_EXPONENT,
        "injection": False,
        "pairing": False,
        "target_mode": preset.mode if preset else "",
        "target": preset.value if preset else "",
        "target_mac": preset.mac if preset else "",
    }
    if args.dry_run:
        print()
        log("Dry run. The radio was not changed.", "ok")
        print(json.dumps(session, indent=2))
        if preset is None:
            print("No target flag was passed. An interactive run would scan and show the list.")
        return 0
    outdir = Path(args.output) if args.output else Path.cwd() / f"marksman_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    hold = RfkillHold()
    book: dict[str, HuntStats] = {}
    try:
        names = [ctrl["iface"]]
        if kind == "wifi" and ctrl.get("phy"):
            names.append(ctrl["phy"])
        hold.open(names)
        open_session(outdir, session)
        log(f"Session directory: {outdir}", "ok")
        if kind == "wifi":
            book = wifi_flow(ctrl, session, outdir, args, preset)
        else:
            book = bluetooth_flow(ctrl, session, outdir, args, preset)
        return 0
    except KeyboardInterrupt:
        log("Stopped.", "warn")
        return 130
    except RadioError as exc:
        log(str(exc), "err")
        return 1
    finally:
        hold.close()
        session["generated_end"] = iso_utc()
        if outdir.exists():
            (outdir / "operator_session.json").write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
            write_reports(outdir, session, book)
            give_to_user(outdir)


def self_test() -> int:
    failures: list[str] = []

    def check(cond: bool, message: str) -> None:
        if not cond:
            failures.append(message)

    freq = 2437.0
    fspl = 20.0 * math.log10(freq) - 27.55
    expect = 10 ** ((20.0 - fspl - (-50.0)) / (10.0 * PATH_LOSS_EXPONENT))
    got = estimate_distance_m(-50, freq, 20.0)
    check(got is not None and abs(got - expect) < 1e-9, "wifi distance formula")
    check(abs((got or 0) - 12.71) < 0.02, "wifi -50 dBm is about 12.7 m")
    ble = estimate_distance_m(-60, 2402, 0.0)
    check(ble is not None and abs(ble - 5.48) < 0.02, "ble -60 dBm is about 5.5 m")
    check(estimate_distance_m(0, 2437, 20) is None, "reject 0 dBm")
    check(estimate_distance_m(-50, 2437000, 20) is not None, "kHz frequency")
    tx, assumed = resolve_tx("wifi-ap", 12)
    check(tx == 12 and not assumed, "advertised tx")
    tx, assumed = resolve_tx("ble", 0)
    check(tx == 0 and assumed, "ble assumed tx")
    tx, assumed = resolve_tx("bt-classic", None)
    check(tx == 4 and assumed, "classic assumed tx")
    check(normalize_mac("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF", "mac normalize")
    check(normalize_mac("nope") is None, "mac reject")
    check(channel_for_mhz(2412) == 1 and mhz_for_channel(6) == 2437, "channel map")
    check(interpret_choice("0") == ("rescan", None), "choice 0")
    check(interpret_choice("5") == ("slot", 4), "choice 5")
    check(interpret_choice("6") == ("manual-name", None), "choice 6")
    check(interpret_choice("7") == ("manual-mac", None), "choice 7")
    check(interpret_choice("8") == ("invalid", None), "choice 8")
    check(interpret_choice("q") == ("quit", None), "choice q")

    scan = """
BSS aa:bb:cc:dd:ee:01(on wlx0)
	freq: 2412
	signal: -42.00 dBm
	SSID: Home
	DS Parameter set: channel 1
BSS aa:bb:cc:dd:ee:02(on wlx0)
	freq: 5180
	signal: -70.00 dBm
	SSID: Home
BSS aa:bb:cc:dd:ee:03(on wlx0)
	freq: 2437
	signal: -55 dBm
	SSID: Guest
BSS aa:bb:cc:dd:ee:04(on wlx0)
	freq: 2462
	signal: -81.00 dBm
	SSID:
BSS aa:bb:cc:dd:ee:05(on wlx0)
	freq: 2462
	signal: -30.00 dBm
	SSID: Zoo
"""
    hits = parse_iw_scan(scan)
    check(len(hits) == 5, "iw scan count")
    top = top_ssids(hits)
    check([hit.name for hit in top] == ["Zoo", "Home", "Guest"], "ssid rank")
    check(top[1].mac == "AA:BB:CC:DD:EE:01", "strongest BSSID kept for an SSID")
    check(all(hit.name for hit in top), "hidden ssid dropped")

    def rt(present_words: list[int], fields: bytes) -> bytes:
        blob = b"".join(struct.pack("<I", word) for word in present_words)
        it_len = 4 + len(blob) + len(fields)
        return struct.pack("<BBH", 0, 0, it_len) + blob + fields

    parsed = parse_radiotap(rt([0x22], bytes([0x00, 256 - 42])))
    check(parsed is not None and parsed[1] == -42, "radiotap signal -42")
    parsed = parse_radiotap(rt([0x21], b"\x00" * 8 + bytes([256 - 55])))
    check(parsed is not None and parsed[1] == -55, "radiotap after tsft")
    check(parse_radiotap(rt([0x22], bytes([0x40, 256 - 40]))) is None, "bad fcs dropped")
    parsed = parse_radiotap(rt([(1 << 31) | (1 << 5), 0], bytes([256 - 33])))
    check(parsed is not None and parsed[1] == -33, "extended present bitmap")

    bssid = bytes.fromhex("aabbccddeeff")
    ssid = b"TestNet"
    beacon = struct.pack("<H", 0x0080) + b"\x00\x00" + (b"\xff" * 6) + bssid + bssid + b"\x00\x00"
    beacon += b"\x00" * 8 + struct.pack("<HH", 100, 0x0011)
    beacon += bytes([0, len(ssid)]) + ssid
    packet = rt([0x22], bytes([0x00, 256 - 48])) + beacon
    frame = parse_wifi_packet(packet)
    check(frame is not None and frame["ssid"] == "TestNet", "beacon ssid")
    check(frame is not None and frame["transmitter"] == "AA:BB:CC:DD:EE:FF", "beacon transmitter")
    check(frame is not None and frame["rssi"] == -48 and frame["beacon"], "beacon rssi")
    target = Target(mode="ssid", value="TestNet", phy_hint="wifi-ap")
    check(frame is not None and frame_matches(frame, target), "ssid match")
    check(frame is not None and frame_matches(frame, Target(mode="mac", value="AA:BB:CC:DD:EE:FF", mac="AA:BB:CC:DD:EE:FF")), "mac match")

    payload = {
        "type": "a{oa{sa{sv}}}",
        "data": [
            {
                "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_10": {
                    "org.bluez.Device1": {
                        "Address": {"type": "s", "data": "AA:BB:CC:DD:EE:10"},
                        "Name": {"type": "s", "data": "Kitchen"},
                        "RSSI": {"type": "n", "data": -58},
                        "AddressType": {"type": "s", "data": "public"},
                    }
                },
                "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_11": {
                    "org.bluez.Device1": {
                        "Address": {"type": "s", "data": "AA:BB:CC:DD:EE:11"},
                        "Alias": {"type": "s", "data": "AA:BB:CC:DD:EE:11"},
                        "RSSI": {"type": "n", "data": -40},
                    }
                },
                "/org/bluez/hci0": {"org.bluez.Adapter1": {"Name": {"type": "s", "data": "pop-os"}}},
            }
        ],
    }
    devices = parse_managed_objects(payload, "hci0")
    check(len(devices) == 2, "bluez device count")
    named = top_named(devices)
    check(len(named) == 1 and named[0].name == "Kitchen", "unnamed alias dropped")
    check(named[0].phy_hint == "ble" and named[0].rssi == -58, "ble hint and rssi")
    check(bt_match(named[0], Target(mode="name", value="kit")), "name substring")
    check(not bt_match(devices[1], Target(mode="name", value="Kitchen")), "other device skipped")

    state = SightState("Loft", "Jeff", "Wi-Fi", "wlx0", Target(mode="ssid", value="Home", display="Home", phy_hint="wifi-ap", freq_mhz=2437))
    stamp = 0.0
    for rssi in [-80, -80, -80, -80, -80, -60, -60, -60, -60, -60]:
        state.fold(
            Sample(rssi, 2437, "AA:BB:CC:DD:EE:01", "Home", 20, True, 6, "wifi-ap"),
            stamp,
        )
        stamp += 0.5
    view = state.snapshot(stamp)
    check(view.trend == "CLOSING", f"trend closing, got {view.trend}")
    check(view.distance_m is not None and 20 < view.distance_m < 50, "distance populated")

    far = View(
        "Loft", "Jeff", "Home", "AA:BB:CC:DD:EE:01", "Wi-Fi", "wlx0", "ch 6",
        -90, -90, estimate_distance_m(-90, 2437, 20), "HOLDING", 0.0, [],
        2437, 20, True, 0.2, "", 3, "far",
    )
    near = View(
        "Loft", "Jeff", "Home", "AA:BB:CC:DD:EE:01", "Wi-Fi", "wlx0", "locked ch 6",
        -40, -40, estimate_distance_m(-40, 2437, 20), "CLOSING", 6.0, [],
        2437, 20, True, 0.2, "", 9, "sights",
    )
    far_txt = render_sight(far, width=80, height=25, color=False)
    near_txt = render_sight(near, width=80, height=25, color=False)
    check(near_txt.count("█") > far_txt.count("█") > 0, "bar grows as the signal rises")
    check("CLOSING" in near_txt and "THE MARKSMAN" in near_txt, "sight text")
    check(len(render_sight(near, width=80, height=24, color=False).splitlines()) <= 24, "fits 24 rows")
    check("IN THE SIGHTS" in near_txt or "CLOSE" in near_txt or zone_word(near.zone) in near_txt, "zone word")

    class FakeSource:
        status = "locked ch 6"
        note = ""
        n = 0

        def poll(self):
            self.n += 1
            if self.n > 2:
                return None
            return Sample(-52, 2437, "AA:BB:CC:DD:EE:01", "Home", 20, True, 6, "wifi-ap")

    fake_state = SightState("Loft", "Jeff", "Wi-Fi", "wlx0", Target(mode="ssid", value="Home", display="Home", phy_hint="wifi-ap"))
    fake_stats = HuntStats("ssid:Home")
    reason = run_sight(FakeSource(), fake_state, fake_stats, None, 0.35)
    check(reason == "quit", "sight loop ends on duration")
    check(fake_stats.frames > 0, "sight loop records frames")
    check(package_manager_for({"ID": "pop", "ID_LIKE": "ubuntu debian"}) == "apt", "pop uses apt")
    check(package_manager_for({"ID": "ubuntu"}) == "apt", "ubuntu uses apt")
    check(package_manager_for({"ID": "fedora"}) == "dnf", "fedora uses dnf")
    check(package_manager_for({"ID": "arch"}) == "pacman", "arch uses pacman")
    check(package_manager_for({"ID": "gentoo"}) == "", "unknown distro has no installer")
    apt_pkgs = packages_for("apt")
    check("iw" in apt_pkgs and "bluez" in apt_pkgs and "iproute2" in apt_pkgs, "apt package list")
    check("bluez-utils" in packages_for("pacman"), "arch bluetoothctl package")
    check("NetworkManager" in packages_for("dnf"), "fedora NetworkManager package")
    check(build_parser().parse_args(["--setup"]).setup is True, "setup flag")

    if failures:
        for message in failures:
            print("FAIL", message)
        return 1
    print(f"self-test ok ({TOOL_NAME} {VERSION})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
