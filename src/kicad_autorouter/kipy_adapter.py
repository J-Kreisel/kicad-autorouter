from __future__ import annotations

import json
import math
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from itertools import pairwise
from pathlib import Path
from typing import Any

from .geometry import point_in_polygon
from .model import Board, CopperArea, KeepoutArea, Layer, Pad, Point, Rules, Track, Via

NM_PER_MM = 1_000_000


class UnsupportedBoardError(RuntimeError):
    pass


def _mm(value_nm: int) -> float:
    return value_nm / NM_PER_MM


def _copper_layers(count: int) -> tuple[Layer, ...]:
    if not 2 <= count <= 32:
        raise UnsupportedBoardError(f"Unsupported copper layer count: {count}")
    return (Layer.FRONT, *(Layer(index) for index in range(1, count - 1)), Layer.BACK)


def _model_layer(raw_layer: int, active_layers: tuple[Layer, ...]) -> Layer:
    value = int(raw_layer) - 3
    try:
        layer = Layer(value)
    except ValueError as error:
        raise UnsupportedBoardError(f"Unsupported copper layer identifier: {raw_layer}") from error
    if layer not in active_layers:
        raise UnsupportedBoardError(f"Item exists on inactive copper layer {layer.label}")
    return layer


def _kipy_layer(layer: Layer) -> int:
    return int(layer) + 3


def _effective_rules(netclass: Any, board_rules: dict[str, Any], edge_clearance: float) -> Rules:
    minimum_clearance = float(board_rules.get("min_clearance", 0.0))
    minimum_connection = float(board_rules.get("min_connection", 0.0))
    minimum_via_diameter = float(board_rules.get("min_via_diameter", 0.0))
    minimum_annular_width = float(board_rules.get("min_via_annular_width", 0.0))
    minimum_drill = float(board_rules.get("min_through_hole_diameter", 0.0))
    minimum_track_width = max(
        float(board_rules.get("min_track_width", 0.0)),
        minimum_connection,
        minimum_connection + minimum_drill / 2 + (0.05 if minimum_connection else 0.0),
    )
    via_drill = max(_mm(netclass.via_drill), minimum_drill)
    via_diameter = max(
        _mm(netclass.via_diameter),
        minimum_via_diameter,
        via_drill + 2 * minimum_annular_width,
    )
    return Rules(
        clearance=max(_mm(netclass.clearance), minimum_clearance),
        track_width=max(_mm(netclass.track_width), minimum_track_width),
        via_diameter=via_diameter,
        via_drill=via_drill,
        edge_clearance=edge_clearance,
    )


def _point(vector: Any) -> Point:
    return Point(_mm(vector.x), _mm(vector.y))


def _polyline_points(polyline: Any, description: str) -> tuple[Point, ...]:
    points: list[Point] = []
    for node in polyline.nodes:
        if not node.has_point:
            raise UnsupportedBoardError(f"Arc nodes in {description} are not supported")
        points.append(_point(node.point))
    if len(points) < 3:
        raise UnsupportedBoardError(f"Invalid polygon in {description}")
    return tuple(points)


def _polyline_points_with_arcs(polyline: Any, description: str) -> tuple[Point, ...]:
    points: list[Point] = []
    for node in polyline.nodes:
        if node.has_point:
            candidates = [_point(node.point)]
        elif node.has_arc:
            candidates = _arc_points(node.arc)
        else:
            raise UnsupportedBoardError(f"Invalid node in {description}")
        for candidate in candidates:
            if not points or candidate.distance_to(points[-1]) > 1e-9:
                points.append(candidate)
    if len(points) > 1 and points[0].distance_to(points[-1]) <= 1e-9:
        points.pop()
    if len(points) < 3:
        raise UnsupportedBoardError(f"Invalid polygon in {description}")
    return tuple(points)


def _arc_points(arc: Any) -> list[Point]:
    center = _point(arc.center())
    start = _point(arc.start)
    mid = _point(arc.mid)
    end = _point(arc.end)
    start_angle = math.atan2(start.y - center.y, start.x - center.x)
    mid_angle = math.atan2(mid.y - center.y, mid.x - center.x)
    end_angle = math.atan2(end.y - center.y, end.x - center.x)
    positive_sweep = (end_angle - start_angle) % (2 * math.pi)
    mid_sweep = (mid_angle - start_angle) % (2 * math.pi)
    sweep = positive_sweep if mid_sweep <= positive_sweep + 1e-9 else positive_sweep - 2 * math.pi
    radius = start.distance_to(center)
    count = max(2, math.ceil(abs(sweep) * radius / 0.25))
    points = [
        Point(
            center.x + radius * math.cos(start_angle + sweep * index / count),
            center.y + radius * math.sin(start_angle + sweep * index / count),
        )
        for index in range(count + 1)
    ]
    points[0] = start
    points[-1] = end
    return points


def _shape_segments(shape: Any) -> list[tuple[Point, Point]]:
    from kipy.board_types import (
        BoardArc,
        BoardCircle,
        BoardPolygon,
        BoardRectangle,
        BoardSegment,
    )

    if isinstance(shape, BoardSegment):
        return [(_point(shape.start), _point(shape.end))]
    if isinstance(shape, BoardRectangle):
        top_left = _point(shape.top_left)
        bottom_right = _point(shape.bottom_right)
        top_right = Point(bottom_right.x, top_left.y)
        bottom_left = Point(top_left.x, bottom_right.y)
        points = (top_left, top_right, bottom_right, bottom_left, top_left)
        return list(pairwise(points))
    if isinstance(shape, BoardCircle):
        center = _point(shape.center)
        radius = _mm(round(shape.radius()))
        count = max(36, math.ceil(2 * math.pi * radius / 0.25))
        points = [
            Point(
                center.x + radius * math.cos(2 * math.pi * index / count),
                center.y + radius * math.sin(2 * math.pi * index / count),
            )
            for index in range(count)
        ]
        return list(zip(points, points[1:] + points[:1], strict=True))
    if isinstance(shape, BoardPolygon):
        segments: list[tuple[Point, Point]] = []
        for polygon in shape.polygons:
            for polyline in (polygon.outline, *polygon.holes):
                points = _polyline_points(polyline, "Edge.Cuts polygon")
                segments.extend(zip(points, points[1:] + points[:1], strict=True))
        return segments
    if isinstance(shape, BoardArc):
        return list(pairwise(_arc_points(shape)))
    raise UnsupportedBoardError(f"Unsupported Edge.Cuts shape: {type(shape).__name__}")


def _ordered_outlines(segments: list[tuple[Point, Point]]) -> tuple[tuple[Point, ...], ...]:
    if not segments:
        raise UnsupportedBoardError("No segment-based Edge.Cuts outline found")
    remaining = list(segments)
    outlines: list[tuple[Point, ...]] = []
    tolerance = 1e-6
    while remaining:
        start, end = remaining.pop(0)
        result = [start, end]
        while result[-1].distance_to(result[0]) > tolerance:
            tail = result[-1]
            match = next(
                (
                    (index, other if tail.distance_to(candidate) <= tolerance else candidate)
                    for index, (candidate, other) in enumerate(remaining)
                    if tail.distance_to(candidate) <= tolerance
                    or tail.distance_to(other) <= tolerance
                ),
                None,
            )
            if match is None:
                raise UnsupportedBoardError("Edge.Cuts segments do not form closed outlines")
            index, next_point = match
            remaining.pop(index)
            result.append(next_point)
        outlines.append(tuple(result[:-1]))
    return tuple(outlines)


def _ordered_outline(segments: list[tuple[Point, Point]]) -> tuple[Point, ...]:
    outlines = _ordered_outlines(segments)
    if len(outlines) != 1:
        raise UnsupportedBoardError("Expected one Edge.Cuts outline")
    return outlines[0]


def _polygon_area(polygon: tuple[Point, ...]) -> float:
    return abs(
        sum(
            point.x * polygon[(index + 1) % len(polygon)].y
            - polygon[(index + 1) % len(polygon)].x * point.y
            for index, point in enumerate(polygon)
        )
        / 2
    )


def _balanced_form(text: str, start: int) -> str:
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise UnsupportedBoardError("Malformed KiCad S-expression")


def _forms_named(text: str, name: str) -> list[str]:
    starts: list[int] = []
    quoted = False
    escaped = False
    token = f"({name}"
    for index, character in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character == "(" and text.startswith(name, index + 1):
            end = index + len(token)
            if end < len(text) and text[end].isspace():
                starts.append(index)
    return [_balanced_form(text, start) for start in starts]


def _form_uuid(form: str) -> str | None:
    match = re.search(r'\(uuid\s+"([^"]+)"\)', form)
    return match.group(1) if match else None


def _expand_selected_footprints(
    selected_groups: set[str],
    selected_footprints: set[str],
    group_paths: dict[str, tuple[str, ...]],
    pad_to_footprint: dict[str, str],
) -> set[str]:
    result = set(selected_footprints)
    result.update(
        footprint_id
        for footprint_id in set(pad_to_footprint.values())
        if selected_groups.intersection(group_paths.get(footprint_id, ()))
    )
    return result


def _board_group_paths(board_text: str) -> dict[str, tuple[str, ...]]:
    """Return nearest-to-root group ancestry for every grouped board item UUID."""
    groups: dict[str, tuple[str, ...]] = {}
    for form in _forms_named(board_text, "group"):
        group_id = _form_uuid(form)
        members_match = re.search(r"\(members(?=\s|\))", form)
        if group_id is None or members_match is None:
            continue
        members_form = _balanced_form(form, members_match.start())
        groups[group_id] = tuple(re.findall(r'"([^"]+)"', members_form))

    parent: dict[str, str] = {}
    for group, members in groups.items():
        for member in members:
            if member in parent and parent[member] != group:
                raise UnsupportedBoardError(f"Board item {member} belongs to multiple groups")
            parent[member] = group

    result: dict[str, tuple[str, ...]] = {}
    for item in parent:
        path: list[str] = []
        current = item
        while current in parent:
            current = parent[current]
            if current in path:
                raise UnsupportedBoardError("Board group hierarchy contains a cycle")
            path.append(current)
        result[item] = tuple(path)
    return result


def _distance_mm(value: str, unit: str) -> float:
    factor = {"mm": 1.0, "mil": 0.0254, "in": 25.4}.get(unit.lower())
    if factor is None:
        raise UnsupportedBoardError(f"Unsupported custom-rule unit {unit!r}")
    return float(value) * factor


def _via_pad_hole_clearance(rules_text: str) -> float:
    """Extract broad Via/Pad physical-hole-clearance rules we can evaluate exactly."""
    clearance, _unsupported = _custom_routing_rules(rules_text)
    return clearance


def _custom_routing_rules(rules_text: str) -> tuple[float, tuple[str, ...]]:
    result = 0.0
    unsupported: list[str] = []
    accepted_conditions = {
        "a.type=='via'&&b.type=='pad'",
        "a.type=='pad'&&b.type=='via'",
    }
    routing_constraints = {
        "annular_width",
        "assertion",
        "clearance",
        "connection_width",
        "diff_pair_gap",
        "diff_pair_uncoupled",
        "disallow",
        "hole_clearance",
        "hole_size",
        "hole_to_hole",
        "length",
        "physical_clearance",
        "physical_hole_clearance",
        "skew",
        "track_angle",
        "track_segment_length",
        "track_width",
        "via_diameter",
    }
    for match in re.finditer(r"\(rule(?=\s)", rules_text):
        form = _balanced_form(rules_text, match.start())
        name_match = re.match(r'\(rule\s+"([^"]+)"', form)
        name = name_match.group(1) if name_match else "unnamed custom rule"
        constraints = {
            constraint.lower() for constraint in re.findall(r"\(constraint\s+([a-zA-Z_]+)", form)
        }
        constraint = re.search(
            r"\(constraint\s+physical_hole_clearance\s+"
            r"\(min\s+([0-9]+(?:\.[0-9]+)?)\s*(mm|mil|in)\)\s*\)",
            form,
            re.IGNORECASE,
        )
        condition = re.search(r'\(condition\s+"([^"]+)"\)', form, re.IGNORECASE)
        supported = False
        if constraint is not None and condition is not None:
            normalized = re.sub(r"\s+", "", condition.group(1).lower())
            while normalized.startswith("(") and normalized.endswith(")"):
                normalized = normalized[1:-1]
            supported = normalized in accepted_conditions
        if supported:
            result = max(result, _distance_mm(constraint.group(1), constraint.group(2)))
            constraints.discard("physical_hole_clearance")
        if constraints & routing_constraints:
            unsupported.append(name)
    return result, tuple(unsupported)


class KiPyAdapter:
    """Translate between KiPy objects and the pure routing model."""

    def __init__(self, board: Any):
        self.board = board
        self._kipy_nets: dict[str, Any] = {}
        self._pad_to_footprint: dict[str, str] = {}
        self._pads_by_symbol_pin: dict[tuple[str, str], set[str]] = {}
        self._group_paths: dict[str, tuple[str, ...]] = {}
        self._original_selection_ids: tuple[str, ...] = ()

    @classmethod
    def connect(cls) -> KiPyAdapter:
        try:
            from kipy import KiCad
        except ImportError as error:  # pragma: no cover - depends on KiCad environment
            raise RuntimeError("KiPy is not installed; enter the Nix development shell") from error
        try:
            client = KiCad()
            board = client.get_board()
        except Exception as error:  # KiPy uses its own connection exception hierarchy.
            raise RuntimeError(f"Could not connect to KiCad: {error}") from error
        if board is None:
            raise RuntimeError("No PCB is open in KiCad")
        return cls(board)

    def _project_board_rules(self) -> dict[str, Any]:
        try:
            project = self.board.document.project
            path = Path(project.path) / f"{project.name}.kicad_pro"
            data = json.loads(path.read_text())
            return data["board"]["design_settings"]["rules"]
        except (AttributeError, KeyError, OSError, TypeError, json.JSONDecodeError):
            return {}

    def copper_layers(self) -> tuple[Layer, ...]:
        """Return the active copper stack without performing full board extraction."""
        return _copper_layers(self.board.get_copper_layer_count())

    def _project_custom_rules(self) -> str:
        try:
            project = self.board.document.project
            return (Path(project.path) / f"{project.name}.kicad_dru").read_text()
        except (AttributeError, OSError, TypeError):
            return ""

    def extract(
        self,
        edge_clearance: float | None = None,
        min_hole_to_hole: float | None = None,
    ) -> Board:
        from kipy.board_types import ArcTrack, BoardLayer, PadType, ViaType

        copper_layers = self.copper_layers()
        edge_shapes = [
            shape for shape in self.board.get_shapes() if shape.layer == BoardLayer.BL_Edge_Cuts
        ]
        footprint_edge_shapes = [
            shape
            for footprint in self.board.get_footprints()
            for shape in footprint.definition.shapes
            if shape.layer == BoardLayer.BL_Edge_Cuts
        ]
        edge_segments = [
            segment
            for shape in (*edge_shapes, *footprint_edge_shapes)
            for segment in _shape_segments(shape)
        ]
        outlines = _ordered_outlines(edge_segments)
        outline = max(outlines, key=_polygon_area)
        cutouts = tuple(candidate for candidate in outlines if candidate is not outline)
        if any(not point_in_polygon(cutout[0], outline) for cutout in cutouts):
            raise UnsupportedBoardError("Multiple disjoint board outlines are not supported")

        kipy_nets = list(self.board.get_nets())
        self._kipy_nets = {net.name: net for net in kipy_nets}
        classes = self.board.get_netclass_for_nets(kipy_nets)
        board_rules = self._project_board_rules()
        via_pad_hole_clearance, unsupported_rules = _custom_routing_rules(
            self._project_custom_rules()
        )
        if unsupported_rules:
            names = ", ".join(repr(name) for name in unsupported_rules)
            raise UnsupportedBoardError(f"Unsupported applicable custom DRC rules: {names}")
        if edge_clearance is None:
            edge_clearance = float(board_rules.get("min_copper_edge_clearance", 0.5))
        if min_hole_to_hole is None:
            min_hole_to_hole = float(board_rules.get("min_hole_to_hole", 0.25))
        rules_by_net: dict[str, Rules] = {}
        for name in self._kipy_nets:
            netclass = classes.get(name)
            if netclass is None:
                if name:
                    raise UnsupportedBoardError(f"No netclass rules found for net {name!r}")
                continue
            if None in (
                netclass.clearance,
                netclass.track_width,
                netclass.via_diameter,
                netclass.via_drill,
            ):
                raise UnsupportedBoardError(f"Net {name!r} has incomplete routing rules")
            rules_by_net[name] = _effective_rules(netclass, board_rules, edge_clearance)

        footprints = list(self.board.get_footprints())
        pad_metadata: dict[str, tuple[str, str, str]] = {}
        for footprint in footprints:
            footprint_id = footprint.id.value
            reference = footprint.reference_field.text.value
            for pad in footprint.definition.pads:
                pad_metadata[pad.id.value] = (footprint_id, reference, pad.number)
        self._pad_to_footprint = {
            pad_id: footprint_id
            for pad_id, (footprint_id, _reference, _number) in pad_metadata.items()
        }
        group_paths = _board_group_paths(self.board.get_as_string())
        self._group_paths = group_paths

        pads: list[Pad] = []
        self._pads_by_symbol_pin = {}
        for pad in self.board.get_pads():
            box = self.board.get_item_bounding_box(pad)
            if box is None:
                raise UnsupportedBoardError(f"Could not determine bounds of pad {pad.number}")
            raw_layers = set(pad.padstack.layers)
            if pad.pad_type in (PadType.PT_PTH, PadType.PT_NPTH):
                layers = set(copper_layers)
            else:
                layers = {
                    _model_layer(raw_layer, copper_layers)
                    for raw_layer in raw_layers
                    if int(BoardLayer.BL_F_Cu) <= int(raw_layer) <= int(BoardLayer.BL_B_Cu)
                }
                if not layers:
                    raise UnsupportedBoardError(
                        f"Could not determine copper layers of pad {pad.number}"
                    )
            pad_id = pad.id.value
            footprint_id, reference, number = pad_metadata.get(pad_id, ("", "", pad.number))
            group_path = group_paths.get(pad_id, group_paths.get(footprint_id, ()))
            pads.append(
                Pad(
                    id=pad_id,
                    net=pad.net.name,
                    center=_point(pad.position),
                    width=_mm(box.size.x),
                    height=_mm(box.size.y),
                    layers=frozenset(layers),
                    drill=_mm(
                        max(
                            pad.padstack.drill.diameter.x,
                            pad.padstack.drill.diameter.y,
                        )
                    ),
                    component=reference,
                    number=number,
                    group_path=group_path,
                )
            )
            if reference and number:
                self._pads_by_symbol_pin.setdefault((reference, number), set()).add(pad_id)

        tracks: list[Track] = []
        for track in self.board.get_tracks():
            layer = _model_layer(track.layer, copper_layers)
            if isinstance(track, ArcTrack):
                tracks.extend(
                    Track(track.net.name, start, end, layer, _mm(track.width))
                    for start, end in pairwise(_arc_points(track))
                )
            else:
                tracks.append(
                    Track(
                        track.net.name,
                        _point(track.start),
                        _point(track.end),
                        layer,
                        _mm(track.width),
                    )
                )

        vias = []
        for via in self.board.get_vias():
            if via.type != ViaType.VT_THROUGH:
                raise UnsupportedBoardError("Blind, buried, and microvias are not supported")
            vias.append(
                Via(via.net.name, _point(via.position), _mm(via.diameter), _mm(via.drill_diameter))
            )
        copper_areas: list[CopperArea] = []
        keepouts: list[KeepoutArea] = []
        for zone in self.board.get_zones():
            if zone.is_rule_area():
                settings = zone.proto.rule_area_settings
                layers = frozenset(
                    _model_layer(raw_layer, copper_layers)
                    for raw_layer in zone.layers
                    if int(BoardLayer.BL_F_Cu) <= int(raw_layer) <= int(BoardLayer.BL_B_Cu)
                )
                if layers and (settings.keepout_tracks or settings.keepout_vias):
                    polygon = zone.outline
                    keepouts.append(
                        KeepoutArea(
                            layers=layers,
                            outline=_polyline_points_with_arcs(
                                polygon.outline, "rule-area outline"
                            ),
                            holes=tuple(
                                _polyline_points_with_arcs(hole, "rule-area hole")
                                for hole in polygon.holes
                            ),
                            blocks_tracks=settings.keepout_tracks,
                            blocks_vias=settings.keepout_vias,
                        )
                    )
                # Placement and copper-pour restrictions do not constrain tracks or vias.
                continue
            if zone.net is None or not zone.net.name:
                raise UnsupportedBoardError("Copper zone has no net")
            if not zone.filled_polygons:
                raise UnsupportedBoardError(
                    f"Zone on net {zone.net.name!r} is not filled; refill zones before routing"
                )
            for raw_layer, polygons in zone.filled_polygons.items():
                layer = _model_layer(raw_layer, copper_layers)
                for polygon in polygons:
                    copper_areas.append(
                        CopperArea(
                            net=zone.net.name,
                            layer=layer,
                            outline=_polyline_points(polygon.outline, "zone outline"),
                            holes=tuple(
                                _polyline_points(hole, "zone hole") for hole in polygon.holes
                            ),
                            clearance=_mm(zone.clearance),
                        )
                    )
        return Board(
            outline,
            pads,
            tracks,
            vias,
            rules_by_net,
            cutouts=cutouts,
            copper_areas=copper_areas,
            min_hole_to_hole=min_hole_to_hole,
            keepouts=keepouts,
            via_pad_hole_clearance=via_pad_hole_clearance,
            copper_layers=copper_layers,
        )

    def selected_pad_ids(self) -> set[str]:
        """Expand the current pad/footprint selection into routing-terminal pad UUIDs."""
        from kipy.proto.common.types.enums_pb2 import KiCadObjectType

        pad_items = list(self.board.get_selection(KiCadObjectType.KOT_PCB_PAD))
        footprint_items = list(self.board.get_selection(KiCadObjectType.KOT_PCB_FOOTPRINT))
        selection_text = self.board.get_selection_as_string()
        selected_groups = {
            group_id
            for form in _forms_named(selection_text, "group")
            if (group_id := _form_uuid(form)) is not None
        }
        serialized_footprints = {
            footprint_id
            for form in _forms_named(selection_text, "footprint")
            if (footprint_id := _form_uuid(form)) is not None
        }
        self._original_selection_ids = tuple(
            dict.fromkeys(
                [
                    *(item.id.value for item in pad_items),
                    *(item.id.value for item in footprint_items),
                    *selected_groups,
                ]
            )
        )
        selected_pads = {item.id.value for item in pad_items}
        selected_footprints = _expand_selected_footprints(
            selected_groups,
            {
                *(item.id.value for item in footprint_items),
                *serialized_footprints,
            },
            self._group_paths,
            self._pad_to_footprint,
        )
        selected_pads.update(
            pad_id
            for pad_id, footprint_id in self._pad_to_footprint.items()
            if footprint_id in selected_footprints
        )
        return selected_pads

    def direct_schematic_pairs(self) -> set[frozenset[str]]:
        """Find unlabeled two-pin schematic nets, the conservative direct-wire case."""
        project = self.board.document.project
        schematic = Path(project.path) / f"{project.name}.kicad_sch"
        if not schematic.is_file():
            raise UnsupportedBoardError(f"Project schematic not found: {schematic}")

        with tempfile.NamedTemporaryFile(
            prefix="autorouter-netlist-",
            suffix=".xml",
            delete=False,
        ) as output:
            output_path = Path(output.name)
        try:
            process = subprocess.run(
                [
                    "kicad-cli",
                    "sch",
                    "export",
                    "netlist",
                    "--format",
                    "kicadxml",
                    "--output",
                    str(output_path),
                    str(schematic),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if process.returncode:
                detail = process.stderr.strip() or process.stdout.strip()
                raise UnsupportedBoardError(f"Could not inspect schematic: {detail}")
            root = ET.parse(output_path).getroot()
        except FileNotFoundError as error:
            raise UnsupportedBoardError("kicad-cli is required for schematic priority") from error
        except ET.ParseError as error:
            raise UnsupportedBoardError(f"Invalid exported schematic netlist: {error}") from error
        finally:
            output_path.unlink(missing_ok=True)

        result: set[frozenset[str]] = set()
        for net in root.findall("./nets/net"):
            # KiCad assigns Net-(...-Pad...) names only when no label names the net.
            if not net.get("name", "").startswith("Net-("):
                continue
            nodes = list(net.findall("node"))
            if len(nodes) != 2:
                continue
            pad_sets = [
                self._pads_by_symbol_pin.get((node.get("ref", ""), node.get("pin", "")), set())
                for node in nodes
            ]
            if all(len(pads) == 1 for pads in pad_sets):
                pad_ids = [next(iter(pads)) for pads in pad_sets]
            else:
                continue
            if pad_ids[0] != pad_ids[1]:
                result.add(frozenset(pad_ids))
        return result

    def unroute(
        self,
        model: Board,
        excluded_nets: set[str] | None = None,
        selected_pad_ids: set[str] | None = None,
    ) -> tuple[set[str], int, int]:
        """Atomically remove tracks and vias on nets in the requested selection scope."""
        nets, tracks, vias = self.unroute_scope(model, excluded_nets, selected_pad_ids)
        items = [*tracks, *vias]
        if not items:
            return nets, 0, 0

        commit = self.board.begin_commit()
        try:
            self.board.remove_items(items)
            self.board.push_commit(commit, "Unroute selected nets")
        except Exception:
            self.board.drop_commit(commit)
            raise
        return nets, len(tracks), len(vias)

    def unroute_scope(
        self,
        model: Board,
        excluded_nets: set[str] | None = None,
        selected_pad_ids: set[str] | None = None,
    ) -> tuple[set[str], list[Any], list[Any]]:
        nets = {
            pad.net
            for pad in model.pads
            if pad.net and (selected_pad_ids is None or pad.id in selected_pad_ids)
        }
        if excluded_nets:
            nets.difference_update(excluded_nets)
        tracks = [track for track in self.board.get_tracks() if track.net.name in nets]
        vias = [via for via in self.board.get_vias() if via.net.name in nets]
        return nets, tracks, vias

    def apply(self, tracks: list[Track], vias: list[Via]) -> None:
        from kipy import geometry
        from kipy.board_types import Track as KiTrack
        from kipy.board_types import Via as KiVia
        from kipy.board_types import ViaType

        items: list[Any] = []
        groups: list[str | None] = []
        for route in tracks:
            item = KiTrack()
            item.start = geometry.Vector2.from_xy(
                round(route.start.x * NM_PER_MM), round(route.start.y * NM_PER_MM)
            )
            item.end = geometry.Vector2.from_xy(
                round(route.end.x * NM_PER_MM), round(route.end.y * NM_PER_MM)
            )
            item.layer = _kipy_layer(route.layer)
            item.width = round(route.width * NM_PER_MM)
            item.net = self._kipy_nets[route.net]
            items.append(item)
            groups.append(route.group)
        for route in vias:
            item = KiVia()
            item.position = geometry.Vector2.from_xy(
                round(route.position.x * NM_PER_MM), round(route.position.y * NM_PER_MM)
            )
            item.diameter = round(route.diameter * NM_PER_MM)
            item.drill_diameter = round(route.drill * NM_PER_MM)
            item.net = self._kipy_nets[route.net]
            item.type = ViaType.VT_THROUGH
            items.append(item)
            groups.append(route.group)

        commit = self.board.begin_commit()
        try:
            created = self.board.create_items(items)
            self.board.push_commit(commit, "Autoroute unrouted connections")
        except Exception:
            self.board.drop_commit(commit)
            raise

        grouped: dict[str, list[Any]] = {}
        if any(group is not None for group in groups):
            if created is None:
                raise RuntimeError("KiCad did not return the created routing items")
            for item, group in zip(created, groups, strict=True):
                if group is not None:
                    grouped.setdefault(group, []).append(item)
        for group, group_items in grouped.items():
            self._add_items_to_group(group, group_items)
        if grouped and self._original_selection_ids:
            self._select_ids(self._original_selection_ids)

    def _add_items_to_group(self, group_id: str, items: list[Any]) -> None:
        """Use KiCad's group tool because KiCad 10 does not expose groups as KiPy wrappers."""
        from kipy.proto.common.commands import editor_commands_pb2

        self.board.clear_selection()
        self._select_ids((group_id, *(item.id.value for item in items)))

        action = editor_commands_pb2.RunAction(action="common.Interactive.addToGroup")
        response = self.board.client.send(action, editor_commands_pb2.RunActionResponse)
        if response.status != editor_commands_pb2.RAS_OK:
            raise RuntimeError(f"KiCad could not add routed copper to group {group_id}")
        self.board.clear_selection()

    def _select_ids(self, item_ids: tuple[str, ...]) -> None:
        from kipy.proto.common.commands import editor_commands_pb2

        select = editor_commands_pb2.AddToSelection()
        select.header.document.CopyFrom(self.board.document)
        for item_id in item_ids:
            select.items.add(value=item_id)
        self.board.client.send(select, editor_commands_pb2.SelectionResponse)
