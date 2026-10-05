from __future__ import annotations

import hashlib
import heapq
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from math import ceil, floor, inf, sqrt
from typing import Any, TypeAlias

from ._native import NativeClearanceMap, NativeCostMap, native_conflicts, native_negotiate
from ._native import find_path as native_find_path
from .connectivity import Connection, unrouted_connections
from .geometry import (
    distance_point_to_rectangle,
    distance_point_to_segment,
    distance_to_polygon_edge,
    point_in_polygon,
    segment_distance,
)
from .model import (
    Board,
    CopperArea,
    KeepoutArea,
    Layer,
    Pad,
    Point,
    RoutingDirection,
    Track,
    Via,
)


@dataclass(frozen=True, slots=True)
class RouterConfig:
    grid: float = 0.25
    via_cost: float = 8.0
    bend_cost: float = 0.35
    max_visited: int = 2_000_000
    max_vias_per_connection: int = 8
    max_negotiation_iterations: int = 30
    present_congestion_penalty: float = 4.0
    historical_congestion_penalty: float = 1.0
    cleanup_passes: int = 3
    allow_partial: bool = False
    front_direction: RoutingDirection = RoutingDirection.ANY
    back_direction: RoutingDirection = RoutingDirection.ANY
    layer_directions: tuple[tuple[Layer, RoutingDirection], ...] = ()
    direction_penalty: float = 0.5


@dataclass(frozen=True, slots=True)
class RouteFailure:
    net: str
    source: str
    target: str
    reason: str


@dataclass(slots=True)
class RoutePlan:
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    failures: list[RouteFailure] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.failures

    @property
    def length(self) -> float:
        return sum(track.start.distance_to(track.end) for track in self.tracks)


@dataclass(frozen=True, slots=True)
class RoutingIteration:
    iteration: int
    conflicts: int
    failures: int
    statuses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoutingProblem:
    """Immutable preflight description of the connections the router will attempt."""

    board: Board
    connections: tuple[Connection, ...]
    config: RouterConfig
    board_fingerprint: str

    def clearance_maps(self, connection: Connection) -> ConnectionClearanceMaps:
        rules = self.board.rules_for(connection.net)
        return ConnectionClearanceMaps(
            track=_ClearanceMap(self.board, connection.net, rules.track_width / 2),
            via=_ClearanceMap(
                self.board,
                connection.net,
                rules.via_diameter / 2,
                via_drill=rules.via_drill,
            ),
        )


@dataclass(frozen=True, slots=True)
class ConnectionClearanceMaps:
    """Hard track and via placement maps for one preflight connection."""

    track: _ClearanceMap
    via: _ClearanceMap


def board_fingerprint(board: Board) -> str:
    return hashlib.sha256(repr(board).encode()).hexdigest()


def _track_payload(route: _CandidateRoute) -> list[tuple[float, float, float, float, float, int]]:
    return [
        (track.start.x, track.start.y, track.end.x, track.end.y, track.width, int(track.layer))
        for track in route.tracks
    ]


def _via_payload(route: _CandidateRoute) -> list[tuple[float, float, float, float]]:
    return [(via.position.x, via.position.y, via.diameter, via.drill) for via in route.vias]


def _conflict_site(
    site: tuple[float, float, int | None, float],
) -> tuple[Point, Layer | None, float]:
    x, y, layer, radius = site
    return Point(x, y), None if layer is None else Layer(layer), radius


State = tuple[int, int, Layer, int]  # x, y, layer, via count
ConnectionKey: TypeAlias = tuple[str, str, str]


@dataclass(slots=True)
class _CandidateRoute:
    connection: Connection
    tracks: list[Track]
    vias: list[Via]

    @property
    def key(self) -> ConnectionKey:
        return (self.connection.net, self.connection.source.id, self.connection.target.id)


@dataclass(frozen=True, slots=True)
class _Conflict:
    left: int
    right: int
    sites: tuple[tuple[Point, Layer | None, float], ...]


class _PenaltyMap:
    """Connection-local historical congestion costs on the routing grid."""

    def __init__(self, grid: float):
        self.grid = grid
        self.native = NativeCostMap(grid)

    def cost(self, point: Point, layer: Layer) -> float:
        return self.native.cost(point.x, point.y, int(layer))

    def copy(self) -> _PenaltyMap:
        result = _PenaltyMap(self.grid)
        result.native = self.native.copy_map()
        return result

    def add_point(
        self,
        point: Point,
        layer: Layer | None,
        radius: float,
        amount: float,
    ) -> None:
        self.native.add_point(
            point.x,
            point.y,
            int(layer) if layer is not None else None,
            radius,
            amount,
        )

    def add_track(self, track: Track, radius: float, amount: float) -> None:
        self.native.add_track(
            track.start.x,
            track.start.y,
            track.end.x,
            track.end.y,
            int(track.layer),
            radius,
            amount,
        )

    def add_via(self, via: Via, radius: float, amount: float) -> None:
        self.native.add_via(via.position.x, via.position.y, radius, amount)


class _PreparedLoop:
    def __init__(self, points: tuple[Point, ...], distance: float, cell_size: float):
        self.cell_size = cell_size
        self.by_y: dict[int, list[tuple[Point, Point]]] = {}
        self.nearby: dict[tuple[int, int], list[tuple[Point, Point]]] = {}
        for index, start in enumerate(points):
            end = points[(index + 1) % len(points)]
            edge = (start, end)
            for y in range(
                floor(min(start.y, end.y) / cell_size),
                floor(max(start.y, end.y) / cell_size) + 1,
            ):
                self.by_y.setdefault(y, []).append(edge)
            for x in range(
                floor((min(start.x, end.x) - distance) / cell_size),
                floor((max(start.x, end.x) + distance) / cell_size) + 1,
            ):
                for y in range(
                    floor((min(start.y, end.y) - distance) / cell_size),
                    floor((max(start.y, end.y) + distance) / cell_size) + 1,
                ):
                    self.nearby.setdefault((x, y), []).append(edge)

    def contains(self, point: Point) -> bool:
        inside = False
        for start, end in self.by_y.get(floor(point.y / self.cell_size), ()):
            if (start.y > point.y) != (end.y > point.y):
                x_cross = (end.x - start.x) * (point.y - start.y) / (end.y - start.y) + start.x
                if point.x < x_cross:
                    inside = not inside
        return inside

    def near_boundary(self, point: Point, distance: float) -> bool:
        key = (floor(point.x / self.cell_size), floor(point.y / self.cell_size))
        return any(
            distance_point_to_segment(point, start, end) + 1e-9 < distance
            for start, end in self.nearby.get(key, ())
        )


class _PreparedArea:
    def __init__(self, area: CopperArea | KeepoutArea, required: float, cell_size: float):
        self.area = area
        self.required = required
        self.outline = _PreparedLoop(area.outline, required, cell_size)
        self.holes = tuple(_PreparedLoop(hole, required, cell_size) for hole in area.holes)

    def blocked(self, point: Point) -> bool:
        in_copper = self.outline.contains(point) and not any(
            hole.contains(point) for hole in self.holes
        )
        if in_copper:
            return True
        return self.outline.near_boundary(point, self.required) or any(
            hole.near_boundary(point, self.required) for hole in self.holes
        )


@dataclass(frozen=True, slots=True)
class _Hole:
    center: Point
    diameter: float


Obstacle: TypeAlias = tuple[Pad | Track | Via | _PreparedArea | _Hole, float]


class _ClearanceMap:
    """Spatial hash of obstacles already inflated for one route width or via size."""

    def __init__(
        self,
        board: Board,
        net: str,
        radius: float,
        cell_size: float = 2.0,
        via_drill: float | None = None,
    ):
        self.board = board
        self.net = net
        self.radius = radius
        self.rules = board.rules_for(net)
        self.cell_size = cell_size
        self.via_drill = via_drill
        self.cells: dict[Layer, dict[tuple[int, int], list[Obstacle]]] = {
            layer: {} for layer in board.copper_layers
        }
        self._native_rectangles: list[tuple[int, float, float, float, float, float]] = []
        self._native_segments: list[tuple[int, float, float, float, float, float]] = []
        self._native_points: list[tuple[int, float, float, float]] = []
        self._native_areas: list[
            tuple[int, list[tuple[float, float]], list[list[tuple[float, float]]], float]
        ] = []

        for pad in board.pads:
            required = 0.0
            if pad.net != net:
                clearance = max(
                    self.rules.clearance,
                    board.rules_for(pad.net).clearance if pad.net else self.rules.clearance,
                )
                required = radius + clearance
            if via_drill is not None and board.via_pad_hole_clearance:
                required = max(
                    required,
                    via_drill / 2 + board.via_pad_hole_clearance,
                )
            if required:
                bounds = (
                    pad.center.x - pad.width / 2 - required,
                    pad.center.y - pad.height / 2 - required,
                    pad.center.x + pad.width / 2 + required,
                    pad.center.y + pad.height / 2 + required,
                )
                for layer in pad.layers.intersection(board.copper_layers):
                    self._insert(layer, bounds, (pad, required))
                    self._native_rectangles.append(
                        (int(layer), pad.center.x, pad.center.y, pad.width, pad.height, required)
                    )
            if via_drill is not None and pad.drill:
                self._insert_hole(pad.center, pad.drill)

        for track in board.tracks:
            if track.net == net:
                continue
            required = (
                radius
                + track.width / 2
                + max(self.rules.clearance, board.rules_for(track.net).clearance)
            )
            bounds = (
                min(track.start.x, track.end.x) - required,
                min(track.start.y, track.end.y) - required,
                max(track.start.x, track.end.x) + required,
                max(track.start.y, track.end.y) + required,
            )
            self._insert(track.layer, bounds, (track, required))
            self._native_segments.append(
                (
                    int(track.layer),
                    track.start.x,
                    track.start.y,
                    track.end.x,
                    track.end.y,
                    required,
                )
            )

        for via in board.vias:
            if via.net != net:
                required = (
                    radius
                    + via.diameter / 2
                    + max(self.rules.clearance, board.rules_for(via.net).clearance)
                )
                bounds = (
                    via.position.x - required,
                    via.position.y - required,
                    via.position.x + required,
                    via.position.y + required,
                )
                for layer in board.copper_layers:
                    self._insert(layer, bounds, (via, required))
                    self._native_points.append(
                        (int(layer), via.position.x, via.position.y, required)
                    )
            if via_drill is not None:
                self._insert_hole(via.position, via.drill)

        for area in board.copper_areas:
            if area.net == net:
                continue
            required = radius + max(
                self.rules.clearance,
                area.clearance,
                board.rules_for(area.net).clearance,
            )
            min_x, min_y, max_x, max_y = area.bounds
            prepared = _PreparedArea(area, required, cell_size)
            self._insert(
                area.layer,
                (
                    min_x - required,
                    min_y - required,
                    max_x + required,
                    max_y + required,
                ),
                (prepared, required),
            )
            self._native_areas.append(
                (
                    int(area.layer),
                    [(point.x, point.y) for point in area.outline],
                    [[(point.x, point.y) for point in hole] for hole in area.holes],
                    required,
                )
            )

        for area in board.keepouts:
            blocked = area.blocks_vias if via_drill is not None else area.blocks_tracks
            if not blocked:
                continue
            required = radius
            min_x, min_y, max_x, max_y = area.bounds
            prepared = _PreparedArea(area, required, cell_size)
            for layer in area.layers:
                self._insert(
                    layer,
                    (
                        min_x - required,
                        min_y - required,
                        max_x + required,
                        max_y + required,
                    ),
                    (prepared, required),
                )
                self._native_areas.append(
                    (
                        int(layer),
                        [(point.x, point.y) for point in area.outline],
                        [[(point.x, point.y) for point in hole] for hole in area.holes],
                        required,
                    )
                )

        self.native = NativeClearanceMap(
            [(point.x, point.y) for point in board.outline],
            [[(point.x, point.y) for point in cutout] for cutout in board.cutouts],
            radius,
            self.rules.edge_clearance,
            cell_size,
            self._native_rectangles,
            self._native_segments,
            self._native_points,
            self._native_areas,
        )

    def _insert_hole(self, center: Point, diameter: float) -> None:
        assert self.via_drill is not None
        required = (self.via_drill + diameter) / 2 + self.board.min_hole_to_hole
        bounds = (
            center.x - required,
            center.y - required,
            center.x + required,
            center.y + required,
        )
        hole = _Hole(center, diameter)
        for layer in self.board.copper_layers:
            self._insert(layer, bounds, (hole, required))
            self._native_points.append((int(layer), center.x, center.y, required))

    def add_via(self, via: Via) -> None:
        if self.via_drill is not None:
            self._insert_hole(via.position, via.drill)

    def _insert(
        self,
        layer: Layer,
        bounds: tuple[float, float, float, float],
        obstacle: Obstacle,
    ) -> None:
        min_x, min_y, max_x, max_y = bounds
        for x in range(floor(min_x / self.cell_size), floor(max_x / self.cell_size) + 1):
            for y in range(floor(min_y / self.cell_size), floor(max_y / self.cell_size) + 1):
                self.cells[layer].setdefault((x, y), []).append(obstacle)

    def clear(self, point: Point, layer: Layer) -> bool:
        if not point_in_polygon(point, self.board.outline):
            return False
        if (
            distance_to_polygon_edge(point, self.board.outline) + 1e-9
            < self.radius + self.rules.edge_clearance
        ):
            return False
        for cutout in self.board.cutouts:
            if point_in_polygon(point, cutout):
                return False
            if (
                distance_to_polygon_edge(point, cutout) + 1e-9
                < self.radius + self.rules.edge_clearance
            ):
                return False

        key = (floor(point.x / self.cell_size), floor(point.y / self.cell_size))
        for obstacle, required in self.cells[layer].get(key, ()):
            if isinstance(obstacle, Pad):
                if (
                    distance_point_to_rectangle(
                        point, obstacle.center, obstacle.width, obstacle.height
                    )
                    + 1e-9
                    < required
                ):
                    return False
            elif isinstance(obstacle, Track):
                if distance_point_to_segment(point, obstacle.start, obstacle.end) + 1e-9 < required:
                    return False
            elif isinstance(obstacle, Via):
                if point.distance_to(obstacle.position) + 1e-9 < required:
                    return False
            elif isinstance(obstacle, _Hole):
                if point.distance_to(obstacle.center) + 1e-9 < required:
                    return False
            elif obstacle.blocked(point):
                return False
        return True


class Router:
    """Deterministic multilayer 45-degree negotiated-congestion router.

    Coordinates and dimensions use millimetres. Existing copper is immutable.
    """

    _MOVES = ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (-1, -1), (1, -1))

    def __init__(
        self,
        config: RouterConfig | None = None,
        progress: Callable[[int, int, int], None] | None = None,
        cleanup_progress: Callable[[int, int], None] | None = None,
        iteration_progress: Callable[[RoutingIteration], None] | None = None,
        connection_progress: Callable[[int, int, int, str], None] | None = None,
    ):
        self.config = config or RouterConfig()
        self.progress = progress
        self.cleanup_progress = cleanup_progress
        self.iteration_progress = iteration_progress
        self.connection_progress = connection_progress
        if self.config.grid <= 0:
            raise ValueError("Grid spacing must be positive")
        if self.config.max_visited <= 0:
            raise ValueError("A* visited-state limit must be positive")
        if not 0 <= self.config.max_vias_per_connection <= 255:
            raise ValueError("Maximum vias per connection must be between 0 and 255")
        if self.config.max_negotiation_iterations <= 0:
            raise ValueError("Negotiation iteration count must be positive")
        if self.config.present_congestion_penalty <= 0:
            raise ValueError("Present congestion penalty must be positive")
        if self.config.historical_congestion_penalty <= 0:
            raise ValueError("Historical congestion penalty must be positive")
        if self.config.cleanup_passes < 0:
            raise ValueError("Cleanup pass count cannot be negative")
        if self.config.direction_penalty < 0:
            raise ValueError("Direction penalty cannot be negative")

    def _layer_direction(self, layer: Layer) -> RoutingDirection:
        configured = dict(self.config.layer_directions).get(layer)
        if configured is not None:
            return configured
        if layer == Layer.FRONT:
            return self.config.front_direction
        if layer == Layer.BACK:
            return self.config.back_direction
        return RoutingDirection.ANY

    @staticmethod
    def _direction_steps(direction: RoutingDirection, dx: int, dy: int) -> int:
        if direction == RoutingDirection.ANY:
            return 0
        if direction == RoutingDirection.HORIZONTAL:
            return 0 if dy == 0 else 1 if dx else 2
        if direction == RoutingDirection.VERTICAL:
            return 0 if dx == 0 else 1 if dy else 2
        if direction == RoutingDirection.DIAGONAL_DOWN:
            return 0 if dx and dy and dx == dy else 1 if not (dx and dy) else 2
        if direction == RoutingDirection.DIAGONAL_UP:
            return 0 if dx and dy and dx == -dy else 1 if not (dx and dy) else 2
        return 0

    @staticmethod
    def _has_existing_layer_bridge(board: Board, net: str, point: Point) -> bool:
        if any(
            via.net == net and point.distance_to(via.position) <= via.diameter / 2
            for via in board.vias
        ):
            return True
        track_radius = board.rules_for(net).track_width / 2
        return any(
            pad.net == net
            and pad.layers == frozenset(board.copper_layers)
            and point.distance_to(pad.center)
            <= max(0.0, min(pad.width, pad.height) / 2 - track_radius)
            for pad in board.pads
        )

    def _line_clear(
        self,
        start: Point,
        end: Point,
        layer: Layer,
        clearance_map: _ClearanceMap,
    ) -> bool:
        distance = start.distance_to(end)
        steps = max(1, ceil(distance / (self.config.grid / 2)))
        return all(
            clearance_map.clear(
                Point(
                    start.x + (end.x - start.x) * index / steps,
                    start.y + (end.y - start.y) * index / steps,
                ),
                layer,
            )
            for index in range(1, steps + 1)
        )

    def _fanout_clear(
        self,
        start: Point,
        target: Point,
        layer: Layer,
        clearance_map: _ClearanceMap,
    ) -> bool:
        elbow = Point(target.x, start.y)
        return self._line_clear(start, elbow, layer, clearance_map) and self._line_clear(
            elbow, target, layer, clearance_map
        )

    def route(
        self,
        board: Board,
        excluded_nets: set[str] | None = None,
        selected_pad_ids: set[str] | None = None,
        priority_pairs: set[frozenset[str]] | None = None,
    ) -> RoutePlan:
        problem = self.prepare(board, excluded_nets, selected_pad_ids, priority_pairs)
        return self.route_problem(problem)

    def prepare(
        self,
        board: Board,
        excluded_nets: set[str] | None = None,
        selected_pad_ids: set[str] | None = None,
        priority_pairs: set[frozenset[str]] | None = None,
    ) -> RoutingProblem:
        if len(board.outline) < 3:
            raise ValueError("A closed board outline with at least three vertices is required")
        connections = tuple(
            unrouted_connections(
                board,
                excluded_nets,
                selected_pad_ids,
                priority_pairs,
            )
        )
        return RoutingProblem(board, connections, self.config, board_fingerprint(board))

    def route_problem(self, problem: RoutingProblem) -> RoutePlan:
        if self.config != problem.config:
            raise ValueError("Routing problem was prepared with different router settings")
        board = problem.board
        connections = list(problem.connections)
        if not connections:
            return RoutePlan()
        return self._route_problem_native(board, connections)

    def _route_problem_native(self, board: Board, connections: list[Connection]) -> RoutePlan:
        net_index: dict[str, int] = {}
        track_maps: list[Any] = []
        via_maps: list[Any] = []
        map_index: list[int] = []
        keys: list[tuple[str, str, str]] = []
        nets: list[str] = []
        sources: list[tuple[float, float]] = []
        source_layers: list[list[int]] = []
        targets: list[tuple[float, float, float, float]] = []
        target_layers: list[list[int]] = []
        congestion: list[float] = []
        bridges: list[list[tuple[float, float, float]]] = []
        track_widths: list[float] = []
        clearances: list[float] = []
        via_diameters: list[float] = []
        via_drills: list[float] = []

        all_copper = frozenset(board.copper_layers)
        for connection in connections:
            rules = board.rules_for(connection.net)
            if connection.net not in net_index:
                net_index[connection.net] = len(track_maps)
                track_maps.append(
                    _ClearanceMap(board, connection.net, rules.track_width / 2).native
                )
                via_maps.append(
                    _ClearanceMap(
                        board,
                        connection.net,
                        rules.via_diameter / 2,
                        via_drill=rules.via_drill,
                    ).native
                )
            map_index.append(net_index[connection.net])
            keys.append((connection.net, connection.source.id, connection.target.id))
            nets.append(connection.net)
            sources.append((connection.source.center.x, connection.source.center.y))
            source_layers.append(sorted(int(layer) for layer in connection.source.layers))
            targets.append(
                (
                    connection.target.center.x,
                    connection.target.center.y,
                    connection.target.width,
                    connection.target.height,
                )
            )
            target_layers.append(sorted(int(layer) for layer in connection.target.layers))
            congestion.append(connection.congestion_multiplier)
            track_radius = rules.track_width / 2
            connection_bridges: list[tuple[float, float, float]] = [
                (via.position.x, via.position.y, via.diameter / 2)
                for via in board.vias
                if via.net == connection.net
            ]
            connection_bridges.extend(
                (
                    pad.center.x,
                    pad.center.y,
                    max(0.0, min(pad.width, pad.height) / 2 - track_radius),
                )
                for pad in board.pads
                if pad.net == connection.net and pad.layers == all_copper
            )
            bridges.append(connection_bridges)
            track_widths.append(rules.track_width)
            clearances.append(rules.clearance)
            via_diameters.append(rules.via_diameter)
            via_drills.append(rules.via_drill)

        def connection_callback(iteration: int, index: int, total: int, net: str) -> None:
            if self.connection_progress is not None:
                self.connection_progress(iteration, index, total, net)

        def iteration_callback(
            iteration: int,
            conflicts: int,
            failures: int,
            statuses: list[str],
        ) -> None:
            if self.progress is not None:
                self.progress(iteration, conflicts, failures)
            if self.iteration_progress is not None:
                self.iteration_progress(
                    RoutingIteration(iteration, conflicts, failures, tuple(statuses))
                )

        (
            conflict_free,
            best_candidates,
            best_failures,
            best_conflicted,
        ) = native_negotiate(
            track_maps,
            via_maps,
            map_index,
            keys,
            nets,
            sources,
            source_layers,
            targets,
            target_layers,
            congestion,
            bridges,
            track_widths,
            clearances,
            via_diameters,
            via_drills,
            self.config.grid,
            board.bounds,
            [int(layer) for layer in board.copper_layers],
            [(int(layer), int(self._layer_direction(layer))) for layer in board.copper_layers],
            self.config.direction_penalty,
            board.min_hole_to_hole,
            self.config.max_vias_per_connection,
            self.config.max_visited,
            self.config.max_negotiation_iterations,
            self.config.present_congestion_penalty,
            self.config.historical_congestion_penalty,
            self.config.via_cost,
            self.config.bend_cost,
            connection_callback,
            iteration_callback,
        )

        candidates = [
            _CandidateRoute(
                connections[position],
                [
                    Track(
                        connections[position].net,
                        Point(x1, y1),
                        Point(x2, y2),
                        Layer(layer),
                        width,
                    )
                    for x1, y1, x2, y2, width, layer in tracks
                ],
                [
                    Via(connections[position].net, Point(x, y), diameter, drill)
                    for x, y, diameter, drill in vias
                ],
            )
            for position, tracks, vias in best_candidates
        ]
        if conflict_free:
            finalized = self._finalize_routes(board, candidates)
            return self._candidate_plan(finalized)

        index_of = {position: index for index, (position, _, _) in enumerate(best_candidates)}
        failures = [
            RouteFailure(
                connections[position].net,
                connections[position].source.id,
                connections[position].target.id,
                reason,
            )
            for position, reason in best_failures
        ]
        conflicted = {index_of[position] for position in best_conflicted}
        for index in sorted(conflicted):
            connection = candidates[index].connection
            failures.append(
                RouteFailure(
                    connection.net,
                    connection.source.id,
                    connection.target.id,
                    "negotiated congestion did not resolve",
                )
            )
        if not self.config.allow_partial:
            return RoutePlan(failures=failures)
        accepted = [route for index, route in enumerate(candidates) if index not in conflicted]
        plan = self._candidate_plan(accepted)
        plan.failures = failures
        return plan

    def _route_problem_python(self, problem: RoutingProblem) -> RoutePlan:
        if self.config != problem.config:
            raise ValueError("Routing problem was prepared with different router settings")
        board = problem.board
        connections = list(problem.connections)
        if not connections:
            return RoutePlan()

        fixed_maps: dict[str, tuple[_ClearanceMap, _ClearanceMap]] = {}
        for net in {connection.net for connection in connections}:
            rules = board.rules_for(net)
            fixed_maps[net] = (
                _ClearanceMap(board, net, rules.track_width / 2),
                _ClearanceMap(
                    board,
                    net,
                    rules.via_diameter / 2,
                    via_drill=rules.via_drill,
                ),
            )
        historical = _PenaltyMap(self.config.grid)
        candidates_by_key: dict[ConnectionKey, _CandidateRoute] = {}
        failures_by_key: dict[ConnectionKey, RouteFailure] = {}
        best: (
            tuple[
                tuple[int, int, float],
                list[_CandidateRoute],
                list[RouteFailure],
                list[_Conflict],
            ]
            | None
        ) = None
        ordered = sorted(connections, key=self._connection_key)
        for iteration in range(self.config.max_negotiation_iterations):
            offset = iteration % len(ordered)
            iteration_order = ordered[offset:] + ordered[:offset]
            if iteration % 2:
                iteration_order.reverse()

            for index, connection in enumerate(iteration_order):
                if self.connection_progress is not None:
                    self.connection_progress(
                        iteration + 1, index + 1, len(iteration_order), connection.net
                    )
                key = self._connection_key(connection)
                candidates_by_key.pop(key, None)
                routing_costs = historical.copy()
                for occupied in candidates_by_key.values():
                    self._add_present_congestion(
                        board,
                        routing_costs,
                        connection.net,
                        occupied,
                    )
                track_map, via_map = fixed_maps[connection.net]
                path = self._find_path(
                    board,
                    connection,
                    track_map,
                    via_map,
                    routing_costs,
                )
                if path is None:
                    failures_by_key[key] = RouteFailure(
                        connection.net,
                        connection.source.id,
                        connection.target.id,
                        "no path found",
                    )
                    continue
                failures_by_key.pop(key, None)
                tracks, vias = self._materialize_path(board, connection, path)
                candidates_by_key[key] = _CandidateRoute(connection, tracks, vias)

            candidates = [
                candidates_by_key[self._connection_key(connection)]
                for connection in connections
                if self._connection_key(connection) in candidates_by_key
            ]
            failures = list(failures_by_key.values())
            conflicts = self._conflicts(board, candidates)
            if self.progress is not None:
                self.progress(iteration + 1, len(conflicts), len(failures))
            if self.iteration_progress is not None:
                conflicted_keys = {
                    candidates[index].key
                    for conflict in conflicts
                    for index in (conflict.left, conflict.right)
                }
                failure_keys = {
                    (failure.net, failure.source, failure.target) for failure in failures
                }
                candidate_keys = {candidate.key for candidate in candidates}
                statuses = tuple(
                    "failure"
                    if self._connection_key(connection) in failure_keys
                    else "conflict"
                    if self._connection_key(connection) in conflicted_keys
                    else "routed"
                    if self._connection_key(connection) in candidate_keys
                    else "pending"
                    for connection in connections
                )
                self.iteration_progress(
                    RoutingIteration(iteration + 1, len(conflicts), len(failures), statuses)
                )
            score = (
                len(failures),
                len(conflicts),
                sum(
                    track.start.distance_to(track.end)
                    for route in candidates
                    for track in route.tracks
                ),
            )
            if best is None or score < best[0]:
                best = (score, candidates, failures, conflicts)
            if not failures and not conflicts:
                finalized = self._finalize_routes(board, candidates)
                return self._candidate_plan(finalized)
            if not conflicts:
                break
            for conflict in conflicts:
                self._add_historical_congestion(historical, conflict.sites)

        assert best is not None
        _, candidates, failures, conflicts = best
        conflicted = {index for conflict in conflicts for index in (conflict.left, conflict.right)}
        for index in sorted(conflicted):
            connection = candidates[index].connection
            failures.append(
                RouteFailure(
                    connection.net,
                    connection.source.id,
                    connection.target.id,
                    "negotiated congestion did not resolve",
                )
            )
        if not self.config.allow_partial:
            return RoutePlan(failures=failures)
        accepted = [route for index, route in enumerate(candidates) if index not in conflicted]
        plan = self._candidate_plan(accepted)
        plan.failures = failures
        return plan

    def _finalize_routes(
        self,
        board: Board,
        candidates: list[_CandidateRoute],
    ) -> list[_CandidateRoute]:
        if not self.config.cleanup_passes:
            return candidates
        result = list(candidates)
        for pass_index in range(self.config.cleanup_passes):
            order = list(range(len(result)))
            if pass_index % 2:
                order.reverse()
            improvements = 0
            for index in order:
                current = result[index]
                others = [route for other_index, route in enumerate(result) if other_index != index]
                working = Board(
                    outline=board.outline,
                    pads=list(board.pads),
                    tracks=[*board.tracks, *(track for route in others for track in route.tracks)],
                    vias=[*board.vias, *(via for route in others for via in route.vias)],
                    rules_by_net=dict(board.rules_by_net),
                    default_rules=board.default_rules,
                    cutouts=board.cutouts,
                    copper_areas=list(board.copper_areas),
                    min_hole_to_hole=board.min_hole_to_hole,
                    keepouts=list(board.keepouts),
                    via_pad_hole_clearance=board.via_pad_hole_clearance,
                    copper_layers=board.copper_layers,
                )
                rules = working.rules_for(current.connection.net)
                path = self._find_path(
                    working,
                    current.connection,
                    _ClearanceMap(working, current.connection.net, rules.track_width / 2),
                    _ClearanceMap(
                        working,
                        current.connection.net,
                        rules.via_diameter / 2,
                        via_drill=rules.via_drill,
                    ),
                    bridge_board=board,
                    direction_penalty=0.0,
                )
                if path is None:
                    continue
                tracks, vias = self._materialize_path(
                    working,
                    current.connection,
                    path,
                    bridge_board=board,
                )
                replacement = _CandidateRoute(current.connection, tracks, vias)
                if self._route_has_close_vias(board, replacement):
                    continue
                if any(self._route_conflict_sites(board, replacement, other) for other in others):
                    continue
                if self._route_score(replacement) + 1e-9 < self._route_score(current):
                    result[index] = replacement
                    improvements += 1
            if self.cleanup_progress is not None:
                self.cleanup_progress(pass_index + 1, improvements)
            if not improvements:
                break
        if self._conflicts(board, result):
            raise RuntimeError("Cleanup introduced a generated-route conflict")
        return result

    def _route_score(self, route: _CandidateRoute) -> float:
        length = sum(track.start.distance_to(track.end) for track in route.tracks)
        bends = 0
        bridge_transitions = 0
        for previous, current in itertools.pairwise(route.tracks):
            if previous.end.distance_to(current.start) > 1e-9:
                continue
            if previous.layer != current.layer:
                bridge_transitions += 1
                continue
            first = (
                previous.end.x - previous.start.x,
                previous.end.y - previous.start.y,
            )
            second = (
                current.end.x - current.start.x,
                current.end.y - current.start.y,
            )
            cross = first[0] * second[1] - first[1] * second[0]
            dot = first[0] * second[0] + first[1] * second[1]
            if abs(cross) > 1e-9 or dot <= 0:
                bends += 1
        return (
            length
            + bends * self.config.bend_cost
            + len(route.vias) * self.config.via_cost
            + bridge_transitions * 0.05
        )

    @staticmethod
    def _route_has_close_vias(board: Board, route: _CandidateRoute) -> bool:
        return any(
            left.position.distance_to(right.position) + 1e-9
            < (left.drill + right.drill) / 2 + board.min_hole_to_hole
            for index, left in enumerate(route.vias)
            for right in route.vias[index + 1 :]
        )

    @staticmethod
    def _connection_key(connection: Connection) -> ConnectionKey:
        return (connection.net, connection.source.id, connection.target.id)

    @staticmethod
    def _candidate_plan(candidates: list[_CandidateRoute]) -> RoutePlan:
        def shared_group(connection: Connection) -> str | None:
            target_groups = set(connection.target.group_path)
            return next(
                (group for group in connection.source.group_path if group in target_groups),
                None,
            )

        return RoutePlan(
            tracks=[
                replace(track, group=shared_group(route.connection))
                for route in candidates
                for track in route.tracks
            ],
            vias=[
                replace(via, group=shared_group(route.connection))
                for route in candidates
                for via in route.vias
            ],
        )

    def _add_present_congestion(
        self,
        board: Board,
        penalties: _PenaltyMap,
        searching_net: str,
        occupied: _CandidateRoute,
    ) -> None:
        searching_rules = board.rules_for(searching_net)
        occupied_rules = board.rules_for(occupied.connection.net)
        if searching_net == occupied.connection.net:
            for via in occupied.vias:
                penalties.add_via(
                    via,
                    searching_rules.via_drill / 2 + via.drill / 2 + board.min_hole_to_hole,
                    self.config.present_congestion_penalty,
                )
            return
        clearance = max(searching_rules.clearance, occupied_rules.clearance)
        for track in occupied.tracks:
            penalties.add_track(
                track,
                searching_rules.track_width / 2 + track.width / 2 + clearance,
                self.config.present_congestion_penalty,
            )
        for via in occupied.vias:
            penalties.add_via(
                via,
                searching_rules.track_width / 2 + via.diameter / 2 + clearance,
                self.config.present_congestion_penalty,
            )

    def _add_historical_congestion(
        self,
        penalties: _PenaltyMap,
        sites: tuple[tuple[Point, Layer | None, float], ...],
    ) -> None:
        for point, layer, radius in sites:
            penalties.add_point(
                point,
                layer,
                radius,
                self.config.historical_congestion_penalty,
            )

    @staticmethod
    def _conflicts(board: Board, candidates: list[_CandidateRoute]) -> list[_Conflict]:
        if len(candidates) < 2:
            return []
        payload = native_conflicts(
            [candidate.connection.net for candidate in candidates],
            [board.rules_for(candidate.connection.net).clearance for candidate in candidates],
            [_track_payload(candidate) for candidate in candidates],
            [_via_payload(candidate) for candidate in candidates],
            board.min_hole_to_hole,
        )
        return [
            _Conflict(left, right, tuple(_conflict_site(site) for site in sites))
            for left, right, sites in payload
        ]

    @staticmethod
    def _conflicts_python(board: Board, candidates: list[_CandidateRoute]) -> list[_Conflict]:
        result: list[_Conflict] = []
        for left in range(len(candidates)):
            for right in range(left + 1, len(candidates)):
                sites = Router._route_conflict_sites_python(
                    board, candidates[left], candidates[right]
                )
                if sites:
                    result.append(_Conflict(left, right, tuple(sites)))
        return result

    @staticmethod
    def _segment_conflict_sites(
        left: Track,
        right: Track,
        required: float,
        spacing: float,
    ) -> list[tuple[Point, Layer | None, float]]:
        sites: list[tuple[Point, Layer | None, float]] = []
        for track, other in ((left, right), (right, left)):
            distance = track.start.distance_to(track.end)
            steps = max(1, ceil(distance / spacing))
            for index in range(steps + 1):
                point = Point(
                    track.start.x + (track.end.x - track.start.x) * index / steps,
                    track.start.y + (track.end.y - track.start.y) * index / steps,
                )
                if distance_point_to_segment(point, other.start, other.end) + 1e-9 < required:
                    sites.append((point, track.layer, required))
        return sites

    @staticmethod
    def _route_conflict_sites(
        board: Board,
        left: _CandidateRoute,
        right: _CandidateRoute,
    ) -> list[tuple[Point, Layer | None, float]]:
        payload = native_conflicts(
            [left.connection.net, right.connection.net],
            [
                board.rules_for(left.connection.net).clearance,
                board.rules_for(right.connection.net).clearance,
            ],
            [_track_payload(left), _track_payload(right)],
            [_via_payload(left), _via_payload(right)],
            board.min_hole_to_hole,
        )
        if not payload:
            return []
        return [_conflict_site(site) for site in payload[0][2]]

    @staticmethod
    def _route_conflict_sites_python(
        board: Board,
        left: _CandidateRoute,
        right: _CandidateRoute,
    ) -> list[tuple[Point, Layer | None, float]]:
        sites: list[tuple[Point, Layer | None, float]] = []
        same_net = left.connection.net == right.connection.net
        clearance = max(
            board.rules_for(left.connection.net).clearance,
            board.rules_for(right.connection.net).clearance,
        )
        if not same_net:
            for left_track in left.tracks:
                for right_track in right.tracks:
                    required = (left_track.width + right_track.width) / 2 + clearance
                    if (
                        left_track.layer == right_track.layer
                        and segment_distance(
                            left_track.start,
                            left_track.end,
                            right_track.start,
                            right_track.end,
                        )
                        + 1e-9
                        < required
                    ):
                        sites.extend(
                            Router._segment_conflict_sites(
                                left_track,
                                right_track,
                                required,
                                spacing=0.125,
                            )
                        )
                for via in right.vias:
                    required = via.diameter / 2 + left_track.width / 2 + clearance
                    if (
                        distance_point_to_segment(via.position, left_track.start, left_track.end)
                        + 1e-9
                        < required
                    ):
                        sites.append((via.position, None, required))
            for right_track in right.tracks:
                for via in left.vias:
                    required = via.diameter / 2 + right_track.width / 2 + clearance
                    if (
                        distance_point_to_segment(
                            via.position,
                            right_track.start,
                            right_track.end,
                        )
                        + 1e-9
                        < required
                    ):
                        sites.append((via.position, None, required))
        for left_via in left.vias:
            for right_via in right.vias:
                distance = left_via.position.distance_to(right_via.position)
                copper_required = (left_via.diameter + right_via.diameter) / 2 + clearance
                if not same_net and distance + 1e-9 < copper_required:
                    sites.append((left_via.position, None, copper_required))
                    sites.append((right_via.position, None, copper_required))
                hole_required = (left_via.drill + right_via.drill) / 2 + board.min_hole_to_hole
                if distance + 1e-9 < hole_required:
                    sites.append((left_via.position, None, hole_required))
                    sites.append((right_via.position, None, hole_required))
        return list(dict.fromkeys(sites))

    def _find_path(
        self,
        board: Board,
        connection: Connection,
        track_map: _ClearanceMap | None = None,
        via_map: _ClearanceMap | None = None,
        penalties: _PenaltyMap | None = None,
        bridge_board: Board | None = None,
        direction_penalty: float | None = None,
    ) -> list[State] | None:
        rules = board.rules_for(connection.net)
        track_map = track_map or _ClearanceMap(board, connection.net, rules.track_width / 2)
        via_map = via_map or _ClearanceMap(
            board,
            connection.net,
            rules.via_diameter / 2,
            via_drill=rules.via_drill,
        )
        bridges_board = bridge_board or board
        track_radius = bridges_board.rules_for(connection.net).track_width / 2
        bridges = [
            (via.position.x, via.position.y, via.diameter / 2)
            for via in bridges_board.vias
            if via.net == connection.net
        ]
        bridges.extend(
            (
                pad.center.x,
                pad.center.y,
                max(0.0, min(pad.width, pad.height) / 2 - track_radius),
            )
            for pad in bridges_board.pads
            if pad.net == connection.net and pad.layers == frozenset(bridges_board.copper_layers)
        )
        native_penalties = penalties or _PenaltyMap(self.config.grid)
        raw_path = native_find_path(
            track_map.native,
            via_map.native,
            self.config.grid,
            board.bounds,
            (connection.source.center.x, connection.source.center.y),
            [int(layer) for layer in sorted(connection.source.layers)],
            (
                connection.target.center.x,
                connection.target.center.y,
                connection.target.width,
                connection.target.height,
            ),
            [int(layer) for layer in sorted(connection.target.layers)],
            self.config.max_visited,
            self.config.max_vias_per_connection,
            self.config.via_cost,
            self.config.bend_cost,
            [(int(layer), int(self._layer_direction(layer))) for layer in board.copper_layers],
            [int(layer) for layer in board.copper_layers],
            self.config.direction_penalty if direction_penalty is None else direction_penalty,
            connection.congestion_multiplier,
            native_penalties.native,
            bridges,
        )
        if raw_path is None:
            return None
        return [(x, y, Layer(layer), vias) for x, y, layer, vias in raw_path]

    def _find_path_python(
        self,
        board: Board,
        connection: Connection,
        track_map: _ClearanceMap | None = None,
        via_map: _ClearanceMap | None = None,
        penalties: _PenaltyMap | None = None,
        bridge_board: Board | None = None,
        direction_penalty: float | None = None,
    ) -> list[State] | None:
        grid = self.config.grid
        effective_direction_penalty = (
            self.config.direction_penalty if direction_penalty is None else direction_penalty
        )
        min_x, min_y, max_x, max_y = board.bounds

        def point_at(x: int, y: int) -> Point:
            return Point(
                connection.source.center.x + x * grid, connection.source.center.y + y * grid
            )

        min_ix = floor((min_x - connection.source.center.x) / grid)
        min_iy = floor((min_y - connection.source.center.y) / grid)
        max_ix = ceil((max_x - connection.source.center.x) / grid)
        max_iy = ceil((max_y - connection.source.center.y) / grid)
        target_layers = connection.target.layers
        rules = board.rules_for(connection.net)
        track_map = track_map or _ClearanceMap(board, connection.net, rules.track_width / 2)
        via_map = via_map or _ClearanceMap(
            board,
            connection.net,
            rules.via_diameter / 2,
            via_drill=rules.via_drill,
        )

        starts = [
            (0, 0, layer, 0)
            for layer in sorted(connection.source.layers)
            if layer in board.copper_layers
            and self._layer_direction(layer) != RoutingDirection.DISABLED
            and track_map.clear(connection.source.center, layer)
        ]
        if not starts:
            return None

        counter = itertools.count()
        queue: list[tuple[float, float, int, State]] = []
        costs: dict[State, float] = {}
        came_from: dict[State, State] = {}
        pareto: dict[tuple[int, int, Layer], list[tuple[State, float]]] = {}

        def heuristic(state: State) -> float:
            point = point_at(state[0], state[1])
            dx = abs(point.x - connection.target.center.x) / grid
            dy = abs(point.y - connection.target.center.y) / grid
            diagonal = min(dx, dy)
            planar = diagonal * sqrt(2) + max(dx, dy) - diagonal
            layer_cost = 0 if state[2] in target_layers else self.config.via_cost
            return planar * grid + layer_cost

        for state in starts:
            costs[state] = 0.0
            pareto.setdefault(state[:-1], []).append((state, 0.0))
            heapq.heappush(queue, (heuristic(state), 0.0, next(counter), state))

        def accept(next_state: State, new_cost: float, previous: State) -> None:
            base = next_state[:-1]
            via_count = next_state[-1]
            frontier = pareto.setdefault(base, [])
            if any(state[-1] <= via_count and cost <= new_cost for state, cost in frontier):
                return
            dominated = [
                state for state, cost in frontier if state[-1] >= via_count and cost >= new_cost
            ]
            for state in dominated:
                costs.pop(state, None)
            frontier[:] = [entry for entry in frontier if entry[0] not in dominated]
            frontier.append((next_state, new_cost))
            costs[next_state] = new_cost
            came_from[next_state] = previous
            heapq.heappush(
                queue,
                (new_cost + heuristic(next_state), new_cost, next(counter), next_state),
            )

        visited = 0
        goal: State | None = None
        while queue and visited < self.config.max_visited:
            _, queued_cost, _, current = heapq.heappop(queue)
            if queued_cost != costs.get(current):
                continue
            visited += 1
            current_point = point_at(current[0], current[1])
            if (
                connection.target.contains(current_point)
                and current[2] in target_layers
                and self._fanout_clear(
                    current_point,
                    connection.target.center,
                    current[2],
                    track_map,
                )
            ):
                goal = current
                break

            x, y, layer, via_count = current
            previous = came_from.get(current)
            if previous is not None and previous[2] == layer:
                old_dx, old_dy = x - previous[0], y - previous[1]
            else:
                old_dx, old_dy = 0, 0
            for dx, dy in self._MOVES:
                direction = self._layer_direction(layer)
                if direction == RoutingDirection.DISABLED:
                    continue
                nx, ny = x + dx, y + dy
                if not (min_ix <= nx <= max_ix and min_iy <= ny <= max_iy):
                    continue
                point = point_at(nx, ny)
                if not track_map.clear(point, layer):
                    continue
                if not self._move_clear(point_at(x, y), point, layer, track_map):
                    continue
                if dx and dy:  # Do not pass diagonally through a blocked corner.
                    if not track_map.clear(point_at(x + dx, y), layer):
                        continue
                    if not track_map.clear(point_at(x, y + dy), layer):
                        continue
                next_state = (nx, ny, layer, via_count)
                bend = (
                    self.config.bend_cost
                    if (old_dx or old_dy) and (dx, dy) != (old_dx, old_dy)
                    else 0.0
                )
                congestion = (
                    penalties.cost(point, layer) * connection.congestion_multiplier
                    if penalties is not None
                    else 0.0
                )
                new_cost = costs[current] + grid * (sqrt(2) if dx and dy else 1) + bend + congestion
                new_cost += self._direction_steps(direction, dx, dy) * effective_direction_penalty
                if new_cost < costs.get(next_state, inf):
                    accept(next_state, new_cost, current)

            point = point_at(x, y)
            existing_bridge = self._has_existing_layer_bridge(
                bridge_board or board, connection.net, point
            )
            count = via_count if existing_bridge else via_count + 1
            if (
                not (
                    previous is not None
                    and previous[:2] == current[:2]
                    and previous[2] != current[2]
                )
                and count <= self.config.max_vias_per_connection
                and (
                    existing_bridge
                    or all(
                        via_map.clear(point, candidate_layer)
                        for candidate_layer in board.copper_layers
                    )
                )
            ):
                for other in board.copper_layers:
                    if other == layer or self._layer_direction(other) == RoutingDirection.DISABLED:
                        continue
                    next_state = (x, y, other, count)
                    congestion = (
                        max(penalties.cost(point, layer), penalties.cost(point, other))
                        * connection.congestion_multiplier
                        if penalties is not None
                        else 0.0
                    )
                    new_cost = (
                        costs[current]
                        + (0.05 if existing_bridge else self.config.via_cost)
                        + congestion
                    )
                    if new_cost < costs.get(next_state, inf):
                        accept(next_state, new_cost, current)

        if goal is None:
            return None
        path = [goal]
        while path[-1] not in starts:
            path.append(came_from[path[-1]])
        path.reverse()
        return path

    def _move_clear(
        self,
        start: Point,
        end: Point,
        layer: Layer,
        clearance_map: _ClearanceMap,
    ) -> bool:
        """Check the midpoint of one grid move in addition to its endpoints."""
        return clearance_map.clear(
            Point((start.x + end.x) / 2, (start.y + end.y) / 2),
            layer,
        )

    def _materialize_path(
        self,
        board: Board,
        connection: Connection,
        path: list[State],
        bridge_board: Board | None = None,
    ) -> tuple[list[Track], list[Via]]:
        grid = self.config.grid
        nodes = [
            (
                Point(
                    connection.source.center.x + state[0] * grid,
                    connection.source.center.y + state[1] * grid,
                ),
                state[2],
            )
            for state in path
        ]
        # Always terminate at the pad centre. Bounding boxes are conservative
        # extraction geometry and their corners may lie outside the real pad.
        # Two orthogonal fan-out segments retain 45/90-degree routing.
        target_layer = nodes[-1][1]
        elbow = Point(connection.target.center.x, nodes[-1][0].y)
        if elbow != nodes[-1][0]:
            nodes.append((elbow, target_layer))
        if connection.target.center != nodes[-1][0]:
            nodes.append((connection.target.center, target_layer))

        simplified = [nodes[0]]
        for node in nodes[1:]:
            if len(simplified) < 2:
                simplified.append(node)
                continue
            a, b = simplified[-2], simplified[-1]
            same_layer = a[1] == b[1] == node[1]
            ab = (round(b[0].x - a[0].x, 9), round(b[0].y - a[0].y, 9))
            bc = (round(node[0].x - b[0].x, 9), round(node[0].y - b[0].y, 9))
            if same_layer and ab[0] * bc[1] == ab[1] * bc[0]:
                simplified[-1] = node
            else:
                simplified.append(node)

        rules = board.rules_for(connection.net)
        tracks: list[Track] = []
        vias: list[Via] = []
        for (start, layer), (end, next_layer) in itertools.pairwise(simplified):
            if layer == next_layer and start != end:
                tracks.append(Track(connection.net, start, end, layer, rules.track_width))
            elif layer != next_layer and not self._has_existing_layer_bridge(
                bridge_board or board, connection.net, start
            ):
                vias.append(Via(connection.net, start, rules.via_diameter, rules.via_drill))
        return tracks, vias
