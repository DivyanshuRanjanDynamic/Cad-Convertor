"""
MechHub CNC costing model — pure functions, no CAD kernel imports.

Takes the geometric facts extracted by `cnc_analysis.py` (volumes, areas,
holes, accessibility per sample point, turning symmetry) and turns them into
manufacturing facts: stock size, setup count, process choice, machining time
and DFM issues.

Money is deliberately NOT computed here. Times are returned per
machinability group so the web app can price any material / quantity /
shop-rate combination from a single analysis without re-uploading the part.

Python 3.8 compatible (the production image runs the cadquery 'cq' env).
"""

import itertools
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

ANALYSIS_VERSION = "cnc-1"

# ── Stock catalogues (Indian market standard sizes, mm) ───────────────────────

PLATE_THICKNESSES_MM = [
    3, 4, 5, 6, 8, 10, 12, 16, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80,
    90, 100, 110, 120, 130, 140, 150, 160, 180, 200, 250, 300,
]
ROUND_BAR_DIAMETERS_MM = [
    6, 8, 10, 12, 14, 16, 18, 20, 22, 25, 28, 30, 32, 35, 38, 40, 45, 50,
    55, 60, 65, 70, 75, 80, 85, 90, 95, 100, 110, 120, 130, 140, 150, 160,
    180, 200, 220, 250, 300,
]

BLOCK_ALLOWANCE_PER_SIDE_MM = 2.5   # face-off allowance on every block face
BLOCK_SAW_INCREMENT_MM = 5          # block length/width are saw-cut to 5 mm steps
BAR_DIAMETER_ALLOWANCE_MM = 2.0     # total on diameter (1 mm per side)
BAR_LENGTH_ALLOWANCE_MM = 5.0       # facing both ends + parting-off width

# ── Machine envelope (typical Indian job-shop VMC / CNC lathe) ────────────────

VMC_TRAVEL_MM = (800.0, 500.0, 500.0)
LATHE_MAX_DIAMETER_MM = 300.0
LATHE_MAX_LENGTH_MM = 600.0

# ── Machinability groups ──────────────────────────────────────────────────────
# Conservative rates for a 3-axis VMC / 2-axis lathe with carbide tooling,
# tuned for small Indian job shops rather than high-speed production cells.
#   mill_mrr        roughing material-removal rate, cm³/min
#   mill_finish     finishing surface rate, cm²/min
#   drill_feed      drilling feed, mm/min
#   turn_mrr        lathe roughing MRR, cm³/min
#   turn_finish     lathe finishing surface rate, cm²/min
# Calibrate these against real MechMaster job cards — they ARE the model.

MACHINABILITY: Dict[str, Dict[str, float]] = {
    "aluminium":   {"mill_mrr": 40.0, "mill_finish": 15.0, "drill_feed": 300.0, "turn_mrr": 65.0, "turn_finish": 30.0},
    "brass":       {"mill_mrr": 30.0, "mill_finish": 12.0, "drill_feed": 250.0, "turn_mrr": 50.0, "turn_finish": 25.0},
    "copper":      {"mill_mrr": 20.0, "mill_finish": 10.0, "drill_feed": 200.0, "turn_mrr": 35.0, "turn_finish": 20.0},
    "mild_steel":  {"mill_mrr": 15.0, "mill_finish": 8.0,  "drill_feed": 120.0, "turn_mrr": 28.0, "turn_finish": 16.0},
    "alloy_steel": {"mill_mrr": 10.0, "mill_finish": 6.0,  "drill_feed": 80.0,  "turn_mrr": 18.0, "turn_finish": 12.0},
    "stainless":   {"mill_mrr": 8.0,  "mill_finish": 5.0,  "drill_feed": 60.0,  "turn_mrr": 14.0, "turn_finish": 10.0},
    "plastic":     {"mill_mrr": 60.0, "mill_finish": 20.0, "drill_feed": 400.0, "turn_mrr": 90.0, "turn_finish": 40.0},
}

NON_CUT_OVERHEAD = 0.12        # rapids, approach/retract, probing — fraction of cutting time
TOOL_CHANGE_MIN = 0.2          # ATC swap + approach
DRILL_APPROACH_MIN = 0.08      # per hole: rapid, spot, retract
PECK_DEPTH_RATIO = 3.0         # L/D beyond which peck drilling slows the feed
MAX_DRILL_DIAMETER_MM = 20.0   # larger holes are helically interpolated → milled
SMALL_TOOL_RADIUS_MM = 3.0     # internal radius below this forces small, slow cutters

SETUP_BASE_MIN = 20.0          # per fixturing: vise/chuck, zeroing, first-article check
SETUP_PER_TOOL_MIN = 3.0       # load tool, set length offset
HANDLING_PER_SETUP_MIN = 1.5   # per part: unclamp, flip, clamp, deburr edges

# Turning symmetry thresholds
TURNING_PURE_FRACTION = 0.92   # ≥ this share of area revolved about one axis → lathe only
TURNING_MILL_FRACTION = 0.65   # ≥ this → lathe + secondary milling (flats, cross holes)

DIRECTIONS: Dict[str, Tuple[float, float, float]] = {
    "+X": (1.0, 0.0, 0.0), "-X": (-1.0, 0.0, 0.0),
    "+Y": (0.0, 1.0, 0.0), "-Y": (0.0, -1.0, 0.0),
    "+Z": (0.0, 0.0, 1.0), "-Z": (0.0, 0.0, -1.0),
}


# ── Stock selection ───────────────────────────────────────────────────────────

def _round_up_to(value: float, catalogue: Sequence[float]) -> Optional[float]:
    for size in catalogue:
        if size >= value - 1e-6:
            return float(size)
    return None


def _round_up_step(value: float, step: float) -> float:
    return math.ceil((value - 1e-6) / step) * step


def select_block_stock(bbox: Tuple[float, float, float]) -> Dict[str, Any]:
    """Smallest standard block that encloses the part with face-off allowance.

    The thinnest dimension is matched to a standard plate thickness; the other
    two are saw-cut from plate in 5 mm steps.
    """
    padded = [d + 2 * BLOCK_ALLOWANCE_PER_SIDE_MM for d in bbox]
    order = sorted(range(3), key=lambda i: padded[i])
    dims = [0.0, 0.0, 0.0]

    thin_axis = order[0]
    plate = _round_up_to(padded[thin_axis], PLATE_THICKNESSES_MM)
    # Thicker than any stocked plate → bar/billet, cut to the same 5 mm steps.
    dims[thin_axis] = plate if plate is not None else _round_up_step(padded[thin_axis], BLOCK_SAW_INCREMENT_MM)
    for axis in order[1:]:
        dims[axis] = _round_up_step(padded[axis], BLOCK_SAW_INCREMENT_MM)

    return {
        "shape": "block",
        "dimensionsMm": {"x": dims[0], "y": dims[1], "z": dims[2]},
        "volumeMm3": round(dims[0] * dims[1] * dims[2], 1),
    }


def select_bar_stock(max_diameter_mm: float, length_mm: float) -> Optional[Dict[str, Any]]:
    """Smallest standard round bar for a turned part. None if beyond catalogue."""
    diameter = _round_up_to(max_diameter_mm + BAR_DIAMETER_ALLOWANCE_MM, ROUND_BAR_DIAMETERS_MM)
    if diameter is None:
        return None
    length = length_mm + BAR_LENGTH_ALLOWANCE_MM
    return {
        "shape": "round_bar",
        "dimensionsMm": {"diameter": diameter, "length": round(length, 2)},
        "volumeMm3": round(math.pi * (diameter / 2.0) ** 2 * length, 1),
    }


# ── Setup planning (3-axis accessibility set cover) ───────────────────────────

def plan_setups(
    sample_access: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Minimum set of approach directions that reaches every reachable sample.

    `sample_access` items: {"directions": [...], "floorDirections": [...], "areaMm2": float}
    — one sample point on a face, the directions that can reach it at all,
    the subset that reach it tool-end-on (face-milled rather than side-milled),
    and the share of face area it represents. Exact search over the 63
    non-empty subsets of the six principal directions; among covers of equal
    size, the one face-milling the most area wins (shops face floors, they
    don't side-mill them).
    """
    reachable = [s for s in sample_access if s["directions"]]
    unreachable = [s for s in sample_access if not s["directions"]]

    best: Optional[Tuple[str, ...]] = None
    if reachable:
        names = list(DIRECTIONS.keys())
        needed = [frozenset(s["directions"]) for s in reachable]
        floors = [(frozenset(s.get("floorDirections", [])), s["areaMm2"]) for s in reachable]
        for size in range(1, len(names) + 1):
            best_floor_area = -1.0
            for combo in itertools.combinations(names, size):
                chosen = frozenset(combo)
                if not all(n & chosen for n in needed):
                    continue
                floor_area = sum(a for f, a in floors if f & chosen)
                if floor_area > best_floor_area + 1e-6:
                    best, best_floor_area = combo, floor_area
            if best is not None:
                break

    directions = list(best) if best else ["+Z"]
    return {
        "count": len(directions),
        "directions": directions,
        "inaccessibleAreaMm2": round(sum(s["areaMm2"] for s in unreachable), 2),
        "inaccessibleSampleCount": len(unreachable),
    }


# ── Process selection ─────────────────────────────────────────────────────────

def choose_process(
    turnable_fraction: float,
    turning_fits: bool,
    inaccessible_area_mm2: float,
    total_area_mm2: float,
) -> Dict[str, Any]:
    """Pick the cheapest process family that can make the part."""
    if turning_fits and turnable_fraction >= TURNING_PURE_FRACTION:
        return {"recommended": "cnc_turning", "secondaryMilling": False}
    if turning_fits and turnable_fraction >= TURNING_MILL_FRACTION:
        return {"recommended": "cnc_turning", "secondaryMilling": True}

    inaccessible_share = inaccessible_area_mm2 / total_area_mm2 if total_area_mm2 > 0 else 0.0
    if inaccessible_share > 0.02:
        return {"recommended": "cnc_machining_5axis", "secondaryMilling": False}
    return {"recommended": "cnc_machining_3axis", "secondaryMilling": False}


# ── Hole grouping ─────────────────────────────────────────────────────────────

def drillable_holes(holes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [h for h in holes if h["diameterMm"] <= MAX_DRILL_DIAMETER_MM]


def group_holes(holes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse identical holes (diameter, depth, through) into counted rows."""
    groups: Dict[Tuple[float, float, bool], Dict[str, Any]] = {}
    for h in holes:
        key = (round(h["diameterMm"], 2), round(h["depthMm"], 1), bool(h["through"]))
        if key not in groups:
            groups[key] = {
                "diameterMm": key[0],
                "depthMm": key[1],
                "through": key[2],
                "depthToDiameter": round(h["depthMm"] / h["diameterMm"], 2) if h["diameterMm"] > 0 else 0.0,
                "count": 0,
            }
        groups[key]["count"] += 1
    return sorted(groups.values(), key=lambda g: (g["diameterMm"], g["depthMm"]))


def count_tools(holes: Sequence[Dict[str, Any]], min_internal_radius_mm: Optional[float]) -> int:
    """Roughing + finishing end mill, a spot drill and one drill per diameter,
    plus a small finishing cutter when tight internal radii exist."""
    drill_sizes = {round(h["diameterMm"], 1) for h in drillable_holes(holes)}
    tools = 2 + len(drill_sizes) + (1 if drill_sizes else 0)
    if min_internal_radius_mm is not None and min_internal_radius_mm < SMALL_TOOL_RADIUS_MM:
        tools += 1
    return tools


def count_sharp_internal_corners(concave_edges: Sequence[Dict[str, Any]], directions: Sequence[str]) -> int:
    """A concave plane/plane edge can only be cut sharp when one of its faces is
    a floor for some setup (the tool's end cuts it). Otherwise the corner is
    parallel to every tool axis and is left with the cutter radius."""
    used = set(directions)
    return sum(1 for e in concave_edges if not any(n in used for n in e["normals"] if n))


# ── Machining time ────────────────────────────────────────────────────────────

def _drilling_minutes(holes: Sequence[Dict[str, Any]], feed_mm_min: float) -> float:
    total = 0.0
    for h in drillable_holes(holes):
        d = max(h["diameterMm"], 0.1)
        depth = h["depthMm"] + 0.3 * d  # drill-point breakthrough / tip allowance
        ratio = h["depthMm"] / d
        peck = 1.0 + max(0.0, ratio - PECK_DEPTH_RATIO) * 0.15
        # Small drills run proportionally slower feeds per rev.
        size_factor = min(1.0, 0.4 + d / 10.0)
        total += depth / (feed_mm_min * size_factor) * peck + DRILL_APPROACH_MIN
    return total


def _small_radius_factor(min_internal_radius_mm: Optional[float]) -> float:
    if min_internal_radius_mm is None or min_internal_radius_mm >= SMALL_TOOL_RADIUS_MM:
        return 1.0
    r = max(min_internal_radius_mm, 0.2)
    return 1.0 + (SMALL_TOOL_RADIUS_MM - r) / SMALL_TOOL_RADIUS_MM  # up to ~2×


def estimate_machining_time(
    process: str,
    secondary_milling: bool,
    removed_volume_mm3: float,
    surface_area_mm2: float,
    non_turned_area_mm2: float,
    holes: Sequence[Dict[str, Any]],
    min_internal_radius_mm: Optional[float],
    setups: int,
    tool_count: int,
) -> Dict[str, Dict[str, float]]:
    """Per-part cycle time (minutes) for every machinability group."""
    drill_holes = drillable_holes(holes)
    drilled_volume = sum(math.pi * (h["diameterMm"] / 2.0) ** 2 * h["depthMm"] for h in drill_holes)
    drilled_wall_area = sum(math.pi * h["diameterMm"] * h["depthMm"] for h in drill_holes)

    rough_volume_cm3 = max(0.0, removed_volume_mm3 - drilled_volume) / 1000.0
    finish_area_cm2 = max(0.0, surface_area_mm2 - drilled_wall_area) / 100.0
    radius_factor = _small_radius_factor(min_internal_radius_mm)
    tool_changes = tool_count + max(0, setups - 1) * 2

    out: Dict[str, Dict[str, float]] = {}
    for group, rates in MACHINABILITY.items():
        secondary = 0.0
        if process == "cnc_turning":
            turned_area_cm2 = max(0.0, finish_area_cm2 - non_turned_area_mm2 / 100.0)
            roughing = rough_volume_cm3 / rates["turn_mrr"]
            finishing = turned_area_cm2 / rates["turn_finish"]
            if secondary_milling:
                secondary = (non_turned_area_mm2 / 100.0) / rates["mill_finish"] * radius_factor
        else:
            roughing = rough_volume_cm3 / rates["mill_mrr"]
            finishing = finish_area_cm2 / rates["mill_finish"] * radius_factor

        drilling = _drilling_minutes(holes, rates["drill_feed"])
        cutting = roughing + finishing + drilling + secondary
        overhead = cutting * NON_CUT_OVERHEAD
        tool_change = tool_changes * TOOL_CHANGE_MIN
        out[group] = {
            "roughingMin": round(roughing, 2),
            "finishingMin": round(finishing, 2),
            "drillingMin": round(drilling, 2),
            "secondaryMillingMin": round(secondary, 2),
            "toolChangeMin": round(tool_change, 2),
            "overheadMin": round(overhead, 2),
            "cycleMin": round(cutting + overhead + tool_change, 2),
        }
    return out


def estimate_setup_time(setups: int, tool_count: int) -> float:
    """One-time per batch: fixturing every setup plus tool loading."""
    return round(setups * SETUP_BASE_MIN + tool_count * SETUP_PER_TOOL_MIN, 1)


def estimate_handling_time(setups: int) -> float:
    """Per part: load/unload for every setup."""
    return round(setups * HANDLING_PER_SETUP_MIN, 2)


def estimate_programming_time(setups: int, face_count: int, hole_groups: int) -> float:
    """One-time CAM programming effort, capped for very complex parts."""
    minutes = 15.0 + 5.0 * setups + 0.1 * face_count + 1.0 * hole_groups
    return round(min(minutes, 180.0), 1)


# ── DFM ───────────────────────────────────────────────────────────────────────

def _issue(code: str, severity: str, message: str, fix: str, blocking: bool = False,
           feature: Optional[str] = None) -> Dict[str, Any]:
    issue = {
        "code": code,
        "severity": severity,
        "message": message,
        "fixSuggestion": fix,
        "blocking": blocking,
    }
    if feature:
        issue["affectedFeature"] = feature
    return issue


def run_cnc_dfm(
    process: str,
    bbox: Tuple[float, float, float],
    holes: Sequence[Dict[str, Any]],
    min_internal_radius_mm: Optional[float],
    min_wall_thickness_mm: Optional[float],
    sharp_internal_corners: int,
    inaccessible_area_mm2: float,
    total_area_mm2: float,
    body_count: int,
    stock: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []

    if body_count > 1:
        issues.append(_issue(
            "MULTI_BODY", "warning",
            f"File contains {body_count} separate bodies; only the largest was costed.",
            "Upload each part as its own STEP file for accurate per-part pricing.",
        ))

    if stock is None:
        issues.append(_issue(
            "STOCK_UNAVAILABLE", "error",
            "Part is larger than standard stock sizes.",
            "Contact MechHub for a custom billet or forging quote.",
            blocking=True,
        ))

    if process == "cnc_turning":
        diameter = stock["dimensionsMm"]["diameter"] if stock else 0.0
        length = stock["dimensionsMm"]["length"] if stock else 0.0
        if diameter > LATHE_MAX_DIAMETER_MM or length > LATHE_MAX_LENGTH_MM:
            issues.append(_issue(
                "EXCEEDS_LATHE_ENVELOPE", "error",
                f"Turned part (Ø{diameter:.0f} × {length:.0f} mm) exceeds standard lathe capacity.",
                "Split the part or request a large-lathe quote.",
                blocking=True,
            ))
    else:
        dims = sorted(bbox, reverse=True)
        travel = sorted(VMC_TRAVEL_MM, reverse=True)
        if any(d > t for d, t in zip(dims, travel)):
            issues.append(_issue(
                "EXCEEDS_MACHINE_ENVELOPE", "error",
                f"Part ({dims[0]:.0f} × {dims[1]:.0f} × {dims[2]:.0f} mm) exceeds standard VMC travel.",
                "Split the part into machinable sections or request a large-format quote.",
                blocking=True,
            ))

    share = inaccessible_area_mm2 / total_area_mm2 if total_area_mm2 > 0 else 0.0
    if share > 0.001 and process != "cnc_turning":
        issues.append(_issue(
            "UNDERCUT", "warning",
            f"{share * 100:.1f}% of the surface cannot be reached from any 3-axis direction (undercuts).",
            "Remove undercuts, use T-slot cutters, or accept 5-axis pricing.",
            feature="undercut",
        ))

    for h in holes:
        d, depth = h["diameterMm"], h["depthMm"]
        ratio = depth / d if d > 0 else 0.0
        if d < 1.0:
            issues.append(_issue(
                "SMALL_HOLE", "warning",
                f"Ø{d:.2f} mm hole is below standard drill sizes.",
                "Use Ø1 mm or larger, or specify EDM drilling.",
                feature="hole",
            ))
        if ratio > 10:
            issues.append(_issue(
                "DEEP_HOLE", "error",
                f"Ø{d:.1f} mm hole is {ratio:.1f}× diameter deep — needs gun drilling.",
                "Keep hole depth under 10× diameter or step the diameter.",
                feature="hole",
            ))
        elif ratio > 6:
            issues.append(_issue(
                "DEEP_HOLE", "warning",
                f"Ø{d:.1f} mm hole is {ratio:.1f}× diameter deep; peck drilling adds time.",
                "Keep hole depth under 6× diameter where possible.",
                feature="hole",
            ))

    if min_internal_radius_mm is not None and min_internal_radius_mm < 1.0:
        issues.append(_issue(
            "TIGHT_INTERNAL_RADIUS", "warning",
            f"Internal radius of {min_internal_radius_mm:.2f} mm needs a very small cutter.",
            "Increase internal corner radii to ≥ 1/3 of pocket depth (min 1 mm).",
            feature="pocket",
        ))

    if sharp_internal_corners > 0:
        issues.append(_issue(
            "SHARP_INTERNAL_CORNER", "warning",
            f"{sharp_internal_corners} sharp internal corner(s) cannot be cut by a rotating tool.",
            "Add a corner radius slightly larger than the cutter, or a dog-bone relief.",
            feature="pocket",
        ))

    if min_wall_thickness_mm is not None:
        if min_wall_thickness_mm < 0.5:
            issues.append(_issue(
                "THIN_WALL", "error",
                f"Wall thickness of {min_wall_thickness_mm:.2f} mm will deflect or break during machining.",
                "Keep metal walls ≥ 0.8 mm (≥ 1.5 mm for plastics).",
                blocking=True,
                feature="wall",
            ))
        elif min_wall_thickness_mm < 0.8:
            issues.append(_issue(
                "THIN_WALL", "warning",
                f"Wall thickness of {min_wall_thickness_mm:.2f} mm risks chatter and deflection.",
                "Keep metal walls ≥ 0.8 mm (≥ 1.5 mm for plastics).",
                feature="wall",
            ))

    return issues


# ── Orchestration ─────────────────────────────────────────────────────────────

def build_cnc_report(geom: Dict[str, Any]) -> Dict[str, Any]:
    """Turn extracted geometry facts into the /analyze-cnc response body.

    Expected `geom` keys (all lengths mm):
      volumeMm3, surfaceAreaMm2, bbox (x, y, z), faceCount, bodyCount,
      sampleAccess  [{directions, areaMm2}],
      holes         [{diameterMm, depthMm, through}],
      minInternalRadiusMm, minWallThicknessMm,
      concaveEdges  [{normals: [direction label | None, ...]}],
      turning       {fraction, axis, maxDiameterMm, lengthMm, nonTurnedAreaMm2} | None
    """
    bbox = tuple(geom["bbox"])
    volume = geom["volumeMm3"]
    area = geom["surfaceAreaMm2"]
    holes = geom["holes"]
    turning = geom.get("turning")

    setups_info = plan_setups(geom["sampleAccess"])

    bar = None
    if turning:
        bar = select_bar_stock(turning["maxDiameterMm"], turning["lengthMm"])
    turning_fits = bar is not None
    process_info = choose_process(
        turning["fraction"] if turning else 0.0,
        turning_fits,
        setups_info["inaccessibleAreaMm2"],
        area,
    )
    process = process_info["recommended"]

    if process == "cnc_turning":
        stock = bar
        # Op1 + op2 (flip to face the second end), plus a VMC setup for flats / cross holes.
        setups = 2 + (1 if process_info["secondaryMilling"] else 0)
        setup_directions = ["chuck-A", "chuck-B"] + (["vmc"] if process_info["secondaryMilling"] else [])
        non_turned_area = turning["nonTurnedAreaMm2"]
    else:
        stock = select_block_stock(bbox)
        if process == "cnc_machining_5axis":
            # 3+2 indexing reaches five faces per clamping.
            setups = min(2, max(1, setups_info["count"]))
        else:
            setups = setups_info["count"]
        setup_directions = setups_info["directions"]
        non_turned_area = 0.0

    sharp_corners = 0
    if process != "cnc_turning":
        sharp_corners = count_sharp_internal_corners(geom.get("concaveEdges", []), setup_directions)

    removed = max(0.0, stock["volumeMm3"] - volume) if stock else 0.0
    tools = count_tools(holes, geom.get("minInternalRadiusMm"))
    times = estimate_machining_time(
        process,
        process_info["secondaryMilling"],
        removed,
        area,
        non_turned_area,
        holes,
        geom.get("minInternalRadiusMm"),
        setups,
        tools,
    )
    hole_groups = group_holes(holes)

    dfm = run_cnc_dfm(
        process,
        bbox,
        holes,
        geom.get("minInternalRadiusMm"),
        geom.get("minWallThicknessMm"),
        sharp_corners,
        setups_info["inaccessibleAreaMm2"],
        area,
        geom.get("bodyCount", 1),
        stock,
    )

    return {
        "version": ANALYSIS_VERSION,
        "units": "mm",
        "part": {
            "volumeMm3": round(volume, 2),
            "surfaceAreaMm2": round(area, 2),
            "boundingBox": {"x": round(bbox[0], 3), "y": round(bbox[1], 3), "z": round(bbox[2], 3)},
            "faceCount": geom["faceCount"],
            "bodyCount": geom.get("bodyCount", 1),
        },
        "process": {
            "recommended": process,
            "secondaryMilling": process_info["secondaryMilling"],
            "turnableFraction": round(turning["fraction"], 3) if turning else 0.0,
            "turningAxis": turning["axis"] if process == "cnc_turning" else None,
        },
        "stock": None if stock is None else {
            **stock,
            "removedVolumeMm3": round(removed, 1),
            "removalRatio": round(removed / stock["volumeMm3"], 3) if stock["volumeMm3"] > 0 else 0.0,
        },
        "setups": {
            "count": setups,
            "directions": setup_directions,
            "inaccessibleAreaMm2": setups_info["inaccessibleAreaMm2"],
        },
        "features": {
            "holes": hole_groups,
            "holeCount": len(holes),
            "minInternalRadiusMm": geom.get("minInternalRadiusMm"),
            "minWallThicknessMm": geom.get("minWallThicknessMm"),
            "sharpInternalCorners": sharp_corners,
        },
        "toolCount": tools,
        "machiningTime": times,
        "setupTimeMin": estimate_setup_time(setups, tools),
        "handlingTimePerPartMin": estimate_handling_time(setups),
        "programmingTimeMin": estimate_programming_time(setups, geom["faceCount"], len(hole_groups)),
        "dfm": dfm,
    }
