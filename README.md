# KiCad Autorouter

An experimental, conservative autorouter built on KiCad's IPC/KiPy API. It plans routes in a
KiCad-independent core and applies a successful plan to the open PCB as one undoable commit.

> This is an early MVP. Always inspect the result and run KiCad DRC before fabrication.

## Install as a KiCad plugin

### Subscribe for automatic updates (Linux x86-64)

Add this third-party repository URL in KiCad's Plugin and Content Manager:

```text
https://raw.githubusercontent.com/J-Kreisel/kicad-autorouter/pcm/repository.json
```

Open **Plugin and Content Manager → Manage… → Add repository**, paste the URL, refresh the
repository list, and install **KiCad Autorouter**. KiCad will then offer updates published to this
feed. The subscribed feed currently supports Linux x86-64, including NixOS.

### Install a release archive

The recommended installation is a platform-specific Plugin and Content Manager (PCM) archive from
the GitHub Releases page:

1. Download the ZIP matching your operating system and architecture. Do not extract it.
2. Open KiCad's **Plugin and Content Manager** and choose **Install from File…**.
3. Select the downloaded ZIP and restart PCB Editor when installation finishes.
4. In PCB Editor, click **Open Autorouter** on the toolbar or select it under
   **Tools → External Plugins**.

KiCad creates an isolated Python environment and installs the plugin's `kicad-python` and PySide6
dependencies the first time it loads. This may take a minute. The plugin uses KiCad's native IPC
plugin launcher, so it automatically connects to the PCB Editor instance that opened it.

The PCM package contains the precompiled native Rust router. KiCad installs the declared PySide6
and KiPy Python dependencies into its isolated plugin environment; Nix and a Rust toolchain are not
required on the computer using the plugin.

To build and install a development package for the current platform and KiCad 10:

```bash
nix develop
python tools/build_kicad_plugin.py --install --kicad-version 10.0
```

The resulting PCM archive is written to `dist/`. Restart PCB Editor after a manual development
installation. Release tags build Linux x86-64, Windows x86-64, and macOS arm64 archives
automatically.

## Version 1 scope

- Two to 32 copper layers (`F.Cu`, inner layers, and `B.Cu`)
- Standard signal nets and full-stack through vias
- 45-degree grid routing
- Existing tracks and vias are preserved
- Only disconnected pad groups are routed
- Track width, clearance, and via dimensions come from each netclass
- Straight or curved board outlines, including cutouts
- Filled copper zones and placement-only rule areas
- Existing straight and arc tracks
- Track and via rule-area keepouts
- Broad Via/Pad `physical_hole_clearance` custom rules

Not yet supported: general custom design-rule expressions, differential pairs, length matching,
and blind, buried, or microvias. Copper-pour-only keepouts are irrelevant because the
router does not create zones. Pad obstacles currently use conservative bounding rectangles.
Unsupported board features are rejected rather than ignored where the adapter can detect them. See
`docs/validation.md` for tested boards and current results.

## Development

```bash
nix develop
pytest
ruff check .
cargo fmt --check
cargo clippy --all-targets -- -D warnings
```

The routing core has no Python runtime dependencies. `kicad-python` provides live KiCad integration,
and the optional graphical frontend uses PySide6.

## Use with KiCad

1. Open a PCB with 2–32 copper layers in KiCad PCB Editor.
2. Enable KiCad's IPC API server if it is not already enabled.
3. Start the graphical preflight and routing interface:

   ```bash
   nix develop
   kicad-autorouter-gui
   ```

   The GUI builds the connection graph before routing. Selecting a connection shows any copper
   layer's track map or the full-stack via hard-clearance map, netclass dimensions, congestion
   multiplier, and output group. Every active copper layer has an independent direction policy.
   Routing runs in an isolated worker process and the board is modified only when **Apply to
   KiCad** is pressed. The graph is pannable and zoomable; during negotiation, green connections
   are routed, red connections conflict, and orange connections have no path. The GUI can also
   atomically unroute scoped nets while preserving zones.

The command-line interface remains available:

1. Enter the development shell and start with a dry run:

   ```bash
   nix develop
   kicad-autorouter --dry-run
   ```

2. Route all currently disconnected pad groups:

   ```bash
   kicad-autorouter
   ```

   Or select pads and/or footprints in PCB Editor and route only between those terminals:

   ```bash
   kicad-autorouter --selection
   ```

   Add `--schematic-priority` to give direct, unlabeled two-pin schematic connections a
   `0.8` congestion-cost multiplier. This changes route preference, never clearance legality.

Generated tracks and vias inherit the closest PCB group shared by their endpoint components. If
the endpoints only share an ancestor group, that ancestor owns the copper; if they share no group,
the copper remains at board root.

3. Run PCB DRC and inspect every generated route.

Useful options:

```text
--exclude-nets GND +5V Do not route named nets
--selection            Route selected pads/footprints/groups together
--schematic-priority   Prefer direct unlabeled two-pin schematic wires
--grid 0.25            Routing grid in millimetres
--max-vias 8           Per-connection via limit
--max-visited 2000000  Maximum A* states per connection and iteration
--negotiation-iterations 30  Maximum negotiated-congestion iterations
--present-penalty 4          Cost of currently occupied routing resources
--historical-penalty 1       Persistent cost added to conflicted resources
--cleanup-passes 3           Hard-obstacle route optimization passes
--front-direction horizontal  Prefer horizontal F.Cu routing
--back-direction vertical     Prefer vertical B.Cu routing
--layer-direction In1.Cu=horizontal  Set an individual inner-layer policy
--direction-penalty 0.5       Cost per 45-degree deviation step
--dry-run               Do not modify the board
--allow-partial         Apply successful routes when some routes fail
```

Direction choices are `any`, `horizontal`, `vertical`, `diagonal-up`, `diagonal-down`, and
`disabled`. A preferred move costs no extra, a 45-degree deviation costs one direction penalty,
and a 90-degree deviation costs two. Project DRC edge and hole clearances are always used.

Present congestion is the temporary soft cost generated by other provisional routes in the current
negotiation iteration. Historical congestion is the persistent cost added around exact conflict
sites after an iteration; it never decays. Neither changes final clearance legality.

Without `--allow-partial`, one failed connection discards the entire plan and leaves the board
unchanged.

## Architecture

- `model.py`: normalized board representation in millimetres
- `connectivity.py`: existing-copper connectivity and unrouted tasks
- `geometry.py`: dependency-free reference clearance geometry
- `router.py`: negotiated-congestion and cleanup orchestration
- `gui.py`: routing-problem inspection and background-routing interface
- `rust/src/lib.rs`: native A*, clearance maps, and congestion maps
- `kipy_adapter.py`: extraction and atomic KiCad mutation
- `cli.py`: external-tool interface

The routing core deliberately does not import KiPy, allowing fast tests without a running KiCad
instance. Performance-critical search and map operations are implemented in Rust through PyO3;
KiPy extraction, negotiation orchestration, and atomic board mutation remain Python.

The router uses negotiated congestion exclusively. Every connection keeps a provisional route. As
each connection is rerouted, A* sees all other provisional routes as soft present congestion rather
than permanent obstacles. Remaining conflicts add global historical costs that persist across
iterations, while routing order rotates deterministically. This lets competing routes negotiate
different corridors without greedy ownership or random restarts. Only a conflict-free plan is
committed by default.

Negotiation can be computationally expensive on large boards. Progress is printed after each
iteration, including the remaining generated-route conflicts and individual A* failures.

After negotiation reaches zero conflicts, the router removes and re-optimizes each connection in
turn with every other route treated as fixed geometry. Congestion costs are disabled and a
replacement is accepted only when it reduces the same A* objective—length plus bend and via
costs—without introducing a conflict. Directional preference penalties are disabled during this
phase, while disabled layers remain unavailable. Pass order alternates and cleanup stops when a
complete pass makes no improvements.
