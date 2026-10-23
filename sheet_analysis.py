"""
MechHub sheet-metal geometry — OpenCASCADE side of /internal/analyze-sheet.

For a constant-thickness part (flat or bent) the solid itself gives exact
laser-costing facts without unfolding it:

  • flat blank area   = volume / thickness
  • laser cut length  = (surface area − 2 × flat area) / thickness
                        (the edge walls are exactly cut length × thickness)
  • pierces           = 1 outer profile + 1 per internal cut-out
                        (each cut-out leaves an inner wire on both skins)
  • through holes     = concave full cylinders as deep as the sheet
  • bends             = the bend detector's lines, with their lengths

Material-agnostic: the web app prices any sheet material and quantity from it.
"""

import math
import os
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import cadquery as cq
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedMapOfShape

import cnc_analysis

VERSION = "sheet-1"
# A hole counts as a sheet through-hole when its depth is within this band of t
HOLE_DEPTH_BAND = (0.7, 1.5)
# Edge-wall area may come out slightly negative from tessellation-free rounding
EDGE_AREA_TOLERANCE = 0.05
# Share of the flat (planar) area that must sit at one thickness to call it sheet
MIN_CONSTANT_SHARE = 0.5
ANTIPARALLEL_COS = -0.99


def _measure_thickness(planar_faces: List[Any], caster: Any) -> Tuple[Optional[float], float]:
    """Most common skin-to-skin distance, weighted by face area: shoot a ray inward
    from each planar face and keep hits on an opposite, antiparallel face."""
    buckets: Dict[float, float] = {}
    total = 0.0
    for fw, area in planar_faces:
        total += area
        samples = cnc_analysis._sample_face(fw, 1)
        if not samples:
            continue
        p, n = samples[0]
        inward = (-n[0], -n[1], -n[2])
        start = cnc_analysis._add_scaled(p, inward, cnc_analysis.RAY_OFFSET_MM)
        hit = caster.first_hit(start, inward)
        if hit is None:
            continue
        w, hit_face, u, v = hit
        _, hit_n = cnc_analysis._face_normal_at(hit_face, u, v)
        if hit_n is None or cnc_analysis._dot(hit_n, n) > ANTIPARALLEL_COS:
            continue
        d = round(w + cnc_analysis.RAY_OFFSET_MM, 2)
        buckets[d] = buckets.get(d, 0.0) + area
    if not buckets or total <= 0:
        return None, 0.0
    mode = max(buckets, key=buckets.get)
    return mode, buckets[mode] / total


def _round(x: float, places: int = 3) -> float:
    return round(float(x), places)


def _bend_length(b: Dict[str, Any]) -> float:
    s, e = b.get("start"), b.get("end")
    if not s or not e:
        return 0.0
    return math.sqrt((e["x"] - s["x"]) ** 2 + (e["y"] - s["y"]) ** 2 + (e["z"] - s["z"]) ** 2)


def _group_holes(holes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Dict[float, int] = {}
    for h in holes:
        d = round(h["diameterMm"], 2)
        counts[d] = counts.get(d, 0) + 1
    return [{"diameterMm": d, "count": n} for d, n in sorted(counts.items())]


def analyze_sheet_workplane(
    workplane: cq.Workplane,
    bends: List[Dict[str, Any]],
    thickness_mm: Optional[float] = None,
) -> Dict[str, Any]:
    """Measure a sheet part. `bends` come from the converter's bend detector
    (main._extract_bend_features_v2); thickness is measured here unless given."""
    t0 = time.perf_counter()
    solids = sorted(workplane.solids().vals(), key=lambda s: -s.Volume())
    if not solids:
        raise ValueError("STEP file contains no solid bodies — sheet costing needs a closed solid.")
    solid = solids[0]
    volume = solid.Volume()
    if volume <= 0:
        raise ValueError("Solid has zero or negative volume — check that the model is closed.")

    shape_wrapped = solid.wrapped
    caster = cnc_analysis._RayCaster(shape_wrapped)
    face_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape_wrapped, TopAbs_FACE, face_map)

    surface_area = 0.0
    inner_wires = 0
    planar_faces = []
    concave_cyls = []
    for i in range(1, face_map.Extent() + 1):
        fw = TopoDS.Face_s(face_map.FindKey(i))
        face = cq.Face(fw)
        area = face.Area()
        surface_area += area
        info = cnc_analysis._surface_info(fw)
        if info["type"] == "plane":
            planar_faces.append((fw, area))
            inner_wires += len(face.innerWires())
        elif info["type"] == "cylinder":
            samples = cnc_analysis._sample_face(fw, 1)
            if cnc_analysis._concave_cylinder(fw, info, samples):
                concave_cyls.append((i, fw, info))

    if thickness_mm is None:
        thickness_mm, share = _measure_thickness(planar_faces, caster)
        if thickness_mm is None or share < MIN_CONSTANT_SHARE:
            raise ValueError("No constant sheet thickness found — this part doesn't look like sheet metal.")
    if thickness_mm <= 0:
        raise ValueError("No constant sheet thickness found — this part doesn't look like sheet metal.")

    t = float(thickness_mm)
    flat_area = volume / t
    edge_area = surface_area - 2.0 * flat_area
    if edge_area < -EDGE_AREA_TOLERANCE * surface_area:
        raise ValueError(
            f"Detected thickness {t:.2f} mm doesn't match the solid — this part doesn't look like sheet metal."
        )
    cut_length = max(0.0, edge_area) / t

    holes, _, _ = cnc_analysis._detect_holes(concave_cyls, caster)
    lo, hi = HOLE_DEPTH_BAND
    sheet_holes = [h for h in holes if lo * t <= h["depthMm"] <= hi * t]

    bb = solid.BoundingBox()
    bend_rows = [
        {
            "angleDeg": _round(b.get("angle", 0.0), 1),
            "radiusMm": _round(max(0.0, float(b.get("radius", 0.0)))),
            "lengthMm": _round(_bend_length(b), 1),
        }
        for b in bends
    ]

    return {
        "version": VERSION,
        "units": "mm",
        "thicknessMm": _round(t),
        "volumeMm3": _round(volume, 1),
        "surfaceAreaMm2": _round(surface_area, 1),
        "flatAreaMm2": _round(flat_area, 1),
        "cutLengthMm": _round(cut_length, 1),
        "pierces": 1 + int(round(inner_wires / 2.0)),
        "holes": _group_holes(sheet_holes),
        "bends": bend_rows,
        "boundingBox": {"x": _round(bb.xlen), "y": _round(bb.ylen), "z": _round(bb.zlen)},
        "isFlat": len(bend_rows) == 0,
        "bodyCount": len(solids),
        "analysisMs": int((time.perf_counter() - t0) * 1000),
    }


def analyze_step_sheet(
    step_bytes: bytes,
    detect_bends: Callable[[cq.Workplane], List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """STEP bytes → /internal/analyze-sheet response body. Raises ValueError on bad input."""
    with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as f:
        f.write(step_bytes)
        step_path = f.name
    try:
        shape = cq.importers.importStep(step_path)
        if shape is None or shape.val() is None:
            raise ValueError("STEP file could not be parsed. Ensure it is a valid AP214/AP242 file.")
        return analyze_sheet_workplane(shape, detect_bends(shape))
    finally:
        try:
            os.unlink(step_path)
        except OSError:
            pass
