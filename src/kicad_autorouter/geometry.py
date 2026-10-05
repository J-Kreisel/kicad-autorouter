from __future__ import annotations

from itertools import pairwise
from math import floor, hypot

from .model import CopperArea, Point


def point_in_polygon(point: Point, polygon: tuple[Point, ...]) -> bool:
    inside = False
    j = len(polygon) - 1
    for i, current in enumerate(polygon):
        previous = polygon[j]
        crosses = (current.y > point.y) != (previous.y > point.y)
        if crosses:
            x_cross = (previous.x - current.x) * (point.y - current.y) / (
                previous.y - current.y
            ) + current.x
            if point.x < x_cross:
                inside = not inside
        j = i
    return inside


def distance_point_to_segment(point: Point, start: Point, end: Point) -> float:
    dx = end.x - start.x
    dy = end.y - start.y
    if dx == 0 and dy == 0:
        return point.distance_to(start)
    t = max(
        0.0, min(1.0, ((point.x - start.x) * dx + (point.y - start.y) * dy) / (dx * dx + dy * dy))
    )
    projection = Point(start.x + t * dx, start.y + t * dy)
    return point.distance_to(projection)


def distance_to_polygon_edge(point: Point, polygon: tuple[Point, ...]) -> float:
    return min(
        distance_point_to_segment(point, polygon[i], polygon[(i + 1) % len(polygon)])
        for i in range(len(polygon))
    )


def segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    def orientation(p: Point, q: Point, r: Point) -> float:
        return (q.y - p.y) * (r.x - q.x) - (q.x - p.x) * (r.y - q.y)

    o1, o2 = orientation(a, b, c), orientation(a, b, d)
    o3, o4 = orientation(c, d, a), orientation(c, d, b)
    return o1 * o2 < 0 and o3 * o4 < 0


def segment_distance(a: Point, b: Point, c: Point, d: Point) -> float:
    if segments_intersect(a, b, c, d):
        return 0.0
    return min(
        distance_point_to_segment(a, c, d),
        distance_point_to_segment(b, c, d),
        distance_point_to_segment(c, a, b),
        distance_point_to_segment(d, a, b),
    )


def distance_point_to_rectangle(point: Point, center: Point, width: float, height: float) -> float:
    dx = max(abs(point.x - center.x) - width / 2, 0.0)
    dy = max(abs(point.y - center.y) - height / 2, 0.0)
    return hypot(dx, dy)


def distance_segment_to_rectangle(
    start: Point, end: Point, center: Point, width: float, height: float
) -> float:
    left, right = center.x - width / 2, center.x + width / 2
    top, bottom = center.y - height / 2, center.y + height / 2
    corners = (Point(left, top), Point(right, top), Point(right, bottom), Point(left, bottom))
    if (
        distance_point_to_rectangle(start, center, width, height) == 0
        or distance_point_to_rectangle(end, center, width, height) == 0
    ):
        return 0.0
    if any(segments_intersect(start, end, a, b) for a, b in pairwise((*corners, corners[0]))):
        return 0.0
    return min(
        distance_point_to_rectangle(start, center, width, height),
        distance_point_to_rectangle(end, center, width, height),
        *(distance_point_to_segment(corner, start, end) for corner in corners),
    )


def point_in_copper_area(point: Point, area: CopperArea) -> bool:
    return point_in_polygon(point, area.outline) and not any(
        point_in_polygon(point, hole) for hole in area.holes
    )


def segment_touches_copper_area(start: Point, end: Point, area: CopperArea) -> bool:
    if point_in_copper_area(start, area) or point_in_copper_area(end, area):
        return True
    loops = (area.outline, *area.holes)
    return any(
        segments_intersect(start, end, loop[index], loop[(index + 1) % len(loop)])
        for loop in loops
        for index in range(len(loop))
    )


def rectangle_touches_copper_area(
    center: Point, width: float, height: float, area: CopperArea
) -> bool:
    if point_in_copper_area(center, area):
        return True
    left, right = center.x - width / 2, center.x + width / 2
    top, bottom = center.y - height / 2, center.y + height / 2
    corners = (Point(left, top), Point(right, top), Point(right, bottom), Point(left, bottom))
    if any(point_in_copper_area(corner, area) for corner in corners):
        return True
    return any(
        distance_segment_to_rectangle(
            loop[index], loop[(index + 1) % len(loop)], center, width, height
        )
        == 0
        for loop in (area.outline, *area.holes)
        for index in range(len(loop))
    )


class PreparedCopperArea:
    """Spatially indexed copper polygon for repeated connectivity queries."""

    def __init__(self, area: CopperArea, cell_size: float = 2.0):
        self.area = area
        self.cell_size = cell_size
        self._loops = tuple(self._prepare_loop(loop) for loop in (area.outline, *area.holes))

    def _prepare_loop(
        self, loop: tuple[Point, ...]
    ) -> tuple[
        dict[int, list[tuple[Point, Point]]],
        dict[tuple[int, int], list[tuple[Point, Point]]],
    ]:
        by_y: dict[int, list[tuple[Point, Point]]] = {}
        cells: dict[tuple[int, int], list[tuple[Point, Point]]] = {}
        for index, start in enumerate(loop):
            end = loop[(index + 1) % len(loop)]
            edge = (start, end)
            for y in range(
                floor(min(start.y, end.y) / self.cell_size),
                floor(max(start.y, end.y) / self.cell_size) + 1,
            ):
                by_y.setdefault(y, []).append(edge)
            for x in range(
                floor(min(start.x, end.x) / self.cell_size),
                floor(max(start.x, end.x) / self.cell_size) + 1,
            ):
                for y in range(
                    floor(min(start.y, end.y) / self.cell_size),
                    floor(max(start.y, end.y) / self.cell_size) + 1,
                ):
                    cells.setdefault((x, y), []).append(edge)
        return by_y, cells

    def _inside_loop(self, point: Point, loop_index: int) -> bool:
        inside = False
        by_y, _ = self._loops[loop_index]
        for start, end in by_y.get(floor(point.y / self.cell_size), ()):
            if (start.y > point.y) != (end.y > point.y):
                x_cross = (end.x - start.x) * (point.y - start.y) / (end.y - start.y) + start.x
                if point.x < x_cross:
                    inside = not inside
        return inside

    def contains(self, point: Point) -> bool:
        return self._inside_loop(point, 0) and not any(
            self._inside_loop(point, index) for index in range(1, len(self._loops))
        )

    def segment_touches(self, start: Point, end: Point) -> bool:
        if self.contains(start) or self.contains(end):
            return True
        min_x = floor(min(start.x, end.x) / self.cell_size)
        max_x = floor(max(start.x, end.x) / self.cell_size)
        min_y = floor(min(start.y, end.y) / self.cell_size)
        max_y = floor(max(start.y, end.y) / self.cell_size)
        seen: set[tuple[Point, Point]] = set()
        for _, cells in self._loops:
            for x in range(min_x, max_x + 1):
                for y in range(min_y, max_y + 1):
                    for edge in cells.get((x, y), ()):
                        if edge not in seen:
                            seen.add(edge)
                            if segment_distance(start, end, *edge) == 0:
                                return True
        return False

    def rectangle_touches(self, center: Point, width: float, height: float) -> bool:
        left, right = center.x - width / 2, center.x + width / 2
        top, bottom = center.y - height / 2, center.y + height / 2
        corners = (Point(left, top), Point(right, top), Point(right, bottom), Point(left, bottom))
        if self.contains(center) or any(self.contains(corner) for corner in corners):
            return True
        if any(self.segment_touches(a, b) for a, b in pairwise((*corners, corners[0]))):
            return True
        return any(
            left <= point.x <= right and top <= point.y <= bottom for point in self.area.outline[:1]
        )


def polyline_length(points: list[Point]) -> float:
    return sum(a.distance_to(b) for a, b in pairwise(points))
