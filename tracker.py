import json
import struct
import time
import urllib.request
from datetime import datetime, timezone

from PyQt6.QtCore import QThread, QTimer, pyqtSignal

RACE_KEY = "79birace2026"
YB_BASE = "https://pro.yb.tl/"
UPDATE_INTERVAL = 15 * 60   # expected YB update cadence in seconds
BUFFER = 60                  # seconds of lead time after expected update
RETRY_INTERVAL = 2 * 60     # seconds between retries when data hasn't changed
TIMEOUT = 30                 # HTTP timeout in seconds


def _fmt_time(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M UTC")


def _parse_positions(buf):
    """
    Port of the YB tX() JavaScript binary parser.
    Returns list of {id, moments} where moments are dicts with lat, lon, at.
    """
    flags = buf[0]
    has_alt = (flags & 1) == 1
    has_dtf = (flags & 2) == 2
    has_lap = (flags & 4) == 4
    has_pc  = (flags & 8) == 8
    is_race = has_pc and has_lap and has_alt
    if is_race:
        has_alt = has_lap = has_pc = False

    base_ts = struct.unpack_from(">I", buf, 1)[0]
    off = 5
    teams = []

    while off < len(buf):
        team_id = struct.unpack_from(">H", buf, off)[0];  off += 2
        if is_race:
            n = struct.unpack_from(">Q", buf, off)[0];   off += 8
        else:
            n = struct.unpack_from(">H", buf, off)[0];   off += 2

        moments = []
        prev = {}
        for _ in range(n):
            k = buf[off]
            m = {}
            if (k & 128) == 128:
                Y = struct.unpack_from(">H", buf, off)[0]; off += 2
                Z = struct.unpack_from(">h", buf, off)[0]; off += 2
                W = struct.unpack_from(">h", buf, off)[0]; off += 2
                if has_alt:
                    m["alt"] = struct.unpack_from(">h", buf, off)[0]; off += 2
                if has_dtf:
                    m["dtf"] = prev.get("dtf", 0) + struct.unpack_from(">h", buf, off)[0]; off += 2
                    if has_lap: m["lap"] = buf[off]; off += 1
                if has_pc:
                    m["pc"] = struct.unpack_from(">h", buf, off)[0] / 32000; off += 2
                Y = Y & 0x7FFF
                m["lat"] = prev.get("lat", 0) + Z
                m["lon"] = prev.get("lon", 0) + W
                m["at"]  = prev.get("at", 0)  - Y
            else:
                Y = struct.unpack_from(">I", buf, off)[0]; off += 4
                Z = struct.unpack_from(">i", buf, off)[0]; off += 4
                W = struct.unpack_from(">i", buf, off)[0]; off += 4
                if has_alt:
                    m["alt"] = struct.unpack_from(">h", buf, off)[0]; off += 2
                if has_dtf:
                    m["dtf"] = struct.unpack_from(">i", buf, off)[0]; off += 4
                    if has_lap: m["lap"] = buf[off]; off += 1
                if has_pc:
                    m["pc"] = struct.unpack_from(">i", buf, off)[0] / 21000000; off += 4
                m["lat"] = Z
                m["lon"] = W
                m["at"]  = base_ts + Y
            moments.append(m)
            prev = m

        for m in moments:
            m["lat"] /= 1e5
            m["lon"] /= 1e5

        teams.append({"id": team_id, "moments": moments})

    return teams


class YBPoller(QThread):
    data_ready     = pyqtSignal(dict)   # {'teams': [...], 'names': {...}, 'colors': {...}}
    status_changed = pyqtSignal(str)

    def __init__(self, last_ts=0):
        super().__init__()
        self._last_ts = last_ts
        self._timer = None

    def run(self):
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fetch)
        self._schedule(self._initial_delay())
        self.exec()

    def _initial_delay(self):
        if self._last_ts > 0:
            seconds_until = (self._last_ts + UPDATE_INTERVAL + BUFFER) - time.time()
            return max(0, int(seconds_until * 1000))
        return 0

    def _schedule(self, delay_ms):
        if delay_ms > 5000:
            minutes = delay_ms // 1000 // 60
            self.status_changed.emit(f"Next YB update in ~{minutes} min")
        self._timer.start(delay_ms)

    def _fetch(self):
        self.status_changed.emit("Fetching position data…")
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": f"https://pro.yb.tl/{RACE_KEY}/",
        }
        try:
            setup = json.loads(urllib.request.urlopen(
                urllib.request.Request(f"{YB_BASE}JSON/{RACE_KEY}/RaceSetup", headers=headers),
                timeout=TIMEOUT).read())
            names     = {str(t["id"]): t["name"]                      for t in setup.get("teams", [])}
            colors    = {str(t["id"]): "#" + t.get("colour", "888888") for t in setup.get("teams", [])}
            divisions = {str(t["id"]): str(t.get("class") or t.get("classname") or
                                           t.get("division") or t.get("fleet") or "Unknown")
                         for t in setup.get("teams", [])}

            buf = urllib.request.urlopen(
                urllib.request.Request(f"{YB_BASE}BIN/{RACE_KEY}/AllPositions3", headers=headers),
                timeout=TIMEOUT).read()
            teams = _parse_positions(buf)

            new_ts = max((m["at"] for t in teams for m in t["moments"]), default=0)

            if new_ts > self._last_ts:
                self._last_ts = new_ts
                self.data_ready.emit({"teams": teams, "names": names,
                                      "colors": colors, "divisions": divisions})
                self.status_changed.emit(f"Updated {_fmt_time(new_ts)} · {len(teams)} boats")
                seconds_until = (new_ts + UPDATE_INTERVAL + BUFFER) - time.time()
                self._schedule(max(5000, int(seconds_until * 1000)))
            else:
                self.status_changed.emit(f"No new data — rechecking in {RETRY_INTERVAL // 60} min")
                self._schedule(RETRY_INTERVAL * 1000)

        except Exception as exc:
            self.status_changed.emit(f"Fetch error: {exc}")
            self._schedule(RETRY_INTERVAL * 1000)
