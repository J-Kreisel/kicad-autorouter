from __future__ import annotations

import datetime
import multiprocessing
import os
import queue
import re
import sys
import traceback
from pathlib import Path
from typing import Any

from .kipy_adapter import KiPyAdapter, UnsupportedBoardError
from .model import Layer, Point, RoutingDirection
from .router import RoutePlan, Router, RouterConfig, RoutingProblem, board_fingerprint

_PLUGIN_LOG = Path.home() / ".local" / "share" / "kicad" / "kicad-autorouter-launch.log"


def _log_error(context: str, error: BaseException) -> None:
    """Record failures that would otherwise be invisible in the plugin process."""
    try:
        _PLUGIN_LOG.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now(datetime.UTC).isoformat()
        with _PLUGIN_LOG.open("a") as stream:
            stream.write(f"{timestamp} {context}: {type(error).__name__}: {error}\n")
            traceback.print_exception(type(error), error, error.__traceback__, file=stream)
    except OSError:
        pass


def _route_worker(problem: RoutingProblem, messages: Any) -> None:
    try:
        router = Router(
            problem.config,
            iteration_progress=lambda update: messages.put(
                (
                    "iteration",
                    update.iteration,
                    update.conflicts,
                    update.failures,
                    update.statuses,
                )
            ),
            cleanup_progress=lambda pass_index, improvements: messages.put(
                ("cleanup", pass_index, improvements)
            ),
            connection_progress=lambda iteration, index, total, net: messages.put(
                ("connection", iteration, index, total, net)
            ),
        )
        messages.put(("phase", "Routing connections"))
        plan = router.route_problem(problem)
        messages.put(("result", plan))
    except Exception:  # noqa: BLE001 - worker must report every failure to the GUI
        messages.put(("error", traceback.format_exc()))


def main() -> int:
    # PySide from nixpkgs currently crashes in Qt's Wayland platform plugin on some compositors.
    # XWayland is stable, while an explicit user choice remains respected.
    if sys.platform.startswith("linux"):
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    try:
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPolygonF
        from PySide6.QtWidgets import (
            QApplication,
            QCheckBox,
            QComboBox,
            QDoubleSpinBox,
            QFormLayout,
            QGridLayout,
            QGroupBox,
            QLabel,
            QLineEdit,
            QListWidget,
            QMainWindow,
            QMessageBox,
            QProgressBar,
            QPushButton,
            QScrollArea,
            QSpinBox,
            QSplitter,
            QTextEdit,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as error:
        print(
            f"error: PySide6 could not be loaded: {error}",
            file=sys.stderr,
        )
        return 2

    class BoardCanvas(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.problem: RoutingProblem | None = None
            self.connection_index: int | None = None
            self.mode: Layer | None = Layer.FRONT
            self.map_image: QImage | None = None
            self.statuses: tuple[str, ...] = ()
            self.zoom = 1.0
            self.pan_x = 0.0
            self.pan_y = 0.0
            self.drag_position: Any = None
            self.setMinimumSize(500, 400)

        def set_problem(self, problem: RoutingProblem | None) -> None:
            self.problem = problem
            self.connection_index = None
            self.map_image = None
            self.statuses = tuple("pending" for _ in problem.connections) if problem else ()
            self.zoom = 1.0
            self.pan_x = 0.0
            self.pan_y = 0.0
            self.update()

        def set_statuses(self, statuses: tuple[str, ...]) -> None:
            self.statuses = statuses
            self.update()

        def set_connection(self, index: int | None, mode: Layer | None) -> None:
            self.connection_index = index
            self.mode = mode
            self.map_image = self._sample_map() if index is not None else None
            self.update()

        def _sample_map(self) -> QImage | None:
            if self.problem is None or self.connection_index is None:
                return None
            connection = self.problem.connections[self.connection_index]
            maps = self.problem.clearance_maps(connection)
            min_x, min_y, max_x, max_y = self.problem.board.bounds
            width = 260
            height = max(80, round(width * (max_y - min_y) / max(max_x - min_x, 1e-9)))
            height = min(height, 220)
            image = QImage(width, height, QImage.Format.Format_RGB32)
            for pixel_y in range(height):
                y = min_y + (pixel_y + 0.5) * (max_y - min_y) / height
                for pixel_x in range(width):
                    x = min_x + (pixel_x + 0.5) * (max_x - min_x) / width
                    if self.mode is None:
                        clear = all(
                            maps.via.native.clear_point(x, y, int(layer))
                            for layer in self.problem.board.copper_layers
                        )
                    else:
                        layer = self.mode
                        direction = dict(self.problem.config.layer_directions).get(
                            layer, RoutingDirection.ANY
                        )
                        clear = (
                            direction != RoutingDirection.DISABLED
                            and maps.track.native.clear_point(x, y, int(layer))
                        )
                    image.setPixelColor(pixel_x, pixel_y, QColor("#f3f5f7" if clear else "#354052"))
            return image

        def _transform(self, point: Point) -> Any:
            assert self.problem is not None
            from PySide6.QtCore import QPointF

            min_x, min_y, max_x, max_y = self.problem.board.bounds
            margin = 20.0
            scale = (
                min(
                    (self.width() - 2 * margin) / max(max_x - min_x, 1e-9),
                    (self.height() - 2 * margin) / max(max_y - min_y, 1e-9),
                )
                * self.zoom
            )
            offset_x = (self.width() - (max_x - min_x) * scale) / 2
            offset_y = (self.height() - (max_y - min_y) * scale) / 2
            return QPointF(
                offset_x + self.pan_x + (point.x - min_x) * scale,
                offset_y + self.pan_y + (point.y - min_y) * scale,
            )

        def paintEvent(self, _event: Any) -> None:
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor("#1d232d"))
            if self.problem is None:
                painter.setPen(QColor("#c8d0dc"))
                painter.drawText(
                    self.rect(), Qt.AlignmentFlag.AlignCenter, "Generate a routing problem"
                )
                return

            outline = QPolygonF([self._transform(point) for point in self.problem.board.outline])
            if self.map_image is not None:
                bounds = outline.boundingRect()
                painter.drawImage(bounds, self.map_image)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#8ca0bb"), 2))
            painter.drawPolygon(outline)

            colors = {
                "pending": QColor("#61738c"),
                "routed": QColor("#56b870"),
                "conflict": QColor("#ef5b5b"),
                "failure": QColor("#f39c4a"),
            }
            for index, connection in enumerate(self.problem.connections):
                status = self.statuses[index] if index < len(self.statuses) else "pending"
                painter.setPen(QPen(colors[status], 2 if status != "pending" else 1))
                painter.drawLine(
                    self._transform(connection.source.center),
                    self._transform(connection.target.center),
                )

            if self.connection_index is not None:
                connection = self.problem.connections[self.connection_index]
                painter.setPen(QPen(QColor("#ffb454"), 3))
                painter.drawLine(
                    self._transform(connection.source.center),
                    self._transform(connection.target.center),
                )
                painter.setBrush(QColor("#ffb454"))
                for pad in (connection.source, connection.target):
                    center = self._transform(pad.center)
                    painter.drawEllipse(center, 5, 5)

        def wheelEvent(self, event: Any) -> None:
            factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
            self.zoom = min(20.0, max(0.25, self.zoom * factor))
            self.update()

        def mousePressEvent(self, event: Any) -> None:
            if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
                self.drag_position = event.position()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)

        def mouseMoveEvent(self, event: Any) -> None:
            if self.drag_position is None:
                return
            delta = event.position() - self.drag_position
            self.pan_x += delta.x()
            self.pan_y += delta.y()
            self.drag_position = event.position()
            self.update()

        def mouseReleaseEvent(self, _event: Any) -> None:
            self.drag_position = None
            self.unsetCursor()

    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("KiCad Autorouter")
            self.resize(1280, 800)
            self.adapter: KiPyAdapter | None = None
            self.problem: RoutingProblem | None = None
            self.plan: RoutePlan | None = None
            self.worker: multiprocessing.Process | None = None
            self.messages: Any = None

            self.exclude_nets = QLineEdit()
            self.exclude_nets.setPlaceholderText("Net names separated by spaces or commas")
            self.selection = QCheckBox("Use current PCB selection")
            self.schematic_priority = QCheckBox("Prefer direct schematic wires (0.8×)")
            self.grid = self._double(0.25, 0.01, 10.0, 2)
            self.max_vias = self._integer(8, 0, 255)
            self.max_visited = self._integer(2_000_000, 1, 100_000_000)
            self.negotiation_iterations = self._integer(30, 1, 10_000)
            self.present_penalty = self._double(4.0, 0.01, 10_000.0, 2)
            self.historical_penalty = self._double(1.0, 0.01, 10_000.0, 2)
            self.cleanup_passes = self._integer(3, 0, 100)
            self.layer_direction_widgets = {layer: self._direction_combo() for layer in Layer}
            self.front_direction = self.layer_direction_widgets[Layer.FRONT]
            self.back_direction = self.layer_direction_widgets[Layer.BACK]
            self.active_layers = (Layer.FRONT, Layer.BACK)
            self.direction_penalty = self._double(0.5, 0.0, 1000.0, 2)
            self.allow_partial = QCheckBox("Allow partial result")
            self.present_penalty.setToolTip(
                "Soft cost around provisional routes in the current negotiation iteration."
            )
            self.historical_penalty.setToolTip(
                "Persistent cost added where generated routes conflicted in earlier iterations."
            )

            options = QGroupBox("Routing settings")
            self.options_form = QFormLayout(options)
            self.options_form.addRow("Exclude nets", self.exclude_nets)
            self.options_form.addRow(self.selection)
            self.options_form.addRow(self.schematic_priority)
            self.options_form.addRow("Grid (mm)", self.grid)
            self.options_form.addRow("Maximum vias", self.max_vias)
            self.options_form.addRow("Maximum visited states", self.max_visited)
            self.options_form.addRow("Negotiation iterations", self.negotiation_iterations)
            self.options_form.addRow("Present penalty", self.present_penalty)
            self.options_form.addRow("Historical penalty", self.historical_penalty)
            self.options_form.addRow("Cleanup passes", self.cleanup_passes)
            layer_options = QGroupBox("Copper layer usage")
            self.layer_form = QFormLayout(layer_options)
            for layer, widget in self.layer_direction_widgets.items():
                self.layer_form.addRow(layer.label, widget)
                visible = layer in self.active_layers
                widget.setVisible(visible)
                label = self.layer_form.labelForField(widget)
                if label is not None:
                    label.setVisible(visible)
            self.options_form.addRow(layer_options)
            self.options_form.addRow("Direction step penalty", self.direction_penalty)
            self.options_form.addRow(self.allow_partial)

            self.prepare_button = QPushButton("Generate problem")
            self.route_button = QPushButton("Route")
            self.cancel_button = QPushButton("Cancel")
            self.apply_button = QPushButton("Apply to KiCad")
            self.unroute_button = QPushButton("Unroute scoped nets…")
            self.route_button.setEnabled(False)
            self.cancel_button.setEnabled(False)
            self.apply_button.setEnabled(False)
            self.prepare_button.clicked.connect(self.prepare_problem)
            self.route_button.clicked.connect(self.start_routing)
            self.cancel_button.clicked.connect(self.cancel_routing)
            self.apply_button.clicked.connect(self.apply_plan)
            self.unroute_button.clicked.connect(self.unroute_nets)

            buttons = QGridLayout()
            buttons.addWidget(self.prepare_button, 0, 0)
            buttons.addWidget(self.route_button, 0, 1)
            buttons.addWidget(self.cancel_button, 1, 0)
            buttons.addWidget(self.apply_button, 1, 1)
            buttons.addWidget(self.unroute_button, 2, 0, 1, 2)

            left_widget = QWidget()
            left = QVBoxLayout(left_widget)
            left.addWidget(options)
            left.addLayout(buttons)
            left.addStretch()
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(left_widget)
            scroll.setMinimumWidth(330)

            self.connections = QListWidget()
            self.connections.currentRowChanged.connect(self.connection_changed)
            self.map_mode = QComboBox()
            self._sync_map_modes(self.active_layers)
            self.map_mode.currentTextChanged.connect(self.connection_changed)
            self.details = QTextEdit()
            self.details.setReadOnly(True)
            self.details.setMinimumHeight(170)

            connection_panel = QWidget()
            connection_layout = QVBoxLayout(connection_panel)
            connection_layout.addWidget(QLabel("Connections"))
            connection_layout.addWidget(self.connections, 2)
            connection_layout.addWidget(QLabel("Clearance map"))
            connection_layout.addWidget(self.map_mode)
            connection_layout.addWidget(self.details, 1)
            connection_panel.setMinimumWidth(330)

            self.canvas = BoardCanvas()
            splitter = QSplitter()
            splitter.addWidget(scroll)
            splitter.addWidget(self.canvas)
            splitter.addWidget(connection_panel)
            splitter.setStretchFactor(1, 1)

            self.status = QLabel("Connect to KiCad by generating a routing problem.")
            self.progress = QProgressBar()
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            root = QWidget()
            layout = QVBoxLayout(root)
            layout.addWidget(splitter, 1)
            layout.addWidget(self.status)
            layout.addWidget(self.progress)
            self.setCentralWidget(root)

            self.poll_timer = QTimer(self)
            self.poll_timer.setInterval(100)
            self.poll_timer.timeout.connect(self.poll_worker)
            QTimer.singleShot(0, self.detect_layers)

        @staticmethod
        def _double(value: float, minimum: float, maximum: float, decimals: int) -> QDoubleSpinBox:
            widget = QDoubleSpinBox()
            widget.setRange(minimum, maximum)
            widget.setDecimals(decimals)
            widget.setValue(value)
            return widget

        @staticmethod
        def _integer(value: int, minimum: int, maximum: int) -> QSpinBox:
            widget = QSpinBox()
            widget.setRange(minimum, maximum)
            widget.setValue(value)
            return widget

        @staticmethod
        def _direction_combo() -> QComboBox:
            widget = QComboBox()
            for label, direction in (
                ("Any direction", RoutingDirection.ANY),
                ("Horizontal", RoutingDirection.HORIZONTAL),
                ("Vertical", RoutingDirection.VERTICAL),
                ("Diagonal /", RoutingDirection.DIAGONAL_UP),
                ("Diagonal \\", RoutingDirection.DIAGONAL_DOWN),
                ("Do not use", RoutingDirection.DISABLED),
            ):
                widget.addItem(label, int(direction))
            return widget

        def _excluded_nets(self) -> set[str]:
            return {name for name in re.split(r"[\s,]+", self.exclude_nets.text().strip()) if name}

        def _sync_layer_controls(self, layers: tuple[Layer, ...]) -> None:
            self.active_layers = layers
            for layer, widget in self.layer_direction_widgets.items():
                visible = layer in layers
                widget.setVisible(visible)
                label = self.layer_form.labelForField(widget)
                if label is not None:
                    label.setVisible(visible)
            self._sync_map_modes(layers)

        def detect_layers(self) -> None:
            try:
                layers = KiPyAdapter.connect().copper_layers()
            except (RuntimeError, UnsupportedBoardError):
                return
            self._sync_layer_controls(layers)
            names = ", ".join(layer.label for layer in layers)
            self.status.setText(f"Detected {len(layers)} copper layers: {names}")

        def _sync_map_modes(self, layers: tuple[Layer, ...]) -> None:
            current = self.map_mode.currentData() if hasattr(self, "map_mode") else None
            if not hasattr(self, "map_mode"):
                return
            self.map_mode.blockSignals(True)
            self.map_mode.clear()
            for layer in layers:
                self.map_mode.addItem(f"{layer.label} track", int(layer))
            self.map_mode.addItem("Through via", -1)
            index = self.map_mode.findData(current)
            self.map_mode.setCurrentIndex(max(0, index))
            self.map_mode.blockSignals(False)

        def _selected_map_layer(self) -> Layer | None:
            value = self.map_mode.currentData()
            return None if value == -1 else Layer(value)

        def _config(self) -> RouterConfig:
            return RouterConfig(
                grid=self.grid.value(),
                max_vias_per_connection=self.max_vias.value(),
                max_visited=self.max_visited.value(),
                max_negotiation_iterations=self.negotiation_iterations.value(),
                present_congestion_penalty=self.present_penalty.value(),
                historical_congestion_penalty=self.historical_penalty.value(),
                cleanup_passes=self.cleanup_passes.value(),
                allow_partial=self.allow_partial.isChecked(),
                front_direction=RoutingDirection(self.front_direction.currentData()),
                back_direction=RoutingDirection(self.back_direction.currentData()),
                layer_directions=tuple(
                    (layer, RoutingDirection(self.layer_direction_widgets[layer].currentData()))
                    for layer in self.active_layers
                ),
                direction_penalty=self.direction_penalty.value(),
            )

        def prepare_problem(self) -> None:
            try:
                self.status.setText("Extracting board and building connection graph…")
                QApplication.processEvents()
                adapter = KiPyAdapter.connect()
                board = adapter.extract()
                self._sync_layer_controls(board.copper_layers)
                selected = adapter.selected_pad_ids() if self.selection.isChecked() else None
                if self.selection.isChecked() and not selected:
                    raise ValueError("Select at least one pad, footprint, or group in PCB Editor")
                priority = (
                    adapter.direct_schematic_pairs()
                    if self.schematic_priority.isChecked()
                    else None
                )
                router = Router(self._config())
                excluded = self._excluded_nets()
                problem = router.prepare(board, excluded or None, selected, priority)
            except (RuntimeError, ValueError, UnsupportedBoardError) as error:
                QMessageBox.critical(self, "Could not generate problem", str(error))
                self.status.setText("Problem generation failed.")
                return

            self.adapter = adapter
            self.problem = problem
            self.plan = None
            self.connections.clear()
            for connection in problem.connections:
                source = f"{connection.source.component}.{connection.source.number}".strip(".")
                target = f"{connection.target.component}.{connection.target.number}".strip(".")
                marker = " ★" if connection.congestion_multiplier < 1 else ""
                self.connections.addItem(f"{connection.net}: {source} → {target}{marker}")
            self.canvas.set_problem(problem)
            self._set_statuses(tuple("pending" for _ in problem.connections))
            self.route_button.setEnabled(bool(problem.connections))
            self.apply_button.setEnabled(False)
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.status.setText(
                f"Problem ready: {len(problem.connections)} connections on "
                f"{len({connection.net for connection in problem.connections})} nets."
            )
            if problem.connections:
                self.connections.setCurrentRow(0)

        def _set_statuses(self, statuses: tuple[str, ...]) -> None:
            colors = {
                "pending": QColor("#8ca0bb"),
                "routed": QColor("#56b870"),
                "conflict": QColor("#ef5b5b"),
                "failure": QColor("#f39c4a"),
            }
            self.canvas.set_statuses(statuses)
            for index, status in enumerate(statuses):
                item = self.connections.item(index)
                if item is not None:
                    item.setForeground(QBrush(colors[status]))

        def connection_changed(self, *_args: Any) -> None:
            if self.problem is None:
                return
            index = self.connections.currentRow()
            if index < 0:
                self.canvas.set_connection(None, self._selected_map_layer())
                return
            connection = self.problem.connections[index]
            rules = self.problem.board.rules_for(connection.net)
            shared = next(
                (
                    group
                    for group in connection.source.group_path
                    if group in set(connection.target.group_path)
                ),
                "Board root",
            )
            reason = (
                "Direct unlabeled schematic connection"
                if connection.congestion_multiplier < 1
                else "Default"
            )
            extra_rules = []
            if self.problem.board.via_pad_hole_clearance:
                extra_rules.append(
                    "Via/pad physical hole clearance: "
                    f"{self.problem.board.via_pad_hole_clearance:.3f} mm"
                )
            if self.problem.board.keepouts:
                extra_rules.append(f"Rule-area keepouts: {len(self.problem.board.keepouts)}")
            extra_text = "\n".join(extra_rules) if extra_rules else "None"
            self.details.setPlainText(
                f"Net: {connection.net}\n"
                f"Source: {connection.source.component} pad {connection.source.number}\n"
                f"Target: {connection.target.component} pad {connection.target.number}\n"
                f"Track width: {rules.track_width:.3f} mm\n"
                f"Clearance: {rules.clearance:.3f} mm\n"
                f"Via: {rules.via_diameter:.3f} / {rules.via_drill:.3f} mm\n"
                + "".join(
                    f"{layer.label} preference: "
                    f"{dict(self.problem.config.layer_directions)[layer].name.lower()}\n"
                    for layer in self.problem.board.copper_layers
                )
                + f"Congestion multiplier: {connection.congestion_multiplier:.2f} ({reason})\n"
                f"Output group: {shared}\n"
                f"Additional geometric DRC rules:\n{extra_text}"
            )
            self.status.setText("Sampling the selected hard-clearance map…")
            QApplication.processEvents()
            self.canvas.set_connection(index, self._selected_map_layer())
            self.status.setText("Connection map ready. Dark regions are forbidden.")

        def start_routing(self) -> None:
            if self.problem is None or self.worker is not None:
                return
            context = multiprocessing.get_context("spawn")
            self.messages = context.Queue()
            self.worker = context.Process(target=_route_worker, args=(self.problem, self.messages))
            self.worker.start()
            self.prepare_button.setEnabled(False)
            self.route_button.setEnabled(False)
            self.cancel_button.setEnabled(True)
            self.apply_button.setEnabled(False)
            self.progress.setRange(0, 0)
            self.status.setText("Starting routing worker…")
            self.poll_timer.start()

        def poll_worker(self) -> None:
            messages = self.messages
            if messages is None:
                return
            while True:
                try:
                    message = messages.get_nowait()
                except queue.Empty:
                    break
                kind = message[0]
                if kind == "phase":
                    self.status.setText(message[1])
                elif kind == "connection":
                    _, iteration, index, total, net = message
                    self.status.setText(
                        f"Negotiation {iteration}: routing {net} ({index}/{total})…"
                    )
                elif kind == "iteration":
                    _, iteration, conflicts, failures, statuses = message
                    self._set_statuses(statuses)
                    self.status.setText(
                        f"Negotiation {iteration}: {conflicts} conflicts, {failures} failures"
                    )
                elif kind == "cleanup":
                    _, pass_index, improvements = message
                    self.status.setText(
                        f"Cleanup {pass_index}: {improvements} improved connections"
                    )
                elif kind == "result":
                    self.plan = message[1]
                    self.finish_worker()
                    self.apply_button.setEnabled(
                        self.plan.complete or self.problem.config.allow_partial
                    )
                    self.status.setText(
                        f"Route ready: {len(self.plan.tracks)} tracks, {len(self.plan.vias)} vias, "
                        f"{self.plan.length:.2f} mm, {len(self.plan.failures)} failures."
                    )
                    return
                elif kind == "error":
                    self.finish_worker()
                    QMessageBox.critical(self, "Routing failed", message[1])
                    self.status.setText("Routing worker failed.")
                    return

            if self.worker is not None and not self.worker.is_alive() and self.plan is None:
                self.finish_worker()

        def finish_worker(self) -> None:
            self.poll_timer.stop()
            if self.worker is not None:
                self.worker.join(timeout=1)
            self.worker = None
            self.messages = None
            self.prepare_button.setEnabled(True)
            self.route_button.setEnabled(
                self.problem is not None and bool(self.problem.connections)
            )
            self.cancel_button.setEnabled(False)
            self.progress.setRange(0, 1)
            self.progress.setValue(1 if self.plan is not None else 0)

        def cancel_routing(self) -> None:
            if self.worker is not None:
                self.worker.terminate()
                self.worker.join(timeout=2)
            self.plan = None
            self.finish_worker()
            self.status.setText("Routing cancelled; the KiCad board was not modified.")

        def apply_plan(self) -> None:
            if self.adapter is None or self.problem is None or self.plan is None:
                return
            try:
                current = self.adapter.extract()
                if board_fingerprint(current) != self.problem.board_fingerprint:
                    raise RuntimeError("The KiCad board changed; regenerate the routing problem")
                self.adapter.apply(self.plan.tracks, self.plan.vias)
            except Exception as error:  # noqa: BLE001 - surface every KiCad mutation failure
                _log_error("apply", error)
                QMessageBox.critical(self, "Could not apply route", str(error))
                return
            self.apply_button.setEnabled(False)
            self.route_button.setEnabled(False)
            self.status.setText("Routing applied. Run KiCad DRC before fabrication.")

        def unroute_nets(self) -> None:
            if self.worker is not None:
                QMessageBox.warning(self, "Routing active", "Cancel routing before unrouting nets")
                return
            try:
                adapter = KiPyAdapter.connect()
                board = adapter.extract()
                selected = adapter.selected_pad_ids() if self.selection.isChecked() else None
                if self.selection.isChecked() and not selected:
                    raise ValueError("Select at least one pad, footprint, or group in PCB Editor")
                excluded = self._excluded_nets()
                nets, tracks, vias = adapter.unroute_scope(board, excluded or None, selected)
            except (RuntimeError, ValueError, UnsupportedBoardError) as error:
                QMessageBox.critical(self, "Could not inspect nets", str(error))
                return
            if not tracks and not vias:
                QMessageBox.information(
                    self, "Nothing to unroute", "No scoped tracks or vias found"
                )
                return
            answer = QMessageBox.question(
                self,
                "Unroute scoped nets?",
                f"Remove {len(tracks)} tracks and {len(vias)} vias on {len(nets)} nets?\n\n"
                "This is applied as one undoable KiCad operation. Zones are preserved.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                _nets, track_count, via_count = adapter.unroute(board, excluded or None, selected)
            except Exception as error:  # noqa: BLE001 - surface every KiCad mutation failure
                _log_error("unroute", error)
                QMessageBox.critical(self, "Could not unroute nets", str(error))
                return
            self.adapter = adapter
            self.problem = None
            self.plan = None
            self.connections.clear()
            self.canvas.set_problem(None)
            self.route_button.setEnabled(False)
            self.apply_button.setEnabled(False)
            self.status.setText(f"Unrouted {track_count} tracks and {via_count} vias.")

        def closeEvent(self, event: Any) -> None:
            if self.worker is not None:
                self.cancel_routing()
            event.accept()

    multiprocessing.freeze_support()
    application = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
