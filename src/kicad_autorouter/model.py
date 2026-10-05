from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from math import hypot


class Layer(IntEnum):
    FRONT = 0
    INNER_1 = 1
    INNER_2 = 2
    INNER_3 = 3
    INNER_4 = 4
    INNER_5 = 5
    INNER_6 = 6
    INNER_7 = 7
    INNER_8 = 8
    INNER_9 = 9
    INNER_10 = 10
    INNER_11 = 11
    INNER_12 = 12
    INNER_13 = 13
    INNER_14 = 14
    INNER_15 = 15
    INNER_16 = 16
    INNER_17 = 17
    INNER_18 = 18
    INNER_19 = 19
    INNER_20 = 20
    INNER_21 = 21
    INNER_22 = 22
    INNER_23 = 23
    INNER_24 = 24
    INNER_25 = 25
    INNER_26 = 26
    INNER_27 = 27
    INNER_28 = 28
    INNER_29 = 29
    INNER_30 = 30
    BACK = 31

    @property
    def label(self) -> str:
        if self == Layer.FRONT:
            return "F.Cu"
        if self == Layer.BACK:
            return "B.Cu"
        return f"In{int(self)}.Cu"


class RoutingDirection(IntEnum):
    ANY = 0
    HORIZONTAL = 1
    VERTICAL = 2
    DIAGONAL_UP = 3
    DIAGONAL_DOWN = 4
    DISABLED = 5


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def distance_to(self, other: Point) -> float:
        return hypot(self.x - other.x, self.y - other.y)


@dataclass(frozen=True, slots=True)
class Rules:
    clearance: float = 0.2
    track_width: float = 0.2
    via_diameter: float = 0.6
    via_drill: float = 0.3
    edge_clearance: float = 0.5


@dataclass(frozen=True, slots=True)
class Pad:
    id: str
    net: str
    center: Point
    width: float
    height: float
    layers: frozenset[Layer]
    drill: float = 0.0
    component: str = ""
    number: str = ""
    group_path: tuple[str, ...] = ()

    def contains(self, point: Point, margin: float = 0.0) -> bool:
        return (
            abs(point.x - self.center.x) <= self.width / 2 + margin
            and abs(point.y - self.center.y) <= self.height / 2 + margin
        )


@dataclass(frozen=True, slots=True)
class Track:
    net: str
    start: Point
    end: Point
    layer: Layer
    width: float
    group: str | None = None


@dataclass(frozen=True, slots=True)
class Via:
    net: str
    position: Point
    diameter: float
    drill: float
    group: str | None = None


@dataclass(frozen=True, slots=True)
class CopperArea:
    net: str
    layer: Layer
    outline: tuple[Point, ...]
    holes: tuple[tuple[Point, ...], ...] = ()
    clearance: float = 0.0

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        xs = [point.x for point in self.outline]
        ys = [point.y for point in self.outline]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass(frozen=True, slots=True)
class KeepoutArea:
    layers: frozenset[Layer]
    outline: tuple[Point, ...]
    holes: tuple[tuple[Point, ...], ...] = ()
    blocks_tracks: bool = False
    blocks_vias: bool = False

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        xs = [point.x for point in self.outline]
        ys = [point.y for point in self.outline]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass(slots=True)
class Board:
    outline: tuple[Point, ...]
    pads: list[Pad] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    rules_by_net: dict[str, Rules] = field(default_factory=dict)
    default_rules: Rules = field(default_factory=Rules)
    cutouts: tuple[tuple[Point, ...], ...] = ()
    copper_areas: list[CopperArea] = field(default_factory=list)
    min_hole_to_hole: float = 0.25
    keepouts: list[KeepoutArea] = field(default_factory=list)
    via_pad_hole_clearance: float = 0.0
    copper_layers: tuple[Layer, ...] = (Layer.FRONT, Layer.BACK)

    def rules_for(self, net: str) -> Rules:
        return self.rules_by_net.get(net, self.default_rules)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        if not self.outline:
            raise ValueError("Board outline is required")
        xs = [point.x for point in self.outline]
        ys = [point.y for point in self.outline]
        return min(xs), min(ys), max(xs), max(ys)
