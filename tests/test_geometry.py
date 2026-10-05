from kicad_autorouter.geometry import (
    distance_point_to_segment,
    point_in_copper_area,
    point_in_polygon,
    segment_distance,
)
from kicad_autorouter.model import CopperArea, Layer, Point

SQUARE = (Point(0, 0), Point(10, 0), Point(10, 10), Point(0, 10))


def test_point_in_polygon():
    assert point_in_polygon(Point(5, 5), SQUARE)
    assert not point_in_polygon(Point(11, 5), SQUARE)


def test_segment_distances():
    assert distance_point_to_segment(Point(1, 1), Point(0, 0), Point(2, 0)) == 1
    assert segment_distance(Point(0, 0), Point(2, 2), Point(0, 2), Point(2, 0)) == 0


def test_copper_area_holes_are_not_copper():
    area = CopperArea(
        "GND",
        Layer.FRONT,
        SQUARE,
        ((Point(4, 4), Point(6, 4), Point(6, 6), Point(4, 6)),),
    )
    assert point_in_copper_area(Point(2, 2), area)
    assert not point_in_copper_area(Point(5, 5), area)
