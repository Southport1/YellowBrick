"""
Matplotlib map widget — no WebEngine, no GPU dependency.
Tile fetching uses cx.bounds2img() in a background thread (returns plain numpy
arrays, safe to pass across threads).
"""
import math
import threading
import traceback

import contextily as cx
import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT as _NavBase
from matplotlib.figure import Figure
from matplotlib.patches import FancyArrow
from pyproj import Transformer
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QSizePolicy, QSlider,
                              QVBoxLayout, QWidget)

_TO_MERC   = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
_FROM_MERC = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

# Long Island Sound: Mamaroneck (W) → beyond Block Island (E)
_INIT_WEST, _INIT_EAST   = -74.1, -71.4
_INIT_SOUTH, _INIT_NORTH = 40.75, 41.65

NOAA_TILES     = "https://tileservice.charts.noaa.gov/tiles/50000_1/{z}/{x}/{y}.png"
SEAMARKS_TILES = "https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png"

# Arrow dimensions — 1/3 of original values
ARROW_LEN  = 1170
ARROW_W    =  233
ARROW_HEAD =  800
LABEL_OFF  = 1070

_MIN_EXTENT = 5000.0   # metres — maximum zoom-in clamp


class _SlimToolbar(_NavBase):
    """Navigation toolbar with zoom-rectangle and subplot-adjust tools removed."""
    toolitems = [t for t in _NavBase.toolitems
                 if t[0] not in ("Zoom", "Subplots", "Customize")]


def _merc(lon, lat):
    return _TO_MERC.transform(lon, lat)


def _merc_bounds(west, east, south, north):
    x1, y1 = _merc(west, south)
    x2, y2 = _merc(east, north)
    return x1, x2, y1, y2


def _tracks_to_merc(track):
    lons = [p[1] for p in track]
    lats = [p[0] for p in track]
    return _TO_MERC.transform(lons, lats)


class _Signals(QObject):
    tiles_ready = pyqtSignal(object)   # dict with img arrays + status


class MapWidget(QWidget):
    boat_clicked   = pyqtSignal(str)   # emits str team_id on left-click near a boat
    course_changed = pyqtSignal(list)  # emits list of [lat, lon] whenever course is edited

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data             = []
        self._tile_thread      = None
        self._last_tile_bounds = None   # (xlim, ylim) of last successful fetch
        self._last_tile_result = None   # cached numpy arrays — reused when view unchanged
        self._applying_tiles   = False  # guard: suppresses debounce during _apply_tiles
        self._sig              = _Signals()
        self._course_marks     = []
        self._course_mode      = False
        self._sig.tiles_ready.connect(self._apply_tiles)

        # Re-fetch tiles 600 ms after the user stops panning / zooming
        self._tile_debounce = QTimer(self)
        self._tile_debounce.setSingleShot(True)
        self._tile_debounce.setInterval(600)
        self._tile_debounce.timeout.connect(self._start_tile_fetch)

        self.fig    = Figure(facecolor="#0d1b2a")
        self.ax     = self.fig.add_axes([0, 0, 1, 1])
        self.canvas = FigureCanvasQTAgg(self.fig)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Expanding)

        self.toolbar = _SlimToolbar(self.canvas, self)

        # Zoom slider (replaces toolbar zoom-rect tool)
        self._zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self._zoom_slider.setRange(10, 300)   # value / 10 = zoom factor (1× – 30×)
        self._zoom_slider.setValue(10)
        self._zoom_slider.setFixedWidth(130)
        self._zoom_slider.setToolTip("Zoom in/out")
        self._zoom_label = QLabel("1×")
        self._zoom_label.setFixedWidth(36)
        self._zoom_label.setStyleSheet("color:#aabbcc; font-size:10px;")
        self._zoom_slider.valueChanged.connect(self._on_zoom_slider)

        top_row = QWidget()
        top_layout = QHBoxLayout(top_row)
        top_layout.setContentsMargins(0, 0, 4, 0)
        top_layout.setSpacing(4)
        top_layout.addWidget(self.toolbar)
        top_layout.addStretch()
        top_layout.addWidget(QLabel("Zoom:"))
        top_layout.addWidget(self._zoom_slider)
        top_layout.addWidget(self._zoom_label)

        self._status = QLabel("Waiting for data…")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet(
            "color:#aabbcc; background:#0d1b2a; padding:3px; font-size:11px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(top_row)
        layout.addWidget(self.canvas)
        layout.addWidget(self._status)

        self._init_axes()

        self.canvas.mpl_connect("scroll_event",       self._on_scroll)
        self.canvas.mpl_connect("button_press_event", self._on_map_click)
        # Kick off a tile refresh whenever the visible area changes
        self.ax.callbacks.connect("xlim_changed", self._on_view_changed)

    # ── Public API ────────────────────────────────────────────────────────

    def update_tracks(self, data):
        try:
            self._data = data
            self._draw_tracks()
            self._start_tile_fetch()
        except Exception:
            import sys
            sys.stderr.write("update_tracks error:\n" + traceback.format_exc() + "\n")
            sys.stderr.flush()

    # ── Course management ─────────────────────────────────────────────────

    def set_course_mode(self, active: bool):
        self._course_mode = active
        self.canvas.setCursor(
            Qt.CursorShape.CrossCursor if active else Qt.CursorShape.ArrowCursor)

    def delete_last_mark(self):
        if self._course_marks:
            self._course_marks.pop()
            self._draw_tracks()
            self.course_changed.emit(list(self._course_marks))

    def clear_course(self):
        if self._course_marks:
            self._course_marks.clear()
            self._draw_tracks()
            self.course_changed.emit([])

    def load_course(self, marks):
        """Load persisted marks (list of [lat, lon] or (lat, lon))."""
        self._course_marks = [tuple(m) for m in marks]
        self._draw_tracks()

    # ── Axes setup ────────────────────────────────────────────────────────

    def _init_axes(self):
        x1, x2, y1, y2 = _merc_bounds(_INIT_WEST, _INIT_EAST,
                                        _INIT_SOUTH, _INIT_NORTH)
        self.ax.set_xlim(x1, x2)
        self.ax.set_ylim(y1, y2)
        self._init_xrange = x2 - x1
        self._init_yrange = y2 - y1
        self.ax.set_facecolor("#1a3a5a")
        self.ax.set_axis_off()
        self.fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

    # ── Zoom ──────────────────────────────────────────────────────────────

    def _on_zoom_slider(self, value):
        factor = value / 10.0
        label = f"{factor:.0f}×" if factor == int(factor) else f"{factor:.1f}×"
        self._zoom_label.setText(label)
        cx_ = sum(self.ax.get_xlim()) / 2
        cy_ = sum(self.ax.get_ylim()) / 2
        hw = self._init_xrange / factor / 2
        hh = self._init_yrange / factor / 2
        self.ax.set_xlim(cx_ - hw, cx_ + hw)
        self.ax.set_ylim(cy_ - hh, cy_ + hh)
        self.canvas.draw_idle()

    def _on_scroll(self, event):
        """Zoom in/out around the current view centre (no drift)."""
        if event.xdata is None:
            return
        factor = 1.2 if event.button == "up" else (1 / 1.2)
        xlim = self.ax.get_xlim()
        ylim = self.ax.get_ylim()
        x_range = max(_MIN_EXTENT, (xlim[1] - xlim[0]) / factor)
        y_range = max(_MIN_EXTENT, (ylim[1] - ylim[0]) / factor)
        # Centre on the current view centre so the map doesn't drift
        cx_ = (xlim[0] + xlim[1]) / 2
        cy_ = (ylim[0] + ylim[1]) / 2
        self.ax.set_xlim(cx_ - x_range / 2, cx_ + x_range / 2)
        self.ax.set_ylim(cy_ - y_range / 2, cy_ + y_range / 2)
        self.canvas.draw_idle()
        # Sync slider
        new_factor = self._init_xrange / x_range
        new_val = max(10, min(300, round(new_factor * 10)))
        self._zoom_slider.blockSignals(True)
        self._zoom_slider.setValue(new_val)
        lbl = f"{new_factor:.0f}×" if new_factor == int(new_factor) else f"{new_factor:.1f}×"
        self._zoom_label.setText(lbl)
        self._zoom_slider.blockSignals(False)

    # ── Map click: course mode or boat selection ──────────────────────────

    def _on_map_click(self, event):
        if event.button != 1 or event.xdata is None:
            return
        if self.toolbar.mode:   # pan tool is active — don't intercept
            return

        if self._course_mode:
            lon, lat = _FROM_MERC.transform(event.xdata, event.ydata)
            self._course_marks.append((lat, lon))
            self._draw_tracks()
            self.course_changed.emit(list(self._course_marks))
            return

        # Normal boat-click detection
        if not self._data:
            return
        xlim = self.ax.get_xlim()
        threshold = (xlim[1] - xlim[0]) * 0.025
        min_dist = float("inf")
        nearest_id = None
        for boat in self._data:
            bx, by = _merc(boat["lon"], boat["lat"])
            dist = math.hypot(event.xdata - bx, event.ydata - by)
            if dist < min_dist:
                min_dist = dist
                nearest_id = boat["id"]
        if min_dist < threshold and nearest_id is not None:
            self.boat_clicked.emit(nearest_id)

    # ── Track & course drawing (main thread, fast) ────────────────────────

    def _draw_tracks(self):
        xlim = self.ax.get_xlim()
        ylim = self.ax.get_ylim()

        for a in list(self.ax.lines) + list(self.ax.patches) + list(self.ax.texts):
            a.remove()

        # ── Boat tracks ───────────────────────────────────────────────────
        for boat in self._data:
            track = boat["track"]
            if len(track) < 2:
                continue
            xs, ys = _tracks_to_merc(track)
            color  = boat["color"]
            cog    = boat["cog"]

            self.ax.plot(xs, ys, color=color, linewidth=1.5,
                         alpha=0.75, solid_capstyle="round", zorder=2)

            lx, ly  = xs[-1], ys[-1]
            cog_rad = math.radians(cog)
            dx = math.sin(cog_rad) * ARROW_LEN
            dy = math.cos(cog_rad) * ARROW_LEN

            self.ax.add_patch(FancyArrow(
                lx - dx * 0.35, ly - dy * 0.35, dx, dy,
                width=ARROW_W, head_width=ARROW_HEAD,
                head_length=ARROW_LEN * 0.55,
                color=color, length_includes_head=True, zorder=3,
            ))
            self.ax.text(
                lx + LABEL_OFF, ly, boat["name"],
                color="#0d1f3c", fontsize=9, fontweight="bold",
                va="center", ha="left", zorder=4,
            )

        # ── Course line & marks ───────────────────────────────────────────
        if self._course_marks:
            mark_merc = [_merc(lon, lat) for lat, lon in self._course_marks]
            if len(mark_merc) >= 2:
                cxs, cys = zip(*mark_merc)
                self.ax.plot(list(cxs), list(cys), color="#ff2222", linewidth=3,
                             solid_capstyle="round", solid_joinstyle="round",
                             alpha=0.9, zorder=5)
            for i, (mx, my) in enumerate(mark_merc, 1):
                self.ax.plot(mx, my, "o", color="#ff2222", markersize=14,
                             markeredgecolor="white", markeredgewidth=1.5, zorder=6)
                self.ax.text(mx, my, str(i), color="white", fontsize=7,
                             fontweight="bold", ha="center", va="center", zorder=7)

        self.ax.set_xlim(xlim)
        self.ax.set_ylim(ylim)
        self.ax.set_axis_off()
        self.canvas.draw_idle()

    # ── Tile fetching (background thread) ────────────────────────────────

    def _on_view_changed(self, _ax):
        """Called by matplotlib whenever xlim changes (pan, zoom, slider)."""
        if not self._applying_tiles:   # don't loop when _apply_tiles restores limits
            self._tile_debounce.start()

    def _start_tile_fetch(self):
        if self._tile_thread and self._tile_thread.is_alive():
            return
        xlim = tuple(self.ax.get_xlim())
        ylim = tuple(self.ax.get_ylim())
        # Same view as last fetch → reuse cached tiles instantly
        if self._last_tile_bounds == (xlim, ylim) and self._last_tile_result is not None:
            self._apply_tiles(self._last_tile_result)
            return
        self._status.setText("Downloading chart tiles…")
        self._last_tile_bounds = (xlim, ylim)
        self._tile_thread = threading.Thread(
            target=self._tile_worker, args=(xlim, ylim), daemon=True)
        self._tile_thread.start()

    def _tile_worker(self, xlim, ylim):
        w, e = xlim
        s, n = ylim
        result = {"base_img": None, "base_ext": None,
                  "sea_img":  None, "sea_ext":  None,
                  "status": "no tiles"}
        try:
            img, ext = cx.bounds2img(w, s, e, n, ll=False,
                                      source=NOAA_TILES, zoom="auto")
            result["base_img"] = np.asarray(img)
            result["base_ext"] = ext
            result["status"]   = "NOAA"
        except Exception:
            try:
                img, ext = cx.bounds2img(w, s, e, n, ll=False,
                                          source=cx.providers.OpenStreetMap.Mapnik,
                                          zoom="auto")
                result["base_img"] = np.asarray(img)
                result["base_ext"] = ext
                result["status"]   = "OSM"
            except Exception as exc:
                result["status"] = f"tile error: {exc}"

        if result["base_img"] is not None:
            try:
                img2, ext2 = cx.bounds2img(w, s, e, n, ll=False,
                                            source=SEAMARKS_TILES, zoom="auto")
                result["sea_img"] = np.asarray(img2)
                result["sea_ext"] = ext2
                result["status"] += " + seamarks"
            except Exception:
                pass

        self._sig.tiles_ready.emit(result)

    # ── Apply tiles (main thread, called via signal) ──────────────────────

    def _apply_tiles(self, result):
        self._last_tile_result = result   # cache for instant reuse on same view
        self._applying_tiles   = True     # suppress xlim_changed → debounce loop
        try:
            xlim = self.ax.get_xlim()
            ylim = self.ax.get_ylim()

            for im in list(self.ax.images):
                im.remove()

            if result.get("base_img") is not None:
                ext = result["base_ext"]
                self.ax.imshow(np.asarray(result["base_img"]),
                               extent=[ext[0], ext[1], ext[2], ext[3]],
                               origin="upper", zorder=0,
                               interpolation="bilinear", aspect="auto")

            if result.get("sea_img") is not None:
                ext2 = result["sea_ext"]
                self.ax.imshow(np.asarray(result["sea_img"]),
                               extent=[ext2[0], ext2[1], ext2[2], ext2[3]],
                               origin="upper", zorder=1, alpha=0.7,
                               interpolation="bilinear", aspect="auto")

            self._draw_tracks()
            self.ax.set_xlim(xlim)
            self.ax.set_ylim(ylim)
            self.ax.set_axis_off()
            self.canvas.draw_idle()
            self._status.setText(
                f"Chart: {result['status']}  ·  {len(self._data)} boats  ·  "
                "pan: toolbar  ·  zoom: scroll or slider"
            )
        except Exception:
            err = traceback.format_exc()
            self._status.setText("Chart render error — see stderr.log")
            import sys
            sys.stderr.write("_apply_tiles error:\n" + err + "\n")
            sys.stderr.flush()
        finally:
            self._applying_tiles = False
