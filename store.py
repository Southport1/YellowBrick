import csv
import json
import math
import os
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))


def _bearing(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _haversine_nm(lat1, lon1, lat2, lon2):
    R = 3440.065
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(max(0.0, min(1.0, a))))


class TrackStore:
    def __init__(self, path=None):
        if path is None:
            path = os.path.join(_HERE, "tracks.json")
        self.path = path
        self._raw = {}           # str(team_id) -> list of moments sorted by at asc
        self._names = {}         # str(team_id) -> boat name
        self._colors = {}        # str(team_id) -> '#rrggbb'
        self._divisions = {}     # str(team_id) -> division/class name
        self.last_ts = 0
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
            self._raw = data.get("raw", {})
            self._names = data.get("names", {})
            self._colors = data.get("colors", {})
            self._divisions = data.get("divisions", {})
            self.last_ts = data.get("last_ts", 0)
        except Exception:
            pass

    def update(self, teams, names, colors, divisions=None):
        """Replace track data with the latest full fetch. Returns True if data is new."""
        self._names = names
        self._colors = colors
        if divisions:
            self._divisions = divisions
        self._raw = {}
        max_ts = 0
        for t in teams:
            tid = str(t["id"])
            moments = sorted(t["moments"], key=lambda m: m["at"])
            self._raw[tid] = moments
            if moments:
                max_ts = max(max_ts, moments[-1]["at"])
        if max_ts > self.last_ts:
            self.last_ts = max_ts
            self.save()
            return True
        return False

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"raw": self._raw, "names": self._names,
                       "colors": self._colors, "divisions": self._divisions,
                       "last_ts": self.last_ts}, f)
        os.replace(tmp, self.path)

    def get_display_data(self, since_ts=None, visible_ids=None):
        """Return list of boat dicts for the map and sidebar table.

        since_ts: if set, only include track points at or after this Unix timestamp.
        visible_ids: if set, only include boats whose str id is in this set.
        """
        result = []
        for tid, moments in self._raw.items():
            if visible_ids is not None and tid not in visible_ids:
                continue
            filtered = moments if since_ts is None else [m for m in moments if m["at"] >= since_ts]
            if not filtered:
                continue
            name = self._names.get(tid, f"#{tid}")
            color = self._colors.get(tid, "#888888")
            last = filtered[-1]
            prev = filtered[-2] if len(filtered) >= 2 else None

            if prev and last["at"] != prev["at"]:
                cog = round(_bearing(prev["lat"], prev["lon"], last["lat"], last["lon"]), 1)
                dt_h = (last["at"] - prev["at"]) / 3600
                sog = round(_haversine_nm(prev["lat"], prev["lon"], last["lat"], last["lon"]) / dt_h, 1)
            else:
                cog, sog = 0, 0

            result.append({
                "id": tid,
                "name": name,
                "color": color,
                "track": [[m["lat"], m["lon"]] for m in filtered],
                "lat": last["lat"],
                "lon": last["lon"],
                "cog": cog,
                "sog": sog,
                "at": last["at"],
            })
        return sorted(result, key=lambda x: x["name"])

    def get_divisions(self):
        """Returns {division_name: [tid, ...]} sorted by division name."""
        result = {}
        for tid, div in self._divisions.items():
            result.setdefault(div, []).append(tid)
        return dict(sorted(result.items()))

    def get_time_range(self):
        """Returns (earliest_ts, latest_ts) across all track data, or (0, 0) if empty."""
        all_ts = [m["at"] for moments in self._raw.values() for m in moments]
        if not all_ts:
            return 0, 0
        return min(all_ts), max(all_ts)

    def get_track_distance_nm(self, tid):
        """Total track distance in nautical miles for a team."""
        moments = self._raw.get(str(tid), [])
        total = 0.0
        for i in range(1, len(moments)):
            total += _haversine_nm(moments[i-1]["lat"], moments[i-1]["lon"],
                                   moments[i]["lat"], moments[i]["lon"])
        return round(total, 1)

    def export_csv(self, path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["boat", "timestamp", "lat", "lon", "sog_kts", "cog_deg"])
            for tid, moments in sorted(self._raw.items(), key=lambda kv: self._names.get(kv[0], kv[0])):
                name = self._names.get(tid, f"#{tid}")
                prev = None
                for m in moments:
                    if prev and m["at"] != prev["at"]:
                        dt_h = (m["at"] - prev["at"]) / 3600
                        sog = round(_haversine_nm(prev["lat"], prev["lon"], m["lat"], m["lon"]) / dt_h, 1)
                        cog = round(_bearing(prev["lat"], prev["lon"], m["lat"], m["lon"]), 1)
                    else:
                        sog, cog = 0, 0
                    ts = datetime.fromtimestamp(m["at"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    w.writerow([name, ts, round(m["lat"], 5), round(m["lon"], 5), sog, cog])
                    prev = m
