import json
import os
from datetime import datetime, timezone

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QDockWidget, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QSlider, QTableWidget, QTableWidgetItem, QToolBar,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from map_widget import MapWidget
from store import TrackStore
from tracker import YBPoller

_HERE        = os.path.dirname(os.path.abspath(__file__))
_COURSE_PATH = os.path.join(_HERE, "course.json")


# ── Collapsing dock widget ────────────────────────────────────────────────────

class _CollapsingDock(QDockWidget):
    """QDockWidget that minimises to its title bar instead of closing.

    The ▼ button collapses the content; ▶ restores it.  The standard close
    button is hidden — the panel can never be accidentally dismissed.
    """
    panel_collapsed = pyqtSignal(bool)   # True = just collapsed, False = just expanded

    def __init__(self, title, parent=None):
        super().__init__(title, parent)
        # No close button — only move and float are kept
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable |
            QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )

        bar = QWidget()
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(6, 2, 2, 2)
        bar_layout.setSpacing(2)

        lbl = QLabel(title)
        lbl.setStyleSheet("font-weight: bold; font-size: 11px;")

        self._btn = QPushButton("▼")
        self._btn.setFixedSize(22, 18)
        self._btn.setFlat(True)
        self._btn.setToolTip("Minimise / Restore panel")
        self._btn.clicked.connect(self._toggle)

        bar_layout.addWidget(lbl)
        bar_layout.addStretch()
        bar_layout.addWidget(self._btn)
        self.setTitleBarWidget(bar)

    def _toggle(self):
        w = self.widget()
        if w is None:
            return
        if w.isVisible():
            w.hide()
            self._btn.setText("▶")
            self.panel_collapsed.emit(True)
        else:
            w.show()
            self._btn.setText("▼")
            self.panel_collapsed.emit(False)


# ── Boat info popup ───────────────────────────────────────────────────────────

class BoatInfoDialog(QDialog):
    def __init__(self, boat, store, parent=None):
        super().__init__(parent)
        self.setWindowTitle(boat["name"])
        self.setMinimumWidth(340)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        tid = boat["id"]
        div      = store._divisions.get(tid, "—")
        last_fix = datetime.fromtimestamp(boat["at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        moments  = store._raw.get(tid, [])
        first_fix = (datetime.fromtimestamp(moments[0]["at"], tz=timezone.utc)
                     .strftime("%Y-%m-%d %H:%M UTC")) if moments else "—"
        lat, lon = boat["lat"], boat["lon"]
        track_nm = store.get_track_distance_nm(tid)

        form.addRow("Division:",        QLabel(div))
        form.addRow("First fix:",       QLabel(first_fix))
        form.addRow("Last fix:",        QLabel(last_fix))
        form.addRow("Latitude:",        QLabel(f"{abs(lat):.4f}° {'N' if lat >= 0 else 'S'}"))
        form.addRow("Longitude:",       QLabel(f"{abs(lon):.4f}° {'W' if lon < 0 else 'E'}"))
        form.addRow("SOG:",             QLabel(f"{boat['sog']} kts"))
        form.addRow("COG:",             QLabel(f"{boat['cog']}°"))
        form.addRow("Track points:",    QLabel(str(len(moments))))
        form.addRow("Distance sailed:", QLabel(f"{track_nm} nm"))

        layout.addLayout(form)
        btn = QPushButton("Close")
        btn.clicked.connect(self.accept)
        layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)


# ── Division / boat visibility dialog ────────────────────────────────────────

class DivisionDialog(QDialog):
    def __init__(self, store, visible_ids, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Filter by Division / Boat")
        self.setMinimumWidth(420)
        self.setMinimumHeight(520)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self._store   = store
        self._all_ids = set(store._raw.keys())
        self._vis     = set(self._all_ids) if visible_ids is None else set(visible_ids)

        layout = QVBoxLayout(self)
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        layout.addWidget(self._tree)

        btn_row = QHBoxLayout()
        b_all  = QPushButton("Show All")
        b_none = QPushButton("Hide All")
        b_ok   = QPushButton("Apply && Close")
        b_all.clicked.connect(self._show_all)
        b_none.clicked.connect(self._hide_all)
        b_ok.clicked.connect(self.accept)
        btn_row.addWidget(b_all)
        btn_row.addWidget(b_none)
        btn_row.addStretch()
        btn_row.addWidget(b_ok)
        layout.addLayout(btn_row)

        self._populate()
        self._tree.itemChanged.connect(self._on_item_changed)

    def _populate(self):
        self._tree.blockSignals(True)
        self._tree.clear()
        divisions = self._store.get_divisions()
        names = self._store._names
        if not divisions:
            self._tree.addTopLevelItem(QTreeWidgetItem(["No division data — fetch data first"]))
            self._tree.blockSignals(False)
            return
        for div_name, tids in divisions.items():
            div_item = QTreeWidgetItem([div_name])
            div_item.setFlags(div_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            self._tree.addTopLevelItem(div_item)
            for tid in sorted(tids, key=lambda t: names.get(t, t)):
                boat_item = QTreeWidgetItem([names.get(tid, f"#{tid}")])
                boat_item.setData(0, Qt.ItemDataRole.UserRole, tid)
                boat_item.setFlags(boat_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                boat_item.setCheckState(
                    0, Qt.CheckState.Checked if tid in self._vis else Qt.CheckState.Unchecked)
                div_item.addChild(boat_item)
            div_item.setExpanded(True)
            self._sync_div_state(div_item)
        self._tree.blockSignals(False)

    def _sync_div_state(self, div_item):
        n = div_item.childCount()
        if n == 0:
            return
        checked = sum(1 for i in range(n)
                      if div_item.child(i).checkState(0) == Qt.CheckState.Checked)
        if checked == n:
            state = Qt.CheckState.Checked
        elif checked == 0:
            state = Qt.CheckState.Unchecked
        else:
            state = Qt.CheckState.PartiallyChecked
        div_item.setCheckState(0, state)

    def _on_item_changed(self, item, _col):
        self._tree.itemChanged.disconnect(self._on_item_changed)
        try:
            state = item.checkState(0)
            if item.childCount():
                child_state = (Qt.CheckState.Checked
                               if state == Qt.CheckState.Checked
                               else Qt.CheckState.Unchecked)
                for i in range(item.childCount()):
                    child = item.child(i)
                    child.setCheckState(0, child_state)
                    tid = child.data(0, Qt.ItemDataRole.UserRole)
                    if tid:
                        (self._vis.add if child_state == Qt.CheckState.Checked
                         else self._vis.discard)(tid)
            else:
                tid = item.data(0, Qt.ItemDataRole.UserRole)
                if tid:
                    (self._vis.add if state == Qt.CheckState.Checked
                     else self._vis.discard)(tid)
                parent = item.parent()
                if parent:
                    self._sync_div_state(parent)
        finally:
            self._tree.itemChanged.connect(self._on_item_changed)

    def _show_all(self):
        self._tree.blockSignals(True)
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            div = root.child(i)
            div.setCheckState(0, Qt.CheckState.Checked)
            for j in range(div.childCount()):
                div.child(j).setCheckState(0, Qt.CheckState.Checked)
        self._vis = set(self._all_ids)
        self._tree.blockSignals(False)

    def _hide_all(self):
        self._tree.blockSignals(True)
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            div = root.child(i)
            div.setCheckState(0, Qt.CheckState.Unchecked)
            for j in range(div.childCount()):
                div.child(j).setCheckState(0, Qt.CheckState.Unchecked)
        self._vis.clear()
        self._tree.blockSignals(False)

    def get_visible_ids(self):
        return None if self._vis == self._all_ids else self._vis


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YB Race Tracker — Block Island 2026")
        self.resize(1400, 900)

        self.store           = TrackStore()
        self._visible_ids    = None   # None = all visible; set = filtered subset
        self._time_filter_ts = None   # None = no time filter

        # ── Map widget ────────────────────────────────────────────────────
        self._map = MapWidget()
        self.setCentralWidget(self._map)
        self._map.boat_clicked.connect(self._show_boat_info)
        self._map.course_changed.connect(self._on_course_changed)

        # ── Fleet table dock ──────────────────────────────────────────────
        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(["", "Boat", "SOG (kts)", "COG (°)", "Last Fix"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setMinimumWidth(340)
        self._table.cellDoubleClicked.connect(self._on_table_double_click)
        self._table.itemChanged.connect(self._on_table_item_changed)

        self._fleet_dock = _CollapsingDock("Fleet", self)
        self._fleet_dock.setWidget(self._table)
        self._fleet_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self._fleet_dock)

        # ── Course panel dock ─────────────────────────────────────────────
        course_widget = QWidget()
        course_layout = QVBoxLayout(course_widget)
        course_layout.setContentsMargins(6, 6, 6, 6)
        course_layout.setSpacing(6)

        self._btn_add_mark = QPushButton("▶  Start Adding Marks")
        self._btn_add_mark.setCheckable(True)
        self._btn_add_mark.setToolTip("Toggle course-entry mode; click on chart to place marks")
        self._btn_add_mark.toggled.connect(self._toggle_course_mode)
        course_layout.addWidget(self._btn_add_mark)

        btn_del = QPushButton("✕  Delete Last Mark")
        btn_del.clicked.connect(self._map.delete_last_mark)
        course_layout.addWidget(btn_del)

        btn_clear = QPushButton("Clear Course")
        btn_clear.clicked.connect(self._clear_course)
        course_layout.addWidget(btn_clear)

        self._course_count_lbl = QLabel("No marks")
        self._course_count_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._course_count_lbl.setStyleSheet("color: #888; font-size: 10px;")
        course_layout.addWidget(self._course_count_lbl)
        course_layout.addStretch()

        self._course_dock = _CollapsingDock("Course", self)
        self._course_dock.setWidget(course_widget)
        self._course_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self._course_dock.visibilityChanged.connect(self._on_course_dock_hidden)
        self._course_dock.panel_collapsed.connect(self._on_course_panel_collapsed)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self._course_dock)

        # Keep course dock compact; fleet dock gets the majority of the space
        self.resizeDocks([self._course_dock], [140], Qt.Orientation.Vertical)

        # ── Toolbar ───────────────────────────────────────────────────────
        tb = QToolBar("Actions")
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addAction("Export CSV",  self._export_csv)
        tb.addAction("Divisions…",  self._open_division_dialog)
        tb.addSeparator()
        tb.addAction("Refresh Now", self._trigger_refresh)
        tb.addSeparator()

        tb.addWidget(QLabel(" Show last: "))
        self._time_slider = QSlider(Qt.Orientation.Horizontal)
        self._time_slider.setRange(1, 96)
        self._time_slider.setValue(96)
        self._time_slider.setFixedWidth(160)
        self._time_slider.setToolTip("Limit track history shown on chart")
        self._time_slider.valueChanged.connect(self._on_time_slider)
        tb.addWidget(self._time_slider)
        self._time_label = QLabel("All time ")
        self._time_label.setFixedWidth(80)
        tb.addWidget(self._time_label)

        # ── View menu (restores dismissed docks) ──────────────────────────
        view_menu = self.menuBar().addMenu("View")
        view_menu.addAction(self._fleet_dock.toggleViewAction())
        view_menu.addAction(self._course_dock.toggleViewAction())

        # ── Status bar ────────────────────────────────────────────────────
        self._status = QLabel("Starting…")
        self.statusBar().addPermanentWidget(self._status)

        # ── Poller ────────────────────────────────────────────────────────
        self._poller = YBPoller(last_ts=self.store.last_ts)
        self._poller.data_ready.connect(self._on_data)
        self._poller.status_changed.connect(self._on_status)
        self._poller.start()

        # ── Restore saved course ──────────────────────────────────────────
        saved = self._load_course()
        if saved:
            self._map.load_course(saved)
            self._update_course_label(len(saved))

        if self.store.last_ts > 0:
            QTimer.singleShot(200, self._initial_draw)

    # ── Slots ─────────────────────────────────────────────────────────────

    def _initial_draw(self):
        try:
            self._update_time_slider()
            self._refresh_display()
        except Exception:
            import traceback, sys
            sys.stderr.write("_initial_draw error:\n" + traceback.format_exc() + "\n")
            sys.stderr.flush()

    def _on_data(self, payload):
        self.store.update(payload["teams"], payload["names"], payload["colors"],
                          payload.get("divisions", {}))
        self._update_time_slider()
        self._refresh_display()

    def _on_status(self, text):
        self._status.setText(text)

    def _on_time_slider(self, value):
        _, latest = self.store.get_time_range()
        if value >= self._time_slider.maximum() or latest == 0:
            self._time_filter_ts = None
            self._time_label.setText("All time ")
        else:
            self._time_filter_ts = latest - value * 3600
            self._time_label.setText(f"Last {value}h ")
        self._refresh_map()

    def _on_table_item_changed(self, item):
        if item.column() != 0:
            return
        tid = item.data(Qt.ItemDataRole.UserRole)
        if tid is None:
            return
        checked  = item.checkState() == Qt.CheckState.Checked
        all_ids  = set(self.store._raw.keys())
        if checked:
            if self._visible_ids is not None:
                self._visible_ids.add(tid)
                if self._visible_ids == all_ids:
                    self._visible_ids = None
        else:
            if self._visible_ids is None:
                self._visible_ids = all_ids - {tid}
            else:
                self._visible_ids.discard(tid)
        self._refresh_map()

    def _on_table_double_click(self, row, _col):
        chk = self._table.item(row, 0)
        if chk:
            tid = chk.data(Qt.ItemDataRole.UserRole)
            if tid:
                self._show_boat_info(tid)

    # ── Course ────────────────────────────────────────────────────────────

    def _toggle_course_mode(self, active):
        self._map.set_course_mode(active)
        if active:
            self._btn_add_mark.setText("■  Stop Adding Marks")
            self._btn_add_mark.setStyleSheet(
                "QPushButton { background-color: #cc2222; color: white; font-weight: bold; }")
        else:
            self._btn_add_mark.setText("▶  Start Adding Marks")
            self._btn_add_mark.setStyleSheet("")

    def _clear_course(self):
        self._map.clear_course()   # emits course_changed([])

    def _on_course_changed(self, marks):
        self._update_course_label(len(marks))
        self._save_course(marks)

    def _on_course_dock_hidden(self, visible):
        # Fired when the whole dock is shown/hidden (e.g. via View menu)
        if not visible and self._btn_add_mark.isChecked():
            self._btn_add_mark.setChecked(False)

    def _on_course_panel_collapsed(self, collapsed):
        # Fired when the ▼/▶ button minimises the course panel
        if collapsed and self._btn_add_mark.isChecked():
            self._btn_add_mark.setChecked(False)   # cancel course-entry mode

    def _update_course_label(self, n):
        if n == 0:
            self._course_count_lbl.setText("No marks")
        else:
            self._course_count_lbl.setText(f"{n} mark{'s' if n != 1 else ''} placed")

    def _save_course(self, marks):
        try:
            with open(_COURSE_PATH, "w") as f:
                json.dump(marks, f)
        except Exception:
            pass

    def _load_course(self):
        try:
            if os.path.exists(_COURSE_PATH):
                with open(_COURSE_PATH) as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    # ── Helpers ───────────────────────────────────────────────────────────

    def _update_time_slider(self):
        earliest, latest = self.store.get_time_range()
        if latest > earliest:
            max_h = max(2, int((latest - earliest) / 3600) + 2)
            self._time_slider.blockSignals(True)
            was_at_max = self._time_slider.value() >= self._time_slider.maximum()
            self._time_slider.setRange(1, max_h)
            if was_at_max:
                self._time_slider.setValue(max_h)
                self._time_filter_ts = None
                self._time_label.setText("All time ")
            self._time_slider.blockSignals(False)

    def _refresh_display(self):
        all_data = self.store.get_display_data()
        self._refresh_table(all_data)
        map_data = self.store.get_display_data(
            since_ts=self._time_filter_ts, visible_ids=self._visible_ids)
        self._map.update_tracks(map_data)

    def _refresh_map(self):
        map_data = self.store.get_display_data(
            since_ts=self._time_filter_ts, visible_ids=self._visible_ids)
        self._map.update_tracks(map_data)

    def _refresh_table(self, data):
        self._table.blockSignals(True)
        self._table.setRowCount(len(data))
        for row, boat in enumerate(data):
            ts = datetime.fromtimestamp(boat["at"], tz=timezone.utc).strftime("%H:%M UTC")
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            visible = self._visible_ids is None or boat["id"] in self._visible_ids
            chk.setCheckState(Qt.CheckState.Checked if visible else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, boat["id"])
            self._table.setItem(row, 0, chk)
            for col, val in enumerate([boat["name"], str(boat["sog"]),
                                        str(boat["cog"]), ts], start=1):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table.setItem(row, col, item)
        self._table.resizeColumnsToContents()
        self._table.blockSignals(False)

    def _export_csv(self):
        default = f"tracks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", default, "CSV files (*.csv)")
        if not path:
            return
        self.store.export_csv(path)
        self.statusBar().showMessage(f"Exported → {path}", 5000)

    def _trigger_refresh(self):
        self._poller._timer.start(100)

    def _open_division_dialog(self):
        dlg = DivisionDialog(self.store, self._visible_ids, parent=self)
        if dlg.exec():
            self._visible_ids = dlg.get_visible_ids()
            self._refresh_table(self.store.get_display_data())
            self._refresh_map()

    def _show_boat_info(self, tid):
        boat = next((b for b in self.store.get_display_data() if b["id"] == tid), None)
        if boat:
            BoatInfoDialog(boat, self.store, parent=self).exec()
