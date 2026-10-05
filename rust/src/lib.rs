use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap, HashSet};
use std::hash::{BuildHasherDefault, Hasher};

use pyo3::prelude::*;

const HASH_MULTIPLIER: u64 = 0x517c_c1b7_2722_0a95;

/// Fast, non-cryptographic hasher for the integer-keyed routing maps.
#[derive(Default)]
struct FastHasher(u64);

impl Hasher for FastHasher {
    fn finish(&self) -> u64 {
        self.0
    }

    fn write(&mut self, bytes: &[u8]) {
        let mut value = self.0;
        for &byte in bytes {
            value = (value.rotate_left(5) ^ u64::from(byte)).wrapping_mul(HASH_MULTIPLIER);
        }
        self.0 = value;
    }

    fn write_u8(&mut self, value: u8) {
        self.write_u64(u64::from(value));
    }

    fn write_u32(&mut self, value: u32) {
        self.write_u64(u64::from(value));
    }

    fn write_i32(&mut self, value: i32) {
        self.write_u64(u64::from(value as u32));
    }

    fn write_u64(&mut self, value: u64) {
        self.0 = (self.0.rotate_left(5) ^ value).wrapping_mul(HASH_MULTIPLIER);
    }

    fn write_usize(&mut self, value: usize) {
        self.write_u64(value as u64);
    }
}

type FastMap<K, V> = HashMap<K, V, BuildHasherDefault<FastHasher>>;

#[derive(Default)]
struct LayerClearance {
    known: u32,
    clear: u32,
}

#[derive(Clone, Copy, Default)]
struct ViaClearance {
    bridge: bool,
    clear: bool,
}

#[derive(Default)]
struct SearchClearanceCache {
    track: FastMap<(i32, i32), LayerClearance>,
    via: FastMap<(i32, i32), ViaClearance>,
}

fn cached_track_clear(
    map: &NativeClearanceMap,
    cache: &mut FastMap<(i32, i32), LayerClearance>,
    key: (i32, i32),
    point: Point,
    layer: u8,
) -> bool {
    let bit = 1_u32 << layer;
    let cached = cache.entry(key).or_default();
    if cached.known & bit == 0 {
        cached.known |= bit;
        if map.clear(point, layer) {
            cached.clear |= bit;
        }
    }
    cached.clear & bit != 0
}

fn cached_via_clearance(
    map: &NativeClearanceMap,
    cache: &mut FastMap<(i32, i32), ViaClearance>,
    key: (i32, i32),
    point: Point,
    active_layers: &[u8],
    bridges: &[(f64, f64, f64)],
) -> ViaClearance {
    *cache.entry(key).or_insert_with(|| {
        let bridge = bridges
            .iter()
            .any(|&(x, y, radius)| point.distance(Point { x, y }) <= radius);
        ViaClearance {
            bridge,
            clear: bridge || active_layers.iter().all(|&layer| map.clear(point, layer)),
        }
    })
}

#[derive(Clone, Copy, Debug, PartialEq)]
struct Point {
    x: f64,
    y: f64,
}

impl Point {
    fn distance(self, other: Self) -> f64 {
        (self.x - other.x).hypot(self.y - other.y)
    }
}

impl From<(f64, f64)> for Point {
    fn from(value: (f64, f64)) -> Self {
        Self {
            x: value.0,
            y: value.1,
        }
    }
}

fn distance_to_segment(point: Point, start: Point, end: Point) -> f64 {
    let dx = end.x - start.x;
    let dy = end.y - start.y;
    if dx == 0.0 && dy == 0.0 {
        return point.distance(start);
    }
    let t = (((point.x - start.x) * dx + (point.y - start.y) * dy) / (dx * dx + dy * dy))
        .clamp(0.0, 1.0);
    point.distance(Point {
        x: start.x + t * dx,
        y: start.y + t * dy,
    })
}

fn point_in_polygon(point: Point, polygon: &[Point]) -> bool {
    let mut inside = false;
    let mut previous = polygon[polygon.len() - 1];
    for &current in polygon {
        if (current.y > point.y) != (previous.y > point.y) {
            let x_cross = (previous.x - current.x) * (point.y - current.y)
                / (previous.y - current.y)
                + current.x;
            if point.x < x_cross {
                inside = !inside;
            }
        }
        previous = current;
    }
    inside
}

fn distance_to_polygon_edge(point: Point, polygon: &[Point]) -> f64 {
    polygon
        .iter()
        .enumerate()
        .map(|(index, &start)| {
            distance_to_segment(point, start, polygon[(index + 1) % polygon.len()])
        })
        .fold(f64::INFINITY, f64::min)
}

enum Obstacle {
    Rectangle {
        center: Point,
        width: f64,
        height: f64,
        required: f64,
    },
    Segment {
        start: Point,
        end: Point,
        required: f64,
    },
    Point {
        center: Point,
        required: f64,
    },
    Area {
        outline: Vec<Point>,
        holes: Vec<Vec<Point>>,
        required: f64,
    },
}

impl Obstacle {
    fn blocked(&self, point: Point) -> bool {
        match self {
            Self::Rectangle {
                center,
                width,
                height,
                required,
            } => {
                let dx = ((point.x - center.x).abs() - width / 2.0).max(0.0);
                let dy = ((point.y - center.y).abs() - height / 2.0).max(0.0);
                dx.hypot(dy) + 1e-9 < *required
            }
            Self::Segment {
                start,
                end,
                required,
            } => distance_to_segment(point, *start, *end) + 1e-9 < *required,
            Self::Point { center, required } => point.distance(*center) + 1e-9 < *required,
            Self::Area {
                outline,
                holes,
                required,
            } => {
                let in_copper = point_in_polygon(point, outline)
                    && !holes.iter().any(|hole| point_in_polygon(point, hole));
                in_copper
                    || distance_to_polygon_edge(point, outline) + 1e-9 < *required
                    || holes
                        .iter()
                        .any(|hole| distance_to_polygon_edge(point, hole) + 1e-9 < *required)
            }
        }
    }
}

type RectangleSpec = (u8, f64, f64, f64, f64, f64);
type SegmentSpec = (u8, f64, f64, f64, f64, f64);
type PointSpec = (u8, f64, f64, f64);
type AreaSpec = (u8, Vec<(f64, f64)>, Vec<Vec<(f64, f64)>>, f64);

#[pyclass]
struct NativeClearanceMap {
    outline: Vec<Point>,
    cutouts: Vec<Vec<Point>>,
    radius: f64,
    edge_clearance: f64,
    cell_size: f64,
    obstacles: Vec<Obstacle>,
    cells: FastMap<(i32, i32, u8), Vec<usize>>,
}

impl NativeClearanceMap {
    fn insert(&mut self, layer: u8, bounds: (f64, f64, f64, f64), obstacle: Obstacle) {
        let index = self.obstacles.len();
        self.obstacles.push(obstacle);
        let min_x = (bounds.0 / self.cell_size).floor() as i32;
        let min_y = (bounds.1 / self.cell_size).floor() as i32;
        let max_x = (bounds.2 / self.cell_size).floor() as i32;
        let max_y = (bounds.3 / self.cell_size).floor() as i32;
        for x in min_x..=max_x {
            for y in min_y..=max_y {
                self.cells.entry((x, y, layer)).or_default().push(index);
            }
        }
    }

    fn clear(&self, point: Point, layer: u8) -> bool {
        if !point_in_polygon(point, &self.outline)
            || distance_to_polygon_edge(point, &self.outline) + 1e-9
                < self.radius + self.edge_clearance
        {
            return false;
        }
        for cutout in &self.cutouts {
            if point_in_polygon(point, cutout)
                || distance_to_polygon_edge(point, cutout) + 1e-9
                    < self.radius + self.edge_clearance
            {
                return false;
            }
        }
        let key = (
            (point.x / self.cell_size).floor() as i32,
            (point.y / self.cell_size).floor() as i32,
            layer,
        );
        self.cells.get(&key).is_none_or(|indices| {
            indices
                .iter()
                .all(|&index| !self.obstacles[index].blocked(point))
        })
    }
}

#[pymethods]
impl NativeClearanceMap {
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(
        outline: Vec<(f64, f64)>,
        cutouts: Vec<Vec<(f64, f64)>>,
        radius: f64,
        edge_clearance: f64,
        cell_size: f64,
        rectangles: Vec<RectangleSpec>,
        segments: Vec<SegmentSpec>,
        points: Vec<PointSpec>,
        areas: Vec<AreaSpec>,
    ) -> Self {
        let mut result = Self {
            outline: outline.into_iter().map(Point::from).collect(),
            cutouts: cutouts
                .into_iter()
                .map(|loop_| loop_.into_iter().map(Point::from).collect())
                .collect(),
            radius,
            edge_clearance,
            cell_size,
            obstacles: Vec::new(),
            cells: FastMap::default(),
        };
        for (layer, x, y, width, height, required) in rectangles {
            result.insert(
                layer,
                (
                    x - width / 2.0 - required,
                    y - height / 2.0 - required,
                    x + width / 2.0 + required,
                    y + height / 2.0 + required,
                ),
                Obstacle::Rectangle {
                    center: Point { x, y },
                    width,
                    height,
                    required,
                },
            );
        }
        for (layer, x1, y1, x2, y2, required) in segments {
            result.insert(
                layer,
                (
                    x1.min(x2) - required,
                    y1.min(y2) - required,
                    x1.max(x2) + required,
                    y1.max(y2) + required,
                ),
                Obstacle::Segment {
                    start: Point { x: x1, y: y1 },
                    end: Point { x: x2, y: y2 },
                    required,
                },
            );
        }
        for (layer, x, y, required) in points {
            result.insert(
                layer,
                (x - required, y - required, x + required, y + required),
                Obstacle::Point {
                    center: Point { x, y },
                    required,
                },
            );
        }
        for (layer, outline, holes, required) in areas {
            let outline: Vec<Point> = outline.into_iter().map(Point::from).collect();
            let holes: Vec<Vec<Point>> = holes
                .into_iter()
                .map(|hole| hole.into_iter().map(Point::from).collect())
                .collect();
            let min_x = outline
                .iter()
                .map(|point| point.x)
                .fold(f64::INFINITY, f64::min);
            let min_y = outline
                .iter()
                .map(|point| point.y)
                .fold(f64::INFINITY, f64::min);
            let max_x = outline
                .iter()
                .map(|point| point.x)
                .fold(f64::NEG_INFINITY, f64::max);
            let max_y = outline
                .iter()
                .map(|point| point.y)
                .fold(f64::NEG_INFINITY, f64::max);
            result.insert(
                layer,
                (
                    min_x - required,
                    min_y - required,
                    max_x + required,
                    max_y + required,
                ),
                Obstacle::Area {
                    outline,
                    holes,
                    required,
                },
            );
        }
        result
    }

    fn clear_point(&self, x: f64, y: f64, layer: u8) -> bool {
        self.clear(Point { x, y }, layer)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct State {
    x: i32,
    y: i32,
    layer: u8,
    vias: u8,
}

impl std::hash::Hash for State {
    fn hash<H: Hasher>(&self, state: &mut H) {
        let mut key = (self.x as i64 as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
        key ^= (self.y as i64 as u64).wrapping_mul(0xC2B2_AE3D_27D4_EB4F);
        key ^= u64::from(self.layer) << 8;
        key ^= u64::from(self.vias);
        state.write_u64(key);
    }
}

#[derive(Default)]
struct ParetoFrontier {
    first: Option<(State, f64)>,
    overflow: Vec<(State, f64)>,
}

impl ParetoFrontier {
    fn iter(&self) -> impl Iterator<Item = &(State, f64)> {
        self.first.iter().chain(self.overflow.iter())
    }

    fn push(&mut self, label: (State, f64)) {
        if self.first.is_none() {
            self.first = Some(label);
        } else {
            self.overflow.push(label);
        }
    }

    fn retain(&mut self, mut keep: impl FnMut(&(State, f64)) -> bool) {
        self.first = self.first.take().filter(&mut keep);
        self.overflow.retain(&mut keep);
        if self.first.is_none() && !self.overflow.is_empty() {
            self.first = Some(self.overflow.remove(0));
        }
    }
}

type ParetoMap = FastMap<(i32, i32, u8), ParetoFrontier>;

fn pareto_cost(pareto: &ParetoMap, state: State) -> Option<f64> {
    pareto
        .get(&(state.x, state.y, state.layer))?
        .iter()
        .find_map(|(candidate, cost)| (*candidate == state).then_some(*cost))
}

#[derive(Clone, Copy)]
struct QueueEntry {
    estimate: f64,
    cost: f64,
    sequence: u64,
    state: State,
    incoming: (i8, i8, bool),
}

impl PartialEq for QueueEntry {
    fn eq(&self, other: &Self) -> bool {
        self.estimate == other.estimate && self.sequence == other.sequence
    }
}

impl Eq for QueueEntry {}

impl PartialOrd for QueueEntry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for QueueEntry {
    fn cmp(&self, other: &Self) -> Ordering {
        other
            .estimate
            .total_cmp(&self.estimate)
            .then_with(|| other.cost.total_cmp(&self.cost))
            .then_with(|| other.sequence.cmp(&self.sequence))
    }
}

fn penalty_key(point: Point, grid: f64, layer: u8) -> (i32, i32, u8) {
    (
        (point.x / grid).round_ties_even() as i32,
        (point.y / grid).round_ties_even() as i32,
        layer,
    )
}

#[pyclass]
#[derive(Clone)]
struct NativeCostMap {
    grid: f64,
    costs: FastMap<(i32, i32, u8), f64>,
}

impl NativeCostMap {
    fn add_disk(&mut self, point: Point, layer: Option<u8>, radius: f64, amount: f64) {
        let cell_radius = ((radius / self.grid).ceil() as i32).max(1);
        let layers: Vec<u8> = match layer {
            Some(layer) => vec![layer],
            None => (0..32).collect(),
        };
        for candidate_layer in layers {
            let (x, y, _) = penalty_key(point, self.grid, candidate_layer);
            for dx in -cell_radius..=cell_radius {
                for dy in -cell_radius..=cell_radius {
                    if dx * dx + dy * dy <= cell_radius * cell_radius {
                        *self
                            .costs
                            .entry((x + dx, y + dy, candidate_layer))
                            .or_default() += amount;
                    }
                }
            }
        }
    }
}

#[pymethods]
impl NativeCostMap {
    #[new]
    fn new(grid: f64) -> Self {
        Self {
            grid,
            costs: FastMap::default(),
        }
    }

    fn copy_map(&self) -> Self {
        self.clone()
    }

    fn cost(&self, x: f64, y: f64, layer: u8) -> f64 {
        self.costs
            .get(&penalty_key(Point { x, y }, self.grid, layer))
            .copied()
            .unwrap_or(0.0)
    }

    fn add_point(&mut self, x: f64, y: f64, layer: Option<u8>, radius: f64, amount: f64) {
        self.add_disk(Point { x, y }, layer, radius, amount);
    }

    #[allow(clippy::too_many_arguments)]
    fn add_track(
        &mut self,
        x1: f64,
        y1: f64,
        x2: f64,
        y2: f64,
        layer: u8,
        radius: f64,
        amount: f64,
    ) {
        let start = Point { x: x1, y: y1 };
        let end = Point { x: x2, y: y2 };
        let steps = (start.distance(end) / (self.grid / 2.0)).ceil().max(1.0) as usize;
        for index in 0..=steps {
            let ratio = index as f64 / steps as f64;
            self.add_disk(
                Point {
                    x: start.x + (end.x - start.x) * ratio,
                    y: start.y + (end.y - start.y) * ratio,
                },
                Some(layer),
                radius,
                amount,
            );
        }
    }

    fn add_via(&mut self, x: f64, y: f64, radius: f64, amount: f64) {
        self.add_disk(Point { x, y }, None, radius, amount);
    }
}

fn line_clear(map: &NativeClearanceMap, start: Point, end: Point, layer: u8, grid: f64) -> bool {
    let steps = (start.distance(end) / (grid / 2.0)).ceil().max(1.0) as usize;
    (1..=steps).all(|index| {
        let ratio = index as f64 / steps as f64;
        map.clear(
            Point {
                x: start.x + (end.x - start.x) * ratio,
                y: start.y + (end.y - start.y) * ratio,
            },
            layer,
        )
    })
}

fn direction_steps(direction: u8, dx: i32, dy: i32) -> u8 {
    match direction {
        1 => u8::from(dy != 0) + u8::from(dx == 0),
        2 => u8::from(dx != 0) + u8::from(dy == 0),
        3 => {
            if dx != 0 && dy != 0 && dx == -dy {
                0
            } else if dx == 0 || dy == 0 {
                1
            } else {
                2
            }
        }
        4 => {
            if dx != 0 && dy != 0 && dx == dy {
                0
            } else if dx == 0 || dy == 0 {
                1
            } else {
                2
            }
        }
        _ => 0,
    }
}

#[allow(clippy::too_many_arguments)]
fn find_path_core(
    track_map: &NativeClearanceMap,
    via_map: &NativeClearanceMap,
    grid: f64,
    bounds: (f64, f64, f64, f64),
    source: (f64, f64),
    source_layers: &[u8],
    target: (f64, f64, f64, f64),
    target_layers: &[u8],
    max_visited: usize,
    max_vias: u8,
    via_cost: f64,
    bend_cost: f64,
    layer_directions: &[(u8, u8)],
    active_layers: &[u8],
    direction_penalty: f64,
    congestion_multiplier: f64,
    penalties: &NativeCostMap,
    bridges: &[(f64, f64, f64)],
    clearance_cache: &mut SearchClearanceCache,
) -> Option<Vec<(i32, i32, u8, u8)>> {
    let mut direction_of = [0_u8; 32];
    for &(layer, direction) in layer_directions {
        direction_of[layer as usize] = direction;
    }
    let mut active_mask = [false; 32];
    for &layer in active_layers {
        active_mask[layer as usize] = true;
    }
    let source = Point::from(source);
    let target_center = Point {
        x: target.0,
        y: target.1,
    };
    let min_x = ((bounds.0 - source.x) / grid).floor() as i32;
    let min_y = ((bounds.1 - source.y) / grid).floor() as i32;
    let max_x = ((bounds.2 - source.x) / grid).ceil() as i32;
    let max_y = ((bounds.3 - source.y) / grid).ceil() as i32;
    let penalty_x: Vec<i32> = (min_x..=max_x)
        .map(|x| ((source.x + x as f64 * grid) / grid).round_ties_even() as i32)
        .collect();
    let penalty_y: Vec<i32> = (min_y..=max_y)
        .map(|y| ((source.y + y as f64 * grid) / grid).round_ties_even() as i32)
        .collect();
    let point_at = |state: State| Point {
        x: source.x + state.x as f64 * grid,
        y: source.y + state.y as f64 * grid,
    };
    let heuristic = |state: State| {
        let point = point_at(state);
        let dx = (point.x - target_center.x).abs() / grid;
        let dy = (point.y - target_center.y).abs() / grid;
        let diagonal = dx.min(dy);
        let planar = diagonal * 2.0_f64.sqrt() + dx.max(dy) - diagonal;
        planar * grid
            + if target_layers.contains(&state.layer) {
                0.0
            } else {
                via_cost
            }
    };
    let mut starts = Vec::new();
    for &layer in source_layers {
        if active_mask[layer as usize]
            && direction_of[layer as usize] != 5
            && cached_track_clear(track_map, &mut clearance_cache.track, (0, 0), source, layer)
        {
            starts.push(State {
                x: 0,
                y: 0,
                layer,
                vias: 0,
            });
        }
    }
    if starts.is_empty() {
        return None;
    }

    let mut queue = BinaryHeap::new();
    let mut came_from: FastMap<State, State> = FastMap::default();
    let mut pareto: ParetoMap = FastMap::default();
    let mut sequence = 0_u64;
    for state in &starts {
        pareto
            .entry((state.x, state.y, state.layer))
            .or_default()
            .push((*state, 0.0));
        queue.push(QueueEntry {
            estimate: heuristic(*state),
            cost: 0.0,
            sequence,
            state: *state,
            incoming: (0, 0, false),
        });
        sequence += 1;
    }

    let mut accept = |next: State,
                      new_cost: f64,
                      previous: State,
                      incoming: (i8, i8, bool),
                      queue: &mut BinaryHeap<QueueEntry>,
                      came_from: &mut FastMap<State, State>,
                      pareto: &mut ParetoMap| {
        let frontier = pareto.entry((next.x, next.y, next.layer)).or_default();
        if frontier
            .iter()
            .any(|(state, cost)| state.vias <= next.vias && *cost <= new_cost)
        {
            return;
        }
        frontier.retain(|(state, cost)| !(state.vias >= next.vias && *cost >= new_cost));
        frontier.push((next, new_cost));
        came_from.insert(next, previous);
        queue.push(QueueEntry {
            estimate: new_cost + heuristic(next),
            cost: new_cost,
            sequence,
            state: next,
            incoming,
        });
        sequence += 1;
    };

    const MOVES: [(i32, i32); 8] = [
        (1, 0),
        (0, 1),
        (-1, 0),
        (0, -1),
        (1, 1),
        (-1, 1),
        (-1, -1),
        (1, -1),
    ];
    let mut visited = 0;
    let mut goal = None;
    while let Some(entry) = queue.pop() {
        if visited >= max_visited {
            break;
        }
        if pareto_cost(&pareto, entry.state) != Some(entry.cost) {
            continue;
        }
        visited += 1;
        let current = entry.state;
        let current_point = point_at(current);
        let in_target = (current_point.x - target.0).abs() <= target.2 / 2.0
            && (current_point.y - target.1).abs() <= target.3 / 2.0;
        let elbow = Point {
            x: target_center.x,
            y: current_point.y,
        };
        if in_target
            && target_layers.contains(&current.layer)
            && line_clear(track_map, current_point, elbow, current.layer, grid)
            && line_clear(track_map, elbow, target_center, current.layer, grid)
        {
            goal = Some(current);
            break;
        }

        let old_direction = (i32::from(entry.incoming.0), i32::from(entry.incoming.1));
        let direction = direction_of[current.layer as usize];
        for (dx, dy) in MOVES {
            if direction == 5 {
                continue;
            }
            let next = State {
                x: current.x + dx,
                y: current.y + dy,
                layer: current.layer,
                vias: current.vias,
            };
            if next.x < min_x || next.x > max_x || next.y < min_y || next.y > max_y {
                continue;
            }
            let point = point_at(next);
            if !cached_track_clear(
                track_map,
                &mut clearance_cache.track,
                (next.x * 2, next.y * 2),
                point,
                next.layer,
            ) {
                continue;
            }
            let midpoint = Point {
                x: (current_point.x + point.x) / 2.0,
                y: (current_point.y + point.y) / 2.0,
            };
            if !cached_track_clear(
                track_map,
                &mut clearance_cache.track,
                (current.x + next.x, current.y + next.y),
                midpoint,
                next.layer,
            ) {
                continue;
            }
            if dx != 0
                && dy != 0
                && (!cached_track_clear(
                    track_map,
                    &mut clearance_cache.track,
                    ((current.x + dx) * 2, current.y * 2),
                    Point {
                        x: source.x + (current.x + dx) as f64 * grid,
                        y: source.y + current.y as f64 * grid,
                    },
                    next.layer,
                ) || !cached_track_clear(
                    track_map,
                    &mut clearance_cache.track,
                    (current.x * 2, (current.y + dy) * 2),
                    Point {
                        x: source.x + current.x as f64 * grid,
                        y: source.y + (current.y + dy) as f64 * grid,
                    },
                    next.layer,
                ))
            {
                continue;
            }
            let bend = if old_direction != (0, 0) && old_direction != (dx, dy) {
                bend_cost
            } else {
                0.0
            };
            let congestion = penalties
                .costs
                .get(&(
                    penalty_x[(next.x - min_x) as usize],
                    penalty_y[(next.y - min_y) as usize],
                    next.layer,
                ))
                .copied()
                .unwrap_or(0.0)
                * congestion_multiplier;
            let new_cost = entry.cost
                + if dx != 0 && dy != 0 {
                    grid * 2.0_f64.sqrt()
                } else {
                    grid
                }
                + bend
                + congestion
                + direction_steps(direction, dx, dy) as f64 * direction_penalty;
            if new_cost < pareto_cost(&pareto, next).unwrap_or(f64::INFINITY) {
                accept(
                    next,
                    new_cost,
                    current,
                    (dx as i8, dy as i8, false),
                    &mut queue,
                    &mut came_from,
                    &mut pareto,
                );
            }
        }

        let via_clearance = cached_via_clearance(
            via_map,
            &mut clearance_cache.via,
            (current.x, current.y),
            current_point,
            active_layers,
            bridges,
        );
        let bridge = via_clearance.bridge;
        let next_vias = if bridge {
            current.vias
        } else if let Some(count) = current.vias.checked_add(1) {
            count
        } else {
            continue;
        };
        let reverses_layer_change = entry.incoming.2;
        let via_clear = via_clearance.clear;
        if !reverses_layer_change && next_vias <= max_vias && via_clear {
            for &next_layer in active_layers {
                if next_layer == current.layer || direction_of[next_layer as usize] == 5 {
                    continue;
                }
                let next = State {
                    x: current.x,
                    y: current.y,
                    layer: next_layer,
                    vias: next_vias,
                };
                let congestion = penalties
                    .costs
                    .get(&(
                        penalty_x[(current.x - min_x) as usize],
                        penalty_y[(current.y - min_y) as usize],
                        current.layer,
                    ))
                    .copied()
                    .unwrap_or(0.0)
                    .max(
                        penalties
                            .costs
                            .get(&(
                                penalty_x[(current.x - min_x) as usize],
                                penalty_y[(current.y - min_y) as usize],
                                next.layer,
                            ))
                            .copied()
                            .unwrap_or(0.0),
                    )
                    * congestion_multiplier;
                let new_cost = entry.cost + if bridge { 0.05 } else { via_cost } + congestion;
                if new_cost < pareto_cost(&pareto, next).unwrap_or(f64::INFINITY) {
                    accept(
                        next,
                        new_cost,
                        current,
                        (0, 0, true),
                        &mut queue,
                        &mut came_from,
                        &mut pareto,
                    );
                }
            }
        }
    }

    let mut current = goal?;
    let mut path = vec![current];
    while !starts.contains(&current) {
        current = came_from[&current];
        path.push(current);
    }
    path.reverse();
    Some(
        path.into_iter()
            .map(|state| (state.x, state.y, state.layer, state.vias))
            .collect(),
    )
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn find_path(
    track_map: &NativeClearanceMap,
    via_map: &NativeClearanceMap,
    grid: f64,
    bounds: (f64, f64, f64, f64),
    source: (f64, f64),
    source_layers: Vec<u8>,
    target: (f64, f64, f64, f64),
    target_layers: Vec<u8>,
    max_visited: usize,
    max_vias: u8,
    via_cost: f64,
    bend_cost: f64,
    layer_directions: Vec<(u8, u8)>,
    active_layers: Vec<u8>,
    direction_penalty: f64,
    congestion_multiplier: f64,
    penalties: &NativeCostMap,
    bridges: Vec<(f64, f64, f64)>,
) -> Option<Vec<(i32, i32, u8, u8)>> {
    let mut clearance_cache = SearchClearanceCache::default();
    find_path_core(
        track_map,
        via_map,
        grid,
        bounds,
        source,
        &source_layers,
        target,
        &target_layers,
        max_visited,
        max_vias,
        via_cost,
        bend_cost,
        &layer_directions,
        &active_layers,
        direction_penalty,
        congestion_multiplier,
        penalties,
        &bridges,
        &mut clearance_cache,
    )
}

#[pyfunction]
fn native_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

fn orientation(p: Point, q: Point, r: Point) -> f64 {
    (q.y - p.y) * (r.x - q.x) - (q.x - p.x) * (r.y - q.y)
}

fn segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool {
    let o1 = orientation(a, b, c);
    let o2 = orientation(a, b, d);
    let o3 = orientation(c, d, a);
    let o4 = orientation(c, d, b);
    o1 * o2 < 0.0 && o3 * o4 < 0.0
}

fn segment_distance(a: Point, b: Point, c: Point, d: Point) -> f64 {
    if segments_intersect(a, b, c, d) {
        return 0.0;
    }
    distance_to_segment(a, c, d)
        .min(distance_to_segment(b, c, d))
        .min(distance_to_segment(c, a, b))
        .min(distance_to_segment(d, a, b))
}

type ConflictSite = (f64, f64, Option<u8>, f64);
type TrackPayload = (f64, f64, f64, f64, f64, u8);
type ViaPayload = (f64, f64, f64, f64);

fn site_key(x: f64, y: f64, layer: Option<u8>, radius: f64) -> (u64, u64, i32, u64) {
    let normalize = |value: f64| if value == 0.0 { 0.0 } else { value };
    (
        normalize(x).to_bits(),
        normalize(y).to_bits(),
        layer.map_or(-1, i32::from),
        normalize(radius).to_bits(),
    )
}

fn push_site(
    sites: &mut Vec<ConflictSite>,
    seen: &mut HashSet<(u64, u64, i32, u64)>,
    x: f64,
    y: f64,
    layer: Option<u8>,
    radius: f64,
) {
    if seen.insert(site_key(x, y, layer, radius)) {
        sites.push((x, y, layer, radius));
    }
}

#[allow(clippy::too_many_arguments)]
fn segment_conflict_sites(
    sites: &mut Vec<ConflictSite>,
    seen: &mut HashSet<(u64, u64, i32, u64)>,
    left: (Point, Point, u8),
    right: (Point, Point, u8),
    required: f64,
) {
    let spacing = 0.125;
    for (start, end, layer, other) in [
        (left.0, left.1, left.2, (right.0, right.1)),
        (right.0, right.1, right.2, (left.0, left.1)),
    ] {
        let distance = start.distance(end);
        let steps = ((distance / spacing).ceil() as usize).max(1);
        for index in 0..=steps {
            let point = Point {
                x: start.x + (end.x - start.x) * index as f64 / steps as f64,
                y: start.y + (end.y - start.y) * index as f64 / steps as f64,
            };
            if distance_to_segment(point, other.0, other.1) + 1e-9 < required {
                push_site(sites, seen, point.x, point.y, Some(layer), required);
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn route_conflict_sites(
    sites: &mut Vec<ConflictSite>,
    seen: &mut HashSet<(u64, u64, i32, u64)>,
    same_net: bool,
    clearance: f64,
    min_hole_to_hole: f64,
    left_tracks: &[TrackPayload],
    left_vias: &[ViaPayload],
    right_tracks: &[TrackPayload],
    right_vias: &[ViaPayload],
) {
    if !same_net {
        for left_track in left_tracks {
            let left_start = Point {
                x: left_track.0,
                y: left_track.1,
            };
            let left_end = Point {
                x: left_track.2,
                y: left_track.3,
            };
            for right_track in right_tracks {
                let required = (left_track.4 + right_track.4) / 2.0 + clearance;
                if left_track.5 == right_track.5 {
                    let right_start = Point {
                        x: right_track.0,
                        y: right_track.1,
                    };
                    let right_end = Point {
                        x: right_track.2,
                        y: right_track.3,
                    };
                    if segment_distance(left_start, left_end, right_start, right_end) + 1e-9
                        < required
                    {
                        segment_conflict_sites(
                            sites,
                            seen,
                            (left_start, left_end, left_track.5),
                            (right_start, right_end, right_track.5),
                            required,
                        );
                    }
                }
            }
            for right_via in right_vias {
                let required = right_via.2 / 2.0 + left_track.4 / 2.0 + clearance;
                if distance_to_segment(
                    Point {
                        x: right_via.0,
                        y: right_via.1,
                    },
                    left_start,
                    left_end,
                ) + 1e-9
                    < required
                {
                    push_site(sites, seen, right_via.0, right_via.1, None, required);
                }
            }
        }
        for right_track in right_tracks {
            let right_start = Point {
                x: right_track.0,
                y: right_track.1,
            };
            let right_end = Point {
                x: right_track.2,
                y: right_track.3,
            };
            for left_via in left_vias {
                let required = left_via.2 / 2.0 + right_track.4 / 2.0 + clearance;
                if distance_to_segment(
                    Point {
                        x: left_via.0,
                        y: left_via.1,
                    },
                    right_start,
                    right_end,
                ) + 1e-9
                    < required
                {
                    push_site(sites, seen, left_via.0, left_via.1, None, required);
                }
            }
        }
    }
    for left_via in left_vias {
        for right_via in right_vias {
            let distance = Point {
                x: left_via.0,
                y: left_via.1,
            }
            .distance(Point {
                x: right_via.0,
                y: right_via.1,
            });
            let copper_required = (left_via.2 + right_via.2) / 2.0 + clearance;
            if !same_net && distance + 1e-9 < copper_required {
                push_site(sites, seen, left_via.0, left_via.1, None, copper_required);
                push_site(sites, seen, right_via.0, right_via.1, None, copper_required);
            }
            let hole_required = (left_via.3 + right_via.3) / 2.0 + min_hole_to_hole;
            if distance + 1e-9 < hole_required {
                push_site(sites, seen, left_via.0, left_via.1, None, hole_required);
                push_site(sites, seen, right_via.0, right_via.1, None, hole_required);
            }
        }
    }
}

#[pyfunction]
fn native_conflicts(
    nets: Vec<String>,
    clearances: Vec<f64>,
    tracks: Vec<Vec<TrackPayload>>,
    vias: Vec<Vec<ViaPayload>>,
    min_hole_to_hole: f64,
) -> Vec<(usize, usize, Vec<ConflictSite>)> {
    let mut result = Vec::new();
    for left in 0..nets.len() {
        for right in (left + 1)..nets.len() {
            let mut sites = Vec::new();
            let mut seen = HashSet::new();
            route_conflict_sites(
                &mut sites,
                &mut seen,
                nets[left] == nets[right],
                clearances[left].max(clearances[right]),
                min_hole_to_hole,
                &tracks[left],
                &vias[left],
                &tracks[right],
                &vias[right],
            );
            if !sites.is_empty() {
                result.push((left, right, sites));
            }
        }
    }
    result
}

#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
fn native_negotiate(
    py: Python<'_>,
    track_maps: Vec<Py<NativeClearanceMap>>,
    via_maps: Vec<Py<NativeClearanceMap>>,
    map_index: Vec<usize>,
    keys: Vec<(String, String, String)>,
    nets: Vec<String>,
    sources: Vec<(f64, f64)>,
    source_layers: Vec<Vec<u8>>,
    targets: Vec<(f64, f64, f64, f64)>,
    target_layers: Vec<Vec<u8>>,
    congestion: Vec<f64>,
    bridges: Vec<Vec<(f64, f64, f64)>>,
    track_widths: Vec<f64>,
    clearances: Vec<f64>,
    via_diameters: Vec<f64>,
    via_drills: Vec<f64>,
    grid: f64,
    bounds: (f64, f64, f64, f64),
    copper_layers: Vec<u8>,
    layer_directions: Vec<(u8, u8)>,
    direction_penalty: f64,
    min_hole_to_hole: f64,
    max_vias: u8,
    max_visited: usize,
    max_iterations: usize,
    present_penalty: f64,
    historical_penalty: f64,
    via_cost: f64,
    bend_cost: f64,
    connection_callback: Option<Py<PyAny>>,
    iteration_callback: Option<Py<PyAny>>,
) -> PyResult<(
    bool,
    Vec<(usize, Vec<TrackPayload>, Vec<ViaPayload>)>,
    Vec<(usize, String)>,
    Vec<usize>,
)> {
    let count = nets.len();
    if count == 0 {
        return Ok((true, Vec::new(), Vec::new(), Vec::new()));
    }

    let mut candidates: Vec<Option<(Vec<TrackPayload>, Vec<ViaPayload>)>> = vec![None; count];
    let mut failures: Vec<Option<String>> = vec![None; count];
    let mut historical = NativeCostMap::new(grid);
    let mut clearance_caches: Vec<SearchClearanceCache> = (0..count)
        .map(|_| SearchClearanceCache::default())
        .collect();

    let mut ordered: Vec<usize> = (0..count).collect();
    ordered.sort_by(|&a, &b| keys[a].cmp(&keys[b]));

    let mut best: Option<(
        (usize, usize, f64),
        Vec<(usize, Vec<TrackPayload>, Vec<ViaPayload>)>,
        Vec<(usize, String)>,
        Vec<usize>,
    )> = None;
    let mut conflict_free = false;

    for iteration in 0..max_iterations {
        let offset = iteration % ordered.len();
        let mut iteration_order: Vec<usize> = ordered[offset..]
            .iter()
            .chain(ordered[..offset].iter())
            .copied()
            .collect();
        if iteration % 2 == 1 {
            iteration_order.reverse();
        }

        for (index, &position) in iteration_order.iter().enumerate() {
            if let Some(callback) = &connection_callback {
                callback.call1(
                    py,
                    (
                        iteration + 1,
                        index + 1,
                        iteration_order.len(),
                        nets[position].clone(),
                    ),
                )?;
            }
            candidates[position] = None;
            let mut routing_costs = historical.clone();
            for other in 0..count {
                if other == position {
                    continue;
                }
                if let Some((tracks, vias)) = &candidates[other] {
                    add_present_congestion(
                        &mut routing_costs,
                        &nets[position],
                        track_widths[position],
                        clearances[position],
                        via_drills[position],
                        &nets[other],
                        clearances[other],
                        tracks,
                        vias,
                        present_penalty,
                        min_hole_to_hole,
                    );
                }
            }
            let map = map_index[position];
            let track_map = track_maps[map].borrow(py);
            let via_map = via_maps[map].borrow(py);
            let path = find_path_core(
                &track_map,
                &via_map,
                grid,
                bounds,
                sources[position],
                &source_layers[position],
                targets[position],
                &target_layers[position],
                max_visited,
                max_vias,
                via_cost,
                bend_cost,
                &layer_directions,
                &copper_layers,
                direction_penalty,
                congestion[position],
                &routing_costs,
                &bridges[position],
                &mut clearance_caches[position],
            );
            drop(track_map);
            drop(via_map);
            match path {
                None => failures[position] = Some("no path found".to_string()),
                Some(raw) => {
                    failures[position] = None;
                    candidates[position] = Some(materialize_path(
                        sources[position],
                        targets[position],
                        &raw,
                        grid,
                        track_widths[position],
                        via_diameters[position],
                        via_drills[position],
                        &bridges[position],
                    ));
                }
            }
        }

        let filtered: Vec<(usize, Vec<TrackPayload>, Vec<ViaPayload>)> = (0..count)
            .filter_map(|position| {
                candidates[position]
                    .as_ref()
                    .map(|(tracks, vias)| (position, tracks.clone(), vias.clone()))
            })
            .collect();

        let conflict_list = candidate_conflicts(&filtered, &nets, &clearances, min_hole_to_hole);
        let current_failures: Vec<(usize, String)> = (0..count)
            .filter_map(|position| {
                failures[position]
                    .as_ref()
                    .map(|reason| (position, reason.clone()))
            })
            .collect();

        let conflicted_positions: HashSet<usize> = conflict_list
            .iter()
            .flat_map(|(left, right, _)| [filtered[*left].0, filtered[*right].0])
            .collect();
        let routed_positions: HashSet<usize> =
            filtered.iter().map(|(position, _, _)| *position).collect();
        let failed_positions: HashSet<usize> = current_failures
            .iter()
            .map(|(position, _)| *position)
            .collect();

        if let Some(callback) = &iteration_callback {
            let statuses: Vec<String> = (0..count)
                .map(|position| {
                    if failed_positions.contains(&position) {
                        "failure"
                    } else if conflicted_positions.contains(&position) {
                        "conflict"
                    } else if routed_positions.contains(&position) {
                        "routed"
                    } else {
                        "pending"
                    }
                    .to_string()
                })
                .collect();
            callback.call1(
                py,
                (
                    iteration + 1,
                    conflict_list.len(),
                    current_failures.len(),
                    statuses,
                ),
            )?;
        }

        let length: f64 = filtered
            .iter()
            .flat_map(|(_, tracks, _)| tracks.iter())
            .map(|track| (track.2 - track.0).hypot(track.3 - track.1))
            .sum();
        let score = (current_failures.len(), conflict_list.len(), length);
        let take = match &best {
            None => true,
            Some((best_score, _, _, _)) => score < *best_score,
        };
        if take {
            let mut conflicted: Vec<usize> = conflicted_positions.iter().copied().collect();
            conflicted.sort_unstable();
            best = Some((
                score,
                filtered.clone(),
                current_failures.clone(),
                conflicted,
            ));
        }
        if current_failures.is_empty() && conflict_list.is_empty() {
            conflict_free = true;
            break;
        }
        if conflict_list.is_empty() {
            break;
        }
        for (_, _, sites) in &conflict_list {
            for (x, y, layer, radius) in sites {
                historical.add_point(*x, *y, *layer, *radius, historical_penalty);
            }
        }
    }

    let (_, best_candidates, best_failures, best_conflicted) =
        best.expect("negotiation runs at least one iteration");
    Ok((
        conflict_free,
        best_candidates,
        best_failures,
        best_conflicted,
    ))
}

#[allow(clippy::too_many_arguments)]
fn add_present_congestion(
    costs: &mut NativeCostMap,
    searching_net: &str,
    searching_track_width: f64,
    searching_clearance: f64,
    searching_via_drill: f64,
    occupied_net: &str,
    occupied_clearance: f64,
    occupied_tracks: &[TrackPayload],
    occupied_vias: &[ViaPayload],
    penalty: f64,
    min_hole_to_hole: f64,
) {
    if searching_net == occupied_net {
        for via in occupied_vias {
            costs.add_via(
                via.0,
                via.1,
                searching_via_drill / 2.0 + via.3 / 2.0 + min_hole_to_hole,
                penalty,
            );
        }
        return;
    }
    let clearance = searching_clearance.max(occupied_clearance);
    for track in occupied_tracks {
        costs.add_track(
            track.0,
            track.1,
            track.2,
            track.3,
            track.5,
            searching_track_width / 2.0 + track.4 / 2.0 + clearance,
            penalty,
        );
    }
    for via in occupied_vias {
        costs.add_via(
            via.0,
            via.1,
            searching_track_width / 2.0 + via.2 / 2.0 + clearance,
            penalty,
        );
    }
}

fn candidate_conflicts(
    candidates: &[(usize, Vec<TrackPayload>, Vec<ViaPayload>)],
    nets: &[String],
    clearances: &[f64],
    min_hole_to_hole: f64,
) -> Vec<(usize, usize, Vec<ConflictSite>)> {
    let mut result = Vec::new();
    for left in 0..candidates.len() {
        for right in (left + 1)..candidates.len() {
            let mut sites = Vec::new();
            let mut seen = HashSet::new();
            route_conflict_sites(
                &mut sites,
                &mut seen,
                nets[candidates[left].0] == nets[candidates[right].0],
                clearances[candidates[left].0].max(clearances[candidates[right].0]),
                min_hole_to_hole,
                &candidates[left].1,
                &candidates[left].2,
                &candidates[right].1,
                &candidates[right].2,
            );
            if !sites.is_empty() {
                result.push((left, right, sites));
            }
        }
    }
    result
}

fn round9(value: f64) -> f64 {
    let scaled = value * 1e9;
    let floor = scaled.floor();
    let difference = scaled - floor;
    let rounded = if difference > 0.5 {
        floor + 1.0
    } else if difference < 0.5 || (floor as i64) % 2 == 0 {
        floor
    } else {
        floor + 1.0
    };
    rounded / 1e9
}

fn has_existing_bridge(bridges: &[(f64, f64, f64)], point: Point) -> bool {
    bridges
        .iter()
        .any(|&(x, y, radius)| Point { x, y }.distance(point) <= radius)
}

#[allow(clippy::too_many_arguments)]
fn materialize_path(
    source: (f64, f64),
    target: (f64, f64, f64, f64),
    path: &[(i32, i32, u8, u8)],
    grid: f64,
    track_width: f64,
    via_diameter: f64,
    via_drill: f64,
    bridges: &[(f64, f64, f64)],
) -> (Vec<TrackPayload>, Vec<ViaPayload>) {
    let mut nodes: Vec<(Point, u8)> = path
        .iter()
        .map(|&(x, y, layer, _)| {
            (
                Point {
                    x: source.0 + x as f64 * grid,
                    y: source.1 + y as f64 * grid,
                },
                layer,
            )
        })
        .collect();
    let target_layer = nodes.last().expect("non-empty path").1;
    let last = nodes.last().expect("non-empty path").0;
    let elbow = Point {
        x: target.0,
        y: last.y,
    };
    if elbow != last {
        nodes.push((elbow, target_layer));
    }
    let target_center = Point {
        x: target.0,
        y: target.1,
    };
    if target_center != nodes.last().expect("non-empty path").0 {
        nodes.push((target_center, target_layer));
    }

    let mut simplified: Vec<(Point, u8)> = Vec::with_capacity(nodes.len());
    for node in nodes {
        if simplified.len() < 2 {
            simplified.push(node);
            continue;
        }
        let a = simplified[simplified.len() - 2];
        let b = simplified[simplified.len() - 1];
        let same_layer = a.1 == b.1 && b.1 == node.1;
        let ab = (round9(b.0.x - a.0.x), round9(b.0.y - a.0.y));
        let bc = (round9(node.0.x - b.0.x), round9(node.0.y - b.0.y));
        if same_layer && ab.0 * bc.1 == ab.1 * bc.0 {
            *simplified.last_mut().expect("non-empty") = node;
        } else {
            simplified.push(node);
        }
    }

    let mut tracks = Vec::new();
    let mut vias = Vec::new();
    for pair in simplified.windows(2) {
        let (start, layer) = pair[0];
        let (end, next_layer) = pair[1];
        if layer == next_layer {
            if start != end {
                tracks.push((start.x, start.y, end.x, end.y, track_width, layer));
            }
        } else if !has_existing_bridge(bridges, start) {
            vias.push((start.x, start.y, via_diameter, via_drill));
        }
    }
    (tracks, vias)
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<NativeClearanceMap>()?;
    module.add_class::<NativeCostMap>()?;
    module.add_function(wrap_pyfunction!(find_path, module)?)?;
    module.add_function(wrap_pyfunction!(native_conflicts, module)?)?;
    module.add_function(wrap_pyfunction!(native_negotiate, module)?)?;
    module.add_function(wrap_pyfunction!(native_version, module)?)?;
    Ok(())
}
