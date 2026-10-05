from __future__ import annotations

import argparse
import re
import sys

from .kipy_adapter import KiPyAdapter, UnsupportedBoardError
from .model import Layer, RoutingDirection
from .router import Router, RouterConfig

DIRECTIONS = {
    "any": RoutingDirection.ANY,
    "horizontal": RoutingDirection.HORIZONTAL,
    "vertical": RoutingDirection.VERTICAL,
    "diagonal-up": RoutingDirection.DIAGONAL_UP,
    "diagonal-down": RoutingDirection.DIAGONAL_DOWN,
    "disabled": RoutingDirection.DISABLED,
}


def _layer_direction(value: str) -> tuple[Layer, RoutingDirection]:
    layer_name, separator, direction_name = value.partition("=")
    if not separator or direction_name not in DIRECTIONS:
        raise argparse.ArgumentTypeError("expected LAYER=DIRECTION")
    normalized = layer_name.lower()
    if normalized == "f.cu":
        layer = Layer.FRONT
    elif normalized == "b.cu":
        layer = Layer.BACK
    else:
        match = re.fullmatch(r"in(\d+)\.cu", normalized)
        if match is None or not 1 <= int(match.group(1)) <= 30:
            raise argparse.ArgumentTypeError(f"unknown copper layer {layer_name!r}")
        layer = Layer(int(match.group(1)))
    return layer, DIRECTIONS[direction_name]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Route unrouted connections in the open KiCad PCB")
    parser.add_argument("--exclude-nets", nargs="+", help="do not route these net names")
    parser.add_argument(
        "--selection",
        action="store_true",
        help="route only between pads, footprints, and groups selected in PCB Editor",
    )
    parser.add_argument(
        "--schematic-priority",
        action="store_true",
        help="apply 0.8x congestion cost to direct, unlabeled two-pin schematic wires",
    )
    parser.add_argument(
        "--grid", type=float, default=0.25, help="routing grid in mm (default: 0.25)"
    )
    parser.add_argument("--max-vias", type=int, default=8, help="maximum vias per connection")
    parser.add_argument(
        "--max-visited",
        type=int,
        default=2_000_000,
        help="maximum A* states per connection and iteration (default: 2000000)",
    )
    parser.add_argument(
        "--negotiation-iterations",
        type=int,
        default=30,
        help="maximum negotiated-congestion iterations (default: 30)",
    )
    parser.add_argument(
        "--present-penalty",
        type=float,
        default=4.0,
        help="cost of currently occupied routing resources (default: 4)",
    )
    parser.add_argument(
        "--historical-penalty",
        type=float,
        default=1.0,
        help="persistent cost added to conflicted resources (default: 1)",
    )
    parser.add_argument(
        "--cleanup-passes",
        type=int,
        default=3,
        help="maximum hard-obstacle optimization passes (default: 3)",
    )
    parser.add_argument(
        "--front-direction",
        choices=DIRECTIONS,
        default="any",
        help="F.Cu direction preference or disabled (default: any)",
    )
    parser.add_argument(
        "--back-direction",
        choices=DIRECTIONS,
        default="any",
        help="B.Cu direction preference or disabled (default: any)",
    )
    parser.add_argument(
        "--direction-penalty",
        type=float,
        default=0.5,
        help="cost per 45-degree step away from a preferred direction (default: 0.5)",
    )
    parser.add_argument(
        "--layer-direction",
        action="append",
        type=_layer_direction,
        default=[],
        metavar="LAYER=DIRECTION",
        help="set an individual layer policy, for example In1.Cu=horizontal",
    )
    parser.add_argument("--dry-run", action="store_true", help="plan without modifying the board")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="apply successfully routed connections even if others fail",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        adapter = KiPyAdapter.connect()
        board = adapter.extract()
        requested_directions = dict(args.layer_direction)
        inactive = set(requested_directions).difference(board.copper_layers)
        if inactive:
            names = ", ".join(sorted(layer.label for layer in inactive))
            raise ValueError(f"Layer direction specified for inactive layer(s): {names}")
        layer_directions = tuple(
            (
                layer,
                requested_directions.get(
                    layer,
                    DIRECTIONS[args.front_direction]
                    if layer == Layer.FRONT
                    else DIRECTIONS[args.back_direction]
                    if layer == Layer.BACK
                    else RoutingDirection.ANY,
                ),
            )
            for layer in board.copper_layers
        )
        selected_pad_ids = adapter.selected_pad_ids() if args.selection else None
        if args.selection and not selected_pad_ids:
            raise ValueError("--selection requires at least one selected pad, footprint, or group")
        priority_pairs = adapter.direct_schematic_pairs() if args.schematic_priority else None
        router = Router(
            RouterConfig(
                grid=args.grid,
                max_vias_per_connection=args.max_vias,
                max_visited=args.max_visited,
                max_negotiation_iterations=args.negotiation_iterations,
                present_congestion_penalty=args.present_penalty,
                historical_congestion_penalty=args.historical_penalty,
                cleanup_passes=args.cleanup_passes,
                allow_partial=args.allow_partial,
                front_direction=DIRECTIONS[args.front_direction],
                back_direction=DIRECTIONS[args.back_direction],
                layer_directions=layer_directions,
                direction_penalty=args.direction_penalty,
            ),
            progress=lambda iteration, conflicts, failures: print(
                f"Negotiation {iteration}: {conflicts} conflicts, {failures} path failures",
                file=sys.stderr,
                flush=True,
            ),
            cleanup_progress=lambda pass_index, improvements: print(
                f"Cleanup {pass_index}: {improvements} improved connections",
                file=sys.stderr,
                flush=True,
            ),
        )
        plan = router.route(
            board,
            set(args.exclude_nets) if args.exclude_nets else None,
            selected_pad_ids,
            priority_pairs,
        )
    except (RuntimeError, ValueError, UnsupportedBoardError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(
        f"Planned {len(plan.tracks)} tracks, {len(plan.vias)} vias, "
        f"{plan.length:.2f} mm total; {len(plan.failures)} failures."
    )
    for failure in plan.failures:
        print(f"  FAILED {failure.net}: {failure.source} -> {failure.target}: {failure.reason}")
    if plan.failures and not args.allow_partial:
        print("Board was not modified.")
        return 1
    if args.dry_run:
        print("Dry run: board was not modified.")
        return 0
    if not plan.tracks and not plan.vias:
        print("Nothing to route.")
        return 0
    adapter.apply(plan.tracks, plan.vias)
    print("Routing applied to KiCad. Run PCB DRC before fabrication.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
