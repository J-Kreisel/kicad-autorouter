from kicad_autorouter.connectivity import unrouted_connections
from kicad_autorouter.model import Board, CopperArea, Layer, Pad, Point, Track


def pad(name: str, x: float) -> Pad:
    return Pad(name, "N", Point(x, 2), 1, 1, frozenset({Layer.FRONT}))


def test_existing_track_chain_counts_as_connected():
    board = Board(
        outline=(Point(0, 0), Point(10, 0), Point(10, 4), Point(0, 4)),
        pads=[pad("A", 1), pad("B", 9)],
        tracks=[
            Track("N", Point(1, 2), Point(5, 2), Layer.FRONT, 0.2),
            Track("N", Point(5, 2), Point(9, 2), Layer.FRONT, 0.2),
        ],
    )
    assert unrouted_connections(board) == []


def test_three_pads_need_two_connections():
    board = Board(
        outline=(Point(0, 0), Point(12, 0), Point(12, 4), Point(0, 4)),
        pads=[pad("A", 1), pad("B", 6), pad("C", 11)],
    )
    assert len(unrouted_connections(board)) == 2


def test_selection_only_connects_selected_terminals():
    pads = [pad("A", 1), pad("B", 6), pad("C", 11)]
    board = Board(
        outline=(Point(0, 0), Point(12, 0), Point(12, 4), Point(0, 4)),
        pads=pads,
    )

    connections = unrouted_connections(board, selected_pad_ids={"A", "C"})

    assert len(connections) == 1
    assert {connections[0].source.id, connections[0].target.id} == {"A", "C"}


def test_priority_pair_receives_congestion_multiplier():
    pads = [pad("A", 1), pad("B", 9)]
    board = Board(
        outline=(Point(0, 0), Point(10, 0), Point(10, 4), Point(0, 4)),
        pads=pads,
    )

    connections = unrouted_connections(board, priority_pairs={frozenset(("A", "B"))})

    assert connections[0].congestion_multiplier == 0.8


def test_excluded_net_is_not_in_connection_graph():
    board = Board(
        outline=(Point(0, 0), Point(10, 0), Point(10, 4), Point(0, 4)),
        pads=[pad("A", 1), pad("B", 9)],
    )

    assert unrouted_connections(board, excluded_nets={"N"}) == []


def test_filled_same_net_area_connects_pads():
    area = CopperArea(
        "N",
        Layer.FRONT,
        (Point(0.5, 1), Point(9.5, 1), Point(9.5, 3), Point(0.5, 3)),
    )
    board = Board(
        outline=(Point(0, 0), Point(10, 0), Point(10, 4), Point(0, 4)),
        pads=[pad("A", 1), pad("B", 9)],
        copper_areas=[area],
    )
    assert unrouted_connections(board) == []
