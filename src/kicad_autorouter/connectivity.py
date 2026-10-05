from __future__ import annotations

from dataclasses import dataclass

from .geometry import (
    PreparedCopperArea,
    distance_point_to_rectangle,
    distance_point_to_segment,
    distance_segment_to_rectangle,
    segment_distance,
)
from .model import Board, Pad


class UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parent[right] = left


@dataclass(frozen=True, slots=True)
class Connection:
    net: str
    source: Pad
    target: Pad
    congestion_multiplier: float = 1.0


def unrouted_connections(
    board: Board,
    excluded_nets: set[str] | None = None,
    selected_pad_ids: set[str] | None = None,
    priority_pairs: set[frozenset[str]] | None = None,
    priority_multiplier: float = 0.8,
) -> list[Connection]:
    """Return a deterministic nearest-component connection list for each net."""
    result: list[Connection] = []
    nets = sorted(
        {
            pad.net
            for pad in board.pads
            if pad.net and (not excluded_nets or pad.net not in excluded_nets)
        }
    )
    for net in nets:
        pads = [pad for pad in board.pads if pad.net == net]
        terminals = [
            index
            for index, pad in enumerate(pads)
            if selected_pad_ids is None or pad.id in selected_pad_ids
        ]
        if len(terminals) < 2:
            continue
        net_tracks = [track for track in board.tracks if track.net == net]
        net_vias = [via for via in board.vias if via.net == net]
        net_areas = [area for area in board.copper_areas if area.net == net]
        prepared_areas = [PreparedCopperArea(area) for area in net_areas]
        pad_offset = 0
        track_offset = len(pads)
        via_offset = track_offset + len(net_tracks)
        area_offset = via_offset + len(net_vias)
        groups = UnionFind(area_offset + len(net_areas))

        # Pads connect to copper intersecting their conservative rectangular bounds.
        for i, pad in enumerate(pads):
            for j, track in enumerate(net_tracks):
                if (
                    track.layer in pad.layers
                    and distance_segment_to_rectangle(
                        track.start, track.end, pad.center, pad.width, pad.height
                    )
                    <= track.width / 2
                ):
                    groups.union(pad_offset + i, track_offset + j)
            for j, via in enumerate(net_vias):
                if (
                    distance_point_to_rectangle(via.position, pad.center, pad.width, pad.height)
                    <= via.diameter / 2
                ):
                    groups.union(pad_offset + i, via_offset + j)
            for j, prepared in enumerate(prepared_areas):
                if prepared.area.layer in pad.layers and prepared.rectangle_touches(
                    pad.center, pad.width, pad.height
                ):
                    groups.union(pad_offset + i, area_offset + j)

        # Existing track chains and vias form the rest of the conductive graph.
        for i, left in enumerate(net_tracks):
            for j, right in enumerate(net_tracks[i + 1 :], i + 1):
                if (
                    left.layer == right.layer
                    and segment_distance(left.start, left.end, right.start, right.end)
                    <= (left.width + right.width) / 2
                ):
                    groups.union(track_offset + i, track_offset + j)
            for j, via in enumerate(net_vias):
                if (
                    distance_point_to_segment(via.position, left.start, left.end)
                    <= via.diameter / 2 + left.width / 2
                ):
                    groups.union(track_offset + i, via_offset + j)
            for j, prepared in enumerate(prepared_areas):
                if prepared.area.layer == left.layer and prepared.segment_touches(
                    left.start, left.end
                ):
                    groups.union(track_offset + i, area_offset + j)

        for i, left in enumerate(net_vias):
            for j, right in enumerate(net_vias[i + 1 :], i + 1):
                if (
                    left.position.distance_to(right.position)
                    <= (left.diameter + right.diameter) / 2
                ):
                    groups.union(via_offset + i, via_offset + j)
            for j, prepared in enumerate(prepared_areas):
                if prepared.contains(left.position):
                    groups.union(via_offset + i, area_offset + j)

        for i, left in enumerate(net_areas):
            for j, right in enumerate(net_areas[i + 1 :], i + 1):
                if left.layer != right.layer:
                    continue
                if any(
                    prepared_areas[j].segment_touches(
                        left.outline[index],
                        left.outline[(index + 1) % len(left.outline)],
                    )
                    for index in range(len(left.outline))
                ):
                    groups.union(area_offset + i, area_offset + j)

        for i, left in enumerate(pads):
            for j, right in enumerate(pads[i + 1 :], i + 1):
                overlap_x = abs(left.center.x - right.center.x) <= (left.width + right.width) / 2
                overlap_y = abs(left.center.y - right.center.y) <= (left.height + right.height) / 2
                if left.layers & right.layers and overlap_x and overlap_y:
                    groups.union(i, j)

        while len({groups.find(i) for i in terminals}) > 1:
            candidates = [
                (pads[i].center.distance_to(pads[j].center), pads[i].id, pads[j].id, i, j)
                for offset, i in enumerate(terminals)
                for j in terminals[offset + 1 :]
                if groups.find(i) != groups.find(j)
            ]
            _, _, _, i, j = min(candidates)
            multiplier = (
                priority_multiplier
                if priority_pairs is not None
                and frozenset((pads[i].id, pads[j].id)) in priority_pairs
                else 1.0
            )
            result.append(Connection(net, pads[i], pads[j], multiplier))
            groups.union(i, j)
    return result
