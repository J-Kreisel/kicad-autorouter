# Validation record

Validation uses KiCad 10.0.6. The external example boards were taken unmodified from the official
KiCad source repository, tag `10.0.6` (`demos/`). They are not redistributed here.

## Automated checks

- Pure geometry, connectivity, routing, cutout, zone, layer-change, and atomic-failure tests.
- Native KiPy `Track` and `Via` construction test.
- `kicad-cli pcb drc` check of the committed unrouted and routed reference boards.
- Ruff static checks and a reproducible Nix package build.
- Rust formatting, Clippy with warnings denied, and native/Python A* differential testing.

## Live KiCad IPC checks

### Minimal reference board

The router connected two PTH pads through a live KiCad IPC session. The board was saved through
KiPy and checked with `kicad-cli pcb drc`:

- Unconnected items: 1 -> 0
- New DRC errors: 0

### Official `multichannel_mixer`, fully unrouted derivative

All 576 tracks, 29 vias, and 6 zones were removed from a temporary copy of the completed official
demo while preserving its valid 114-footprint placement. This left 200 unrouted connections. With
the project's global board minima merged into its netclass rules, the router completed the board:

- 805 generated tracks
- 70 through vias
- 2371.78 mm generated track length
- Unconnected items: 200 -> 0
- Failed connections: 0
- New DRC violations: **0**

Pathfinder-style negotiated congestion reached a conflict-free plan in 19 iterations. The run had
zero individual A* path failures; provisional conflicts fell from 43 after the first iteration to
zero after the nineteenth.

The initial legal negotiated plan contained 1071 tracks, 71 vias, and 2594.82 mm of track. Three
hard-obstacle cleanup passes improved 75, 12, and 4 connections respectively. Cleanup uses the same
length-plus-bend-plus-via objective as A*, without congestion costs, and accepts only strictly
better conflict-free replacements. It reduced the plan by 266 tracks, one via, and 223.04 mm while
preserving zero new DRC violations.

The 87 reports remaining after routing exactly match the copper-free baseline: 81 footprint-library
mismatches and 6 silkscreen/text warnings. This is the largest full-board acceptance case.

The native Rust A* and congestion-map implementation reproduced all 19 negotiation conflict counts,
all three cleanup improvement counts, and the exact final track/via/length totals. End-to-end runtime
on the validation machine fell from approximately two hours to 296.5 seconds. An isolated 100 x
100 mm alternating-wall A* benchmark fell from 25.912 seconds in Python to 0.812 seconds in Rust.

### Official `multichannel_mixer-unrouted` partial-routing stress test

The unmodified board contains 118 footprints, 327 connected pads, 140 existing straight tracks,
filled copper zones, and placement rule areas. On a fresh copy the router generated:

- 406 straight tracks
- 14 through vias
- 1101.33 mm generated track length
- Unconnected items: 148 -> 49
- New DRC violations: **0**
- Pre-existing DRC violations resolved: 5

Forty-seven connection attempts were reported as failures and were not emitted. This board is a
dense stress case rather than the simple-board target; importantly, partial routing remained DRC
safe.

### Official `ecc83-pp` demo, fully unrouted derivative

All existing tracks, vias, and the copper zone were removed from a temporary copy, leaving the
official component placement and 20 unrouted connections. The router completed the board in one
atomic run:

- 71 generated tracks
- 255.65 mm generated track length
- Unconnected items: 20 -> 0
- Failed connections: 0
- New DRC violations: **0**

This is the representative full-board acceptance case for the intended simple-board scope.

### Compatibility extraction

Successfully extracted and analyzed these official KiCad examples:

- `pic_programmer.kicad_pcb`: 247 pads, 370 tracks, 6 vias, filled GND zone
- `ecc83-pp.kicad_pcb`: 33 pads, 59 tracks
- `StickHub.kicad_pcb`: 278 pads, curved Edge.Cuts, arc tracks, 87 vias, filled zones

Arc tracks and curved Edge.Cuts are conservatively tessellated for collision and connectivity
analysis. Generated tracks remain straight 45/90-degree segments.

## Issues found through validation

Live-board testing found and led to fixes for:

- Incorrect interpretation of back-side SMD padstack shape layers.
- Track endpoints stopping in a pad bounding box but outside the actual copper.
- Unsafe final pad fanout crossing filled-zone copper.
- Same-net via drill-to-drill spacing.
- Reuse of existing vias and through-hole pads for layer transitions.
- Excessive search-state and obstacle-map rebuilding costs.
- KiPy footprint instance/definition API differences.
- Curved board outlines and existing arc tracks.
- Board-wide minimum track, connection, via, annular-ring, and drill constraints overriding looser
  netclass values.
- Route-order starvation, replaced by Pathfinder-style negotiated congestion with present and
  persistent historical costs.
- Narrow diagonal entries into vias and through-hole pads.
- Historical-congestion detours and excess bends, reduced by legality-preserving hard-obstacle
  cleanup passes.

## Remaining limits

- Exactly two copper layers.
- No differential pairs or length matching.
- No blind, buried, or microvias.
- Copper/track/via keepout rule areas are rejected; placement-only rule areas are ignored.
- Custom design-rule expressions are not evaluated.
- Pads use conservative axis-aligned bounding boxes for obstacle avoidance; routes terminate at pad
  centers to ensure electrical contact.
- Filled zones are treated as existing copper obstacles. Zones must be filled before routing.
- Negotiated congestion is bounded, so exceptionally dense boards can still remain unrouted when
  its iteration budget is exhausted.
