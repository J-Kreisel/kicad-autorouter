from math import isclose

from kicad_autorouter import (
    Board,
    CopperArea,
    KeepoutArea,
    Layer,
    Pad,
    Point,
    Router,
    RouterConfig,
    RoutingDirection,
    Rules,
    Via,
)
from kicad_autorouter.connectivity import Connection
from kicad_autorouter.model import Track
from kicad_autorouter.router import _CandidateRoute, _ClearanceMap, _PenaltyMap

OUTLINE = (Point(0, 0), Point(12, 0), Point(12, 10), Point(0, 10))
RULES = Rules(clearance=0.2, track_width=0.2, via_diameter=0.6, via_drill=0.3, edge_clearance=0.3)


def signal_pad(name: str, x: float, y: float, layers=frozenset({Layer.FRONT})) -> Pad:
    return Pad(name, "SIGNAL", Point(x, y), 1, 1, layers)


def test_routes_straight_connection():
    board = Board(OUTLINE, [signal_pad("A", 2, 5), signal_pad("B", 10, 5)], default_rules=RULES)
    plan = Router(RouterConfig(grid=0.25)).route(board)
    assert plan.complete
    assert len(plan.tracks) == 1
    assert plan.vias == []
    assert isclose(plan.length, 8.0)


def test_reports_per_connection_progress():
    board = Board(OUTLINE, [signal_pad("A", 2, 5), signal_pad("B", 10, 5)], default_rules=RULES)
    config = RouterConfig(grid=0.25)
    problem = Router(config).prepare(board)
    reports: list[tuple[int, int, int, str]] = []
    Router(config, connection_progress=lambda *args: reports.append(args)).route_problem(problem)

    assert reports
    assert reports[0][0] == 1  # negotiation iteration
    assert reports[0][1] == 1  # connection index
    assert reports[0][2] == 1  # connection total
    assert reports[0][3] == "SIGNAL"


def _plan_signature(plan):
    return (
        plan.complete,
        sorted(
            (
                track.net,
                int(track.layer),
                track.start.x,
                track.start.y,
                track.end.x,
                track.end.y,
                track.width,
            )
            for track in plan.tracks
        ),
        sorted(
            (via.net, via.position.x, via.position.y, via.diameter, via.drill) for via in plan.vias
        ),
        sorted((failure.net, failure.reason) for failure in plan.failures),
    )


def test_native_negotiation_matches_python_reference():
    boards = [
        Board(OUTLINE, [signal_pad("A", 2, 5), signal_pad("B", 10, 5)], default_rules=RULES),
        Board(
            OUTLINE,
            [
                Pad("H-left", "H", Point(1, 5), 1, 1, frozenset({Layer.FRONT})),
                Pad("H-right", "H", Point(11, 5), 1, 1, frozenset({Layer.FRONT})),
                Pad("V-top", "V", Point(6, 1), 1, 1, frozenset({Layer.FRONT})),
                Pad("V-bottom", "V", Point(6, 9), 1, 1, frozenset({Layer.FRONT})),
            ],
            default_rules=RULES,
        ),
        Board(
            OUTLINE,
            [
                Pad("A", "A", Point(2, 5), 1, 1, frozenset({Layer.FRONT})),
                Pad("B", "B", Point(10, 5), 1, 1, frozenset({Layer.BACK})),
            ],
            default_rules=RULES,
        ),
    ]
    for board in boards:
        config = RouterConfig(grid=0.5)
        problem = Router(config).prepare(board)
        native = Router(config).route_problem(problem)
        reference = Router(config)._route_problem_python(problem)
        assert _plan_signature(native) == _plan_signature(reference)


def test_preflight_problem_can_be_inspected_then_routed():
    board = Board(OUTLINE, [signal_pad("A", 2, 5), signal_pad("B", 10, 5)], default_rules=RULES)
    router = Router(RouterConfig(grid=0.25))

    problem = router.prepare(board)
    plan = router.route_problem(problem)

    assert len(problem.connections) == 1
    assert len(problem.board_fingerprint) == 64
    assert plan.complete
    assert plan.length == 8


def test_routes_around_foreign_pad_with_45_degree_segments():
    blocker = Pad("X", "OTHER", Point(6, 5), 2, 3, frozenset({Layer.FRONT, Layer.BACK}))
    board = Board(
        OUTLINE,
        [signal_pad("A", 2, 5), signal_pad("B", 10, 5), blocker],
        default_rules=RULES,
    )
    plan = Router(RouterConfig(grid=0.25)).route(board)
    assert plan.complete
    assert len(plan.tracks) >= 3
    for track in plan.tracks:
        dx = abs(track.end.x - track.start.x)
        dy = abs(track.end.y - track.start.y)
        assert isclose(dx, 0) or isclose(dy, 0) or isclose(dx, dy)


def test_uses_via_to_reach_pad_on_other_layer():
    board = Board(
        OUTLINE,
        [
            signal_pad("A", 2, 5, frozenset({Layer.FRONT})),
            signal_pad("B", 10, 5, frozenset({Layer.BACK})),
        ],
        default_rules=RULES,
    )
    plan = Router(RouterConfig(grid=0.5)).route(board)
    assert plan.complete
    assert len(plan.vias) == 1
    assert board.pads[1].contains(plan.vias[0].position)
    assert plan.tracks[-1].end == board.pads[1].center


def test_routes_on_inner_copper_layer():
    layers = (Layer.FRONT, Layer.INNER_1, Layer.INNER_2, Layer.BACK)
    board = Board(
        OUTLINE,
        [
            signal_pad("A", 2, 5, frozenset({Layer.INNER_1})),
            signal_pad("B", 10, 5, frozenset({Layer.INNER_1})),
        ],
        default_rules=RULES,
        copper_layers=layers,
    )

    plan = Router(RouterConfig(grid=0.5)).route(board)

    assert plan.complete
    assert plan.tracks
    assert {track.layer for track in plan.tracks} == {Layer.INNER_1}
    assert plan.vias == []


def test_through_via_can_reach_an_inner_layer():
    layers = (Layer.FRONT, Layer.INNER_1, Layer.INNER_2, Layer.BACK)
    board = Board(
        OUTLINE,
        [
            signal_pad("A", 2, 5, frozenset({Layer.FRONT})),
            signal_pad("B", 10, 5, frozenset({Layer.INNER_2})),
        ],
        default_rules=RULES,
        copper_layers=layers,
    )

    plan = Router(RouterConfig(grid=0.5)).route(board)

    assert plan.complete
    assert len(plan.vias) == 1
    assert {track.layer for track in plan.tracks} == {Layer.FRONT, Layer.INNER_2}


def test_disabled_layer_cannot_be_used():
    board = Board(
        OUTLINE,
        [
            signal_pad("A", 2, 5, frozenset({Layer.FRONT})),
            signal_pad("B", 10, 5, frozenset({Layer.BACK})),
        ],
        default_rules=RULES,
    )

    plan = Router(RouterConfig(grid=0.5, back_direction=RoutingDirection.DISABLED)).route(board)

    assert not plan.complete


def test_direction_preference_uses_stepwise_angular_distance():
    assert Router._direction_steps(RoutingDirection.HORIZONTAL, 1, 0) == 0
    assert Router._direction_steps(RoutingDirection.HORIZONTAL, 1, 1) == 1
    assert Router._direction_steps(RoutingDirection.HORIZONTAL, 0, 1) == 2
    assert Router._direction_steps(RoutingDirection.DIAGONAL_UP, 1, -1) == 0
    assert Router._direction_steps(RoutingDirection.DIAGONAL_UP, 1, 0) == 1
    assert Router._direction_steps(RoutingDirection.DIAGONAL_UP, 1, 1) == 2


def test_impossible_route_is_atomic_by_default():
    wall = Pad("X", "OTHER", Point(6, 5), 2, 10, frozenset({Layer.FRONT, Layer.BACK}))
    board = Board(
        OUTLINE,
        [signal_pad("A", 2, 5), signal_pad("B", 10, 5), wall],
        default_rules=RULES,
    )
    plan = Router(RouterConfig(grid=0.5)).route(board)
    assert not plan.complete
    assert plan.tracks == []
    assert plan.vias == []


def test_result_is_deterministic():
    blocker = Pad("X", "OTHER", Point(6, 5), 2, 3, frozenset({Layer.FRONT, Layer.BACK}))
    board = Board(
        OUTLINE, [signal_pad("A", 2, 5), signal_pad("B", 10, 5), blocker], default_rules=RULES
    )
    router = Router(RouterConfig(grid=0.5))
    assert router.route(board).tracks == router.route(board).tracks


def test_foreign_copper_area_forces_layer_change():
    area = CopperArea(
        "GND",
        Layer.FRONT,
        (Point(5, 0), Point(7, 0), Point(7, 10), Point(5, 10)),
        clearance=0.2,
    )
    board = Board(
        OUTLINE,
        [signal_pad("A", 2, 5), signal_pad("B", 10, 5)],
        default_rules=RULES,
        copper_areas=[area],
    )
    plan = Router(RouterConfig(grid=0.5)).route(board)
    assert plan.complete
    assert len(plan.vias) == 2
    assert any(track.layer == Layer.BACK for track in plan.tracks)


def test_routes_around_board_cutout():
    cutout = (Point(5, 3), Point(7, 3), Point(7, 7), Point(5, 7))
    board = Board(
        OUTLINE,
        [signal_pad("A", 2, 5), signal_pad("B", 10, 5)],
        default_rules=RULES,
        cutouts=(cutout,),
    )
    plan = Router(RouterConfig(grid=0.5)).route(board)
    assert plan.complete
    assert len(plan.tracks) >= 3


def test_track_and_via_keepouts_affect_their_own_clearance_maps():
    keepout = KeepoutArea(
        layers=frozenset({Layer.FRONT}),
        outline=(Point(5, 4), Point(7, 4), Point(7, 6), Point(5, 6)),
        blocks_tracks=True,
    )
    board = Board(
        OUTLINE,
        [signal_pad("A", 2, 5), signal_pad("B", 10, 5)],
        default_rules=RULES,
        keepouts=[keepout],
    )
    problem = Router(RouterConfig(grid=0.5)).prepare(board)
    maps = problem.clearance_maps(problem.connections[0])

    assert not maps.track.clear(Point(6, 5), Layer.FRONT)
    assert maps.track.clear(Point(6, 5), Layer.BACK)
    assert maps.via.clear(Point(6, 5), Layer.FRONT)


def test_via_pad_physical_hole_clearance_applies_to_same_net_pad():
    pads = [signal_pad("A", 2, 5), signal_pad("B", 10, 5)]
    board = Board(
        OUTLINE,
        pads,
        default_rules=RULES,
        via_pad_hole_clearance=0.5,
    )
    problem = Router(RouterConfig(grid=0.5)).prepare(board)
    maps = problem.clearance_maps(problem.connections[0])

    assert not maps.via.clear(Point(2, 5), Layer.FRONT)
    assert maps.track.clear(Point(2, 5), Layer.FRONT)


def test_reuses_existing_same_net_via():
    board = Board(
        OUTLINE,
        [
            signal_pad("A", 2, 5, frozenset({Layer.FRONT})),
            signal_pad("B", 10, 5, frozenset({Layer.BACK})),
        ],
        vias=[Via("SIGNAL", Point(6, 5), 0.6, 0.3)],
        default_rules=RULES,
    )
    plan = Router(RouterConfig(grid=0.5)).route(board)
    assert plan.complete
    assert plan.vias == []
    assert {track.layer for track in plan.tracks} == {Layer.FRONT, Layer.BACK}


def test_only_reuses_through_hole_pad_away_from_its_edge():
    pad = Pad(
        "PTH",
        "SIGNAL",
        Point(6, 5),
        2,
        2,
        frozenset({Layer.FRONT, Layer.BACK}),
        drill=0.8,
    )
    board = Board(OUTLINE, [pad], default_rules=RULES)

    assert Router._has_existing_layer_bridge(board, "SIGNAL", pad.center)
    assert not Router._has_existing_layer_bridge(board, "SIGNAL", Point(6.9, 5.9))


def test_native_conflicts_match_python_reference():
    import random

    from kicad_autorouter.connectivity import Connection
    from kicad_autorouter.router import _CandidateRoute

    def candidate(rng, net):
        tracks = [
            Track(
                net=net,
                start=Point(rng.uniform(0, 8), rng.uniform(0, 8)),
                end=Point(rng.uniform(0, 8), rng.uniform(0, 8)),
                layer=Layer.FRONT,
                width=0.2,
            )
            for _ in range(rng.randint(1, 3))
        ]
        vias = [
            Via(net, Point(rng.uniform(0, 8), rng.uniform(0, 8)), 0.6, 0.3)
            for _ in range(rng.randint(0, 2))
        ]
        pad = Pad("p", net, Point(0, 0), 1, 1, frozenset({Layer.FRONT}))
        return _CandidateRoute(Connection(net, pad, pad), tracks, vias)

    board = Board(OUTLINE, [], default_rules=RULES)
    for seed in range(8):
        rng = random.Random(seed)
        candidates = [candidate(rng, f"N{index % 3}") for index in range(6)]
        native = Router._conflicts(board, candidates)
        reference = Router._conflicts_python(board, candidates)
        assert [(c.left, c.right, c.sites) for c in native] == [
            (c.left, c.right, c.sites) for c in reference
        ]


def test_present_congestion_separates_crossing_nets():
    pads = [
        Pad("H-left", "H", Point(1, 5), 1, 1, frozenset({Layer.FRONT})),
        Pad("H-right", "H", Point(11, 5), 1, 1, frozenset({Layer.FRONT})),
        Pad("V-top", "V", Point(6, 1), 1, 1, frozenset({Layer.FRONT})),
        Pad("V-bottom", "V", Point(6, 9), 1, 1, frozenset({Layer.FRONT})),
    ]
    plan = Router(RouterConfig(grid=0.5)).route(Board(OUTLINE, pads, default_rules=RULES))

    assert plan.complete
    assert {track.net for track in plan.tracks if track.layer == Layer.BACK} == {"V"}
    assert len(plan.vias) == 2


def test_historical_congestion_resolves_conflict_across_iterations():
    pads = [
        Pad("H-left", "H", Point(1, 5), 1, 1, frozenset({Layer.FRONT})),
        Pad("H-right", "H", Point(11, 5), 1, 1, frozenset({Layer.FRONT})),
        Pad("V-top", "V", Point(6, 1), 1, 1, frozenset({Layer.FRONT})),
        Pad("V-bottom", "V", Point(6, 9), 1, 1, frozenset({Layer.FRONT})),
    ]
    board = Board(OUTLINE, pads, default_rules=RULES)
    config = {
        "grid": 0.5,
        "present_congestion_penalty": 0.01,
        "historical_congestion_penalty": 10,
    }

    assert not Router(RouterConfig(**config, max_negotiation_iterations=1)).route(board).complete
    negotiated = Router(RouterConfig(**config, max_negotiation_iterations=10)).route(board)

    assert negotiated.complete
    assert len(negotiated.vias) >= 2


def test_cleanup_replaces_detour_using_pathfinding_score():
    source = signal_pad("A", 1, 5)
    target = signal_pad("B", 11, 5)
    board = Board(OUTLINE, [source, target], default_rules=RULES)
    detour = _CandidateRoute(
        Connection("SIGNAL", source, target),
        [
            Track("SIGNAL", source.center, Point(3, 7), Layer.FRONT, RULES.track_width),
            Track("SIGNAL", Point(3, 7), Point(9, 7), Layer.FRONT, RULES.track_width),
            Track("SIGNAL", Point(9, 7), target.center, Layer.FRONT, RULES.track_width),
        ],
        [],
    )
    router = Router(RouterConfig(grid=0.5, cleanup_passes=2))

    [cleaned] = router._finalize_routes(board, [detour])

    assert router._route_score(cleaned) < router._route_score(detour)
    assert cleaned.tracks == [
        Track("SIGNAL", source.center, target.center, Layer.FRONT, RULES.track_width)
    ]


def test_cleanup_disables_direction_penalty(monkeypatch):
    source = signal_pad("A", 2, 5)
    target = signal_pad("B", 10, 5)
    board = Board(OUTLINE, [source, target], default_rules=RULES)
    route = _CandidateRoute(
        Connection("SIGNAL", source, target),
        [Track("SIGNAL", source.center, target.center, Layer.FRONT, RULES.track_width)],
        [],
    )
    router = Router(
        RouterConfig(
            grid=0.5,
            cleanup_passes=1,
            front_direction=RoutingDirection.VERTICAL,
            direction_penalty=100.0,
        )
    )
    penalties = []

    def no_replacement(*_args, **kwargs):
        penalties.append(kwargs.get("direction_penalty"))

    monkeypatch.setattr(router, "_find_path", no_replacement)

    router._finalize_routes(board, [route])

    assert penalties == [0.0]


def test_native_astar_matches_python_reference():
    source = signal_pad("A", 2, 5)
    target = signal_pad("B", 10, 5)
    blocker = Pad("X", "OTHER", Point(6, 5), 2, 3, frozenset(Layer))
    board = Board(OUTLINE, [source, target, blocker], default_rules=RULES)
    connection = Connection("SIGNAL", source, target)
    router = Router(RouterConfig(grid=0.5, cleanup_passes=0))
    track_map = _ClearanceMap(board, "SIGNAL", RULES.track_width / 2)
    via_map = _ClearanceMap(
        board,
        "SIGNAL",
        RULES.via_diameter / 2,
        via_drill=RULES.via_drill,
    )

    native = router._find_path(board, connection, track_map, via_map)
    reference = router._find_path_python(board, connection, track_map, via_map)

    assert native == reference


def test_native_astar_matches_python_reference_for_randomized_cases():
    import random

    for seed in range(16):
        rng = random.Random(seed)
        source = Pad("A", "SIGNAL", Point(2, 5), 1, 1, frozenset({Layer.FRONT}))
        target_layer = Layer.BACK if seed % 2 else Layer.FRONT
        target = Pad("B", "SIGNAL", Point(10, 5), 1, 1, frozenset({target_layer}))
        blockers = [
            Pad(
                f"X-{index}",
                f"OTHER-{index}",
                Point(rng.randrange(7, 18) / 2, rng.randrange(3, 18) / 2),
                rng.choice((0.5, 1.0, 1.5)),
                rng.choice((0.5, 1.0, 1.5)),
                frozenset({Layer.FRONT, Layer.BACK}),
            )
            for index in range(6)
        ]
        board = Board(OUTLINE, [source, target, *blockers], default_rules=RULES)
        connection = Connection("SIGNAL", source, target)
        router = Router(RouterConfig(grid=0.5, cleanup_passes=0))
        track_map = _ClearanceMap(board, "SIGNAL", RULES.track_width / 2)
        via_map = _ClearanceMap(
            board,
            "SIGNAL",
            RULES.via_diameter / 2,
            via_drill=RULES.via_drill,
        )
        penalties = _PenaltyMap(router.config.grid)
        for _ in range(5):
            penalties.add_point(
                Point(rng.randrange(2, 23) / 2, rng.randrange(2, 19) / 2),
                rng.choice((Layer.FRONT, Layer.BACK)),
                0.5,
                1.0,
            )

        native = router._find_path(board, connection, track_map, via_map, penalties)
        reference = router._find_path_python(board, connection, track_map, via_map, penalties)

        assert native == reference, f"seed {seed}"


def test_route_copper_uses_closest_shared_parent_group():
    source = Pad(
        "A",
        "SIGNAL",
        Point(2, 5),
        1,
        1,
        frozenset({Layer.FRONT}),
        group_path=("source-child", "shared-parent"),
    )
    target = Pad(
        "B",
        "SIGNAL",
        Point(10, 5),
        1,
        1,
        frozenset({Layer.FRONT}),
        group_path=("target-child", "shared-parent"),
    )
    board = Board(OUTLINE, [source, target], default_rules=RULES)

    plan = Router(RouterConfig(grid=0.5, cleanup_passes=0)).route(board)

    assert plan.tracks
    assert {track.group for track in plan.tracks} == {"shared-parent"}
