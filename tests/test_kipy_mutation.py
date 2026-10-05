from types import SimpleNamespace

import pytest

kipy = pytest.importorskip("kipy")

from kicad_autorouter.kipy_adapter import NM_PER_MM, KiPyAdapter
from kicad_autorouter.model import Layer, Point, Track, Via


class FakeBoard:
    def __init__(self):
        self.created = []
        self.pushed = False
        self.dropped = False
        self.tracks = []
        self.vias = []
        self.removed = []

    def begin_commit(self):
        return object()

    def create_items(self, items):
        self.created.extend(items)

    def push_commit(self, _commit, _message):
        self.pushed = True

    def drop_commit(self, _commit):
        self.dropped = True

    def get_tracks(self):
        return self.tracks

    def get_vias(self):
        return self.vias

    def remove_items(self, items):
        self.removed.extend(items)


def test_apply_builds_native_kipy_track_and_via():
    from kipy.board_types import BoardLayer, Net

    board = FakeBoard()
    adapter = KiPyAdapter(board)
    net = Net()
    net.proto.name = "N"
    adapter._kipy_nets = {"N": net}

    adapter.apply(
        [Track("N", Point(1, 2), Point(3, 4), Layer.FRONT, 0.2)],
        [Via("N", Point(3, 4), 0.6, 0.3)],
    )

    assert board.pushed and not board.dropped
    assert len(board.created) == 2
    assert board.created[0].layer == BoardLayer.BL_F_Cu
    assert board.created[0].width == round(0.2 * NM_PER_MM)
    assert board.created[1].diameter == round(0.6 * NM_PER_MM)
    assert board.created[1].drill_diameter == round(0.3 * NM_PER_MM)


def test_connect_wraps_kipy_connection_errors(monkeypatch):
    class DisconnectedClient:
        def get_board(self):
            raise ConnectionError("connection refused")

    monkeypatch.setattr(kipy, "KiCad", DisconnectedClient)
    with pytest.raises(RuntimeError, match="Could not connect to KiCad"):
        KiPyAdapter.connect()


def test_unroute_respects_selection_scope_and_excluded_nets():
    from kicad_autorouter.model import Board, Pad

    board = FakeBoard()
    selected_track = SimpleNamespace(net=SimpleNamespace(name="A"))
    excluded_track = SimpleNamespace(net=SimpleNamespace(name="B"))
    unrelated_track = SimpleNamespace(net=SimpleNamespace(name="C"))
    selected_via = SimpleNamespace(net=SimpleNamespace(name="A"))
    board.tracks = [selected_track, excluded_track, unrelated_track]
    board.vias = [selected_via]
    model = Board(
        outline=(Point(0, 0), Point(10, 0), Point(10, 10), Point(0, 10)),
        pads=[
            Pad("a", "A", Point(1, 1), 1, 1, frozenset({Layer.FRONT})),
            Pad("b", "B", Point(2, 1), 1, 1, frozenset({Layer.FRONT})),
            Pad("c", "C", Point(3, 1), 1, 1, frozenset({Layer.FRONT})),
        ],
    )

    nets, tracks, vias = KiPyAdapter(board).unroute(
        model,
        excluded_nets={"B"},
        selected_pad_ids={"a", "b"},
    )

    assert nets == {"A"}
    assert (tracks, vias) == (1, 1)
    assert board.removed == [selected_track, selected_via]
