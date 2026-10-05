from kicad_autorouter.gui import _route_worker
from kicad_autorouter.model import Board, Layer, Pad, Point, Rules
from kicad_autorouter.router import Router, RouterConfig


class MessageCollector:
    def __init__(self):
        self.messages = []

    def put(self, message):
        self.messages.append(message)


def test_gui_worker_routes_prepared_problem():
    rules = Rules(track_width=0.2, clearance=0.2, edge_clearance=0.2)
    board = Board(
        outline=(Point(0, 0), Point(10, 0), Point(10, 5), Point(0, 5)),
        pads=[
            Pad("A", "N", Point(1, 2.5), 1, 1, frozenset({Layer.FRONT})),
            Pad("B", "N", Point(9, 2.5), 1, 1, frozenset({Layer.FRONT})),
        ],
        default_rules=rules,
    )
    problem = Router(RouterConfig(grid=0.5, cleanup_passes=0)).prepare(board)
    collector = MessageCollector()

    _route_worker(problem, collector)

    iteration = next(message for message in collector.messages if message[0] == "iteration")
    assert iteration[4] == ("routed",)
    assert collector.messages[-1][0] == "result"
    assert collector.messages[-1][1].complete
