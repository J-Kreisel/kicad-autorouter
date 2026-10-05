from types import SimpleNamespace

import pytest

from kicad_autorouter.kipy_adapter import (
    UnsupportedBoardError,
    _board_group_paths,
    _copper_layers,
    _custom_routing_rules,
    _effective_rules,
    _expand_selected_footprints,
    _model_layer,
    _ordered_outline,
    _ordered_outlines,
    _via_pad_hole_clearance,
)
from kicad_autorouter.model import Layer, Point


def test_orders_shuffled_outline_segments():
    segments = [
        (Point(4, 0), Point(4, 3)),
        (Point(0, 3), Point(0, 0)),
        (Point(4, 3), Point(0, 3)),
        (Point(0, 0), Point(4, 0)),
    ]
    outline = _ordered_outline(segments)
    assert len(outline) == 4
    assert set(outline) == {Point(0, 0), Point(4, 0), Point(4, 3), Point(0, 3)}


def test_rejects_open_outline():
    with pytest.raises(UnsupportedBoardError):
        _ordered_outline([(Point(0, 0), Point(1, 0))])


def test_orders_outer_outline_and_cutout():
    segments = [
        (Point(0, 0), Point(10, 0)),
        (Point(10, 0), Point(10, 10)),
        (Point(10, 10), Point(0, 10)),
        (Point(0, 10), Point(0, 0)),
        (Point(4, 4), Point(6, 4)),
        (Point(6, 4), Point(6, 6)),
        (Point(6, 6), Point(4, 6)),
        (Point(4, 6), Point(4, 4)),
    ]
    assert len(_ordered_outlines(segments)) == 2


def test_global_board_minima_override_looser_netclass_rules():
    netclass = SimpleNamespace(
        clearance=200_000,
        track_width=200_000,
        via_diameter=600_000,
        via_drill=300_000,
    )
    rules = _effective_rules(
        netclass,
        {
            "min_clearance": 0.3,
            "min_connection": 0.3,
            "min_track_width": 0.3,
            "min_via_diameter": 1.5,
            "min_via_annular_width": 0.3,
            "min_through_hole_diameter": 0.3,
        },
        edge_clearance=0.5,
    )

    assert rules.clearance == 0.3
    assert rules.track_width == pytest.approx(0.5)
    assert rules.via_diameter == 1.5
    assert rules.via_drill == 0.3


def test_board_group_paths_find_closest_parent_first():
    board = """
    (kicad_pcb
      (group "child" (uuid "child-id") (members "footprint-id"))
      (group "parent" (uuid "parent-id") (members "child-id"))
    )
    """

    assert _board_group_paths(board)["footprint-id"] == ("child-id", "parent-id")


def test_selected_parent_group_expands_nested_component_members():
    selected = _expand_selected_footprints(
        {"parent-id"},
        {"direct-footprint"},
        {
            "nested-footprint": ("child-id", "parent-id"),
            "other-footprint": ("other-group",),
        },
        {
            "pad-a": "nested-footprint",
            "pad-b": "direct-footprint",
            "pad-c": "other-footprint",
        },
    )

    assert selected == {"direct-footprint", "nested-footprint"}


def test_extracts_broad_via_pad_physical_hole_clearance_rule():
    rules = """
    (version 1)
    (rule "Avoid via solder wicking"
      (condition "A.Type == 'Via' && B.Type == 'Pad'")
      (constraint physical_hole_clearance (min 20mil)))
    """

    assert _via_pad_hole_clearance(rules) == pytest.approx(0.508)


def test_maps_four_layer_stack_to_model_layers():
    layers = _copper_layers(4)

    assert layers == (Layer.FRONT, Layer.INNER_1, Layer.INNER_2, Layer.BACK)
    assert _model_layer(3, layers) == Layer.FRONT
    assert _model_layer(4, layers) == Layer.INNER_1
    assert _model_layer(5, layers) == Layer.INNER_2
    assert _model_layer(34, layers) == Layer.BACK


def test_reports_unsupported_routing_custom_rule():
    rules = """
    (version 1)
    (rule "Special track clearance"
      (condition "A.Type == 'Track' && B.Type == 'Pad'")
      (constraint clearance (min 0.4mm)))
    """

    clearance, unsupported = _custom_routing_rules(rules)

    assert clearance == 0
    assert unsupported == ("Special track clearance",)
