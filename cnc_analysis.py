"""
MechHub CNC geometry extraction — OpenCASCADE side of /analyze-cnc.

Reads a STEP solid and measures the facts the costing model needs:
  • volume, surface area, bounding box
  • per-face sample points and which of the six 3-axis approach directions
    can reach them (ray-cast against the solid itself)
  • drilled holes (concave full cylinders, merged across split faces)
  • minimum internal radius, minimum wall thickness, concave plane/plane edges
  • rotational symmetry about the best candidate axis (turning detection)

Everything returned is plain data; `cnc_model.build_cnc_report` does the rest.
"""

import math
import os
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import cadquery as cq
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepGProp import BRepGProp_Face
from OCP.GeomAbs import (
    GeomAbs_Circle,
    GeomAbs_Cone,
    GeomAbs_Cylinder,
    GeomAbs_Line,
    GeomAbs_Plane,
    GeomAbs_Sphere,
    GeomAbs_SurfaceOfRevolution,
    GeomAbs_Torus,
)
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_IN
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape
from OCP.gp import gp_Ax3, gp_Dir, gp_Lin, gp_Pnt, gp_Pnt2d, gp_Trsf, gp_Vec

import cnc_model

Vec3 = Tuple[float, float, float]

RAY_OFFSET_MM = 0.02          # start rays just off the surface to avoid self-hits
WALL_COS_TOL = 0.05           # |n·d| below this → the face is a wall for direction d
FLOOR_COS = 0.7               # n·d above this → the tool's end faces it (a floor)
MAX_SIDE_MILL_DEPTH_MM = 50.0 # walls deeper than a standard 3×D flute can't be side-milled
AXIS_TOL_MM = 0.05            # coaxiality tolerance
PARALLEL_COS = 0.999
FULL_CIRCLE_SHARE = 0.98      # merged concave cylinders spanning ≥ this × 2π are holes
LARGE_FACE_SHARE = 0.05       # faces above this share of total area get 3 samples
MAX_SAMPLED_FACES = 2500      # beyond this, 1 sample per face to keep latency bounded

UV_CANDIDATES = [
    (0.5, 0.5), (0.25, 0.25), (0.75, 0.75), (0.25, 0.75), (0.75, 0.25),
    (0.5, 0.15), (0.5, 0.85), (0.15, 0.5), (0.85, 0.5),
    (0.1, 0.1), (0.9, 0.9), (0.1, 0.9), (0.9, 0.1),
]


# ── Small vector helpers (plain tuples keep the model code kernel-free) ───────

def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add_scaled(a: Vec3, b: Vec3, s: float) -> Vec3:
    return (a[0] + b[0] * s, a[1] + b[1] * s, a[2] + b[2] * s)


def _norm(a: Vec3) -> float:
    return math.sqrt(_dot(a, a))


def _unit(a: Vec3) -> Optional[Vec3]:
    n = _norm(a)
    if n < 1e-12:
        return None
    return (a[0] / n, a[1] / n, a[2] / n)


def _pnt(p) -> Vec3:
    return (p.X(), p.Y(), p.Z())


def _dist_point_to_line(p: Vec3, origin: Vec3, axis: Vec3) -> float:
    d = _sub(p, origin)
    along = _dot(d, axis)
    return _norm(_add_scaled(d, axis, -along))


def _direction_label(n: Vec3) -> Optional[str]:
    for name, d in cnc_model.DIRECTIONS.items():
        if _dot(n, d) > PARALLEL_COS:
            return name
    return None


# ── Ray casting ───────────────────────────────────────────────────────────────

class _RayCaster:
    def __init__(self, solid_wrapped):
        self._inter = IntCurvesFace_ShapeIntersector()
        self._inter.Load(solid_wrapped, 1e-4)

    def first_hit(self, origin: Vec3, direction: Vec3) -> Optional[Tuple[float, Any, float, float]]:
        """(distance, face, u, v) of the nearest intersection, or None."""
        self._inter.Perform(gp_Lin(gp_Pnt(*origin), gp_Dir(*direction)), 1e-6, 1e9)
        if not self._inter.IsDone() or self._inter.NbPnt() == 0:
            return None
        best = None
        for i in range(1, self._inter.NbPnt() + 1):
            w = self._inter.WParameter(i)
            if best is None or w < best[0]:
                best = (w, self._inter.Face(i), self._inter.UParameter(i), self._inter.VParameter(i))
        return best

    def is_clear(self, origin: Vec3, direction: Vec3) -> bool:
        return self.first_hit(origin, direction) is None


# ── Face sampling ─────────────────────────────────────────────────────────────

def _face_normal_at(face_wrapped, u: float, v: float) -> Tuple[Optional[Vec3], Optional[Vec3]]:
    """Point and orientation-aware outward unit normal at (u, v)."""
    p = gp_Pnt()
    n = gp_Vec()
    BRepGProp_Face(face_wrapped).Normal(u, v, p, n)
    return _pnt(p), _unit((n.X(), n.Y(), n.Z()))


def _sample_face(face_wrapped, count: int) -> List[Tuple[Vec3, Vec3]]:
    """Up to `count` (point, outward normal) pairs lying inside the trimmed face."""
    u0, u1, v0, v1 = cq.Face(face_wrapped)._uvBounds()
    samples: List[Tuple[Vec3, Vec3]] = []
    for fu, fv in UV_CANDIDATES:
        u = u0 + fu * (u1 - u0)
        v = v0 + fv * (v1 - v0)
        state = BRepClass_FaceClassifier(face_wrapped, gp_Pnt2d(u, v), 1e-7).State()
        if state != TopAbs_IN:
            continue
        p, n = _face_normal_at(face_wrapped, u, v)
        if n is None:
            continue
        samples.append((p, n))
        if len(samples) >= count:
            break
    return samples


# ── Analytic surface descriptors ──────────────────────────────────────────────

def _surface_info(face_wrapped) -> Dict[str, Any]:
    adaptor = BRepAdaptor_Surface(face_wrapped)
    stype = adaptor.GetType()
    info: Dict[str, Any] = {"type": "other"}
    if stype == GeomAbs_Plane:
        ax = adaptor.Plane().Axis()
        info = {"type": "plane", "dir": _pnt(ax.Direction())}
    elif stype == GeomAbs_Cylinder:
        cyl = adaptor.Cylinder()
        ax = cyl.Axis()
        u0, u1, _, _ = cq.Face(face_wrapped)._uvBounds()
        info = {
            "type": "cylinder",
            "origin": _pnt(ax.Location()),
            "dir": _pnt(ax.Direction()),
            "radius": cyl.Radius(),
            "span": abs(u1 - u0),
        }
    elif stype == GeomAbs_Cone:
        ax = adaptor.Cone().Axis()
        info = {"type": "cone", "origin": _pnt(ax.Location()), "dir": _pnt(ax.Direction())}
    elif stype == GeomAbs_Torus:
        ax = adaptor.Torus().Axis()
        info = {"type": "torus", "origin": _pnt(ax.Location()), "dir": _pnt(ax.Direction())}
    elif stype == GeomAbs_Sphere:
        info = {"type": "sphere", "origin": _pnt(adaptor.Sphere().Location())}
    elif stype == GeomAbs_SurfaceOfRevolution:
        ax = adaptor.AxeOfRevolution()
        info = {"type": "revolution", "origin": _pnt(ax.Location()), "dir": _pnt(ax.Direction())}
    return info


def _is_coaxial(info: Dict[str, Any], origin: Vec3, axis: Vec3) -> bool:
    if "dir" not in info or "origin" not in info:
        return False
    if abs(_dot(info["dir"], axis)) < PARALLEL_COS:
        return False
    return _dist_point_to_line(info["origin"], origin, axis) < AXIS_TOL_MM


def _edges_of(face_wrapped) -> List[Any]:
    return [e.wrapped for e in cq.Face(face_wrapped).Edges()]


def _plane_is_revolved(face_wrapped, origin: Vec3, axis: Vec3) -> bool:
    """A planar face perpendicular to the axis is lathe-made only if every
    boundary edge is a circle centred on that axis (discs and annuli)."""
    for edge in _edges_of(face_wrapped):
        curve = BRepAdaptor_Curve(edge)
        if curve.GetType() != GeomAbs_Circle:
            return False
        circ_axis = curve.Circle().Axis()
        if abs(_dot(_pnt(circ_axis.Direction()), axis)) < PARALLEL_COS:
            return False
        if _dist_point_to_line(_pnt(circ_axis.Location()), origin, axis) > AXIS_TOL_MM:
            return False
    return True


# ── Feature detection ─────────────────────────────────────────────────────────

def _concave_cylinder(face_wrapped, info: Dict[str, Any], samples: List[Tuple[Vec3, Vec3]]) -> bool:
    """Outward normal pointing toward the axis → material surrounds it (hole/fillet)."""
    if not samples:
        return False
    p, n = samples[0]
    d = _sub(p, info["origin"])
    radial = _add_scaled(d, info["dir"], -_dot(d, info["dir"]))
    return _dot(n, radial) < 0


def _axial_extent(face_wrapped, origin: Vec3, axis: Vec3) -> Tuple[float, float]:
    ts = []
    for v in cq.Face(face_wrapped).Vertices():
        ts.append(_dot(_sub((v.X, v.Y, v.Z), origin), axis))
    if not ts:
        return (0.0, 0.0)
    return (min(ts), max(ts))


def _detect_holes(
    concave_cyls: List[Tuple[int, Any, Dict[str, Any]]],
    caster: _RayCaster,
) -> Tuple[List[Dict[str, Any]], set, Optional[float]]:
    """Merge split concave cylinders; full circles become holes, partial arcs
    are internal corner radii. Returns (holes, hole face indices, min radius)."""
    groups: List[Dict[str, Any]] = []
    for idx, face, info in concave_cyls:
        for g in groups:
            if abs(g["radius"] - info["radius"]) < 1e-3 and _is_coaxial(info, g["origin"], g["dir"]):
                g["members"].append((idx, face))
                g["span"] += info["span"]
                break
        else:
            groups.append({
                "origin": info["origin"],
                "dir": info["dir"],
                "radius": info["radius"],
                "span": info["span"],
                "members": [(idx, face)],
            })

    holes: List[Dict[str, Any]] = []
    hole_faces = set()
    min_internal_radius: Optional[float] = None
    for g in groups:
        if g["span"] < FULL_CIRCLE_SHARE * 2 * math.pi:
            r = g["radius"]
            min_internal_radius = r if min_internal_radius is None else min(min_internal_radius, r)
            continue

        t_min, t_max = math.inf, -math.inf
        for _, face in g["members"]:
            lo, hi = _axial_extent(face, g["origin"], g["dir"])
            t_min, t_max = min(t_min, lo), max(t_max, hi)
        depth = t_max - t_min
        if depth <= 1e-3:
            continue

        mid = _add_scaled(g["origin"], g["dir"], (t_min + t_max) / 2.0)
        neg = (-g["dir"][0], -g["dir"][1], -g["dir"][2])
        open_pos = caster.is_clear(mid, g["dir"])
        open_neg = caster.is_clear(mid, neg)

        holes.append({
            "diameterMm": round(2 * g["radius"], 3),
            "depthMm": round(depth, 3),
            "through": open_pos and open_neg,
            "axis": _direction_label(g["dir"]) or _direction_label(neg),
        })
        for idx, _ in g["members"]:
            hole_faces.add(idx)
    return holes, hole_faces, min_internal_radius


def _detect_turning(
    solid: cq.Solid,
    faces: List[Dict[str, Any]],
    total_area: float,
) -> Optional[Dict[str, Any]]:
    """Find the axis about which most of the surface is revolved."""
    candidates: List[Tuple[Vec3, Vec3, float]] = []
    for f in faces:
        info = f["info"]
        if info["type"] not in ("cylinder", "cone", "torus", "revolution"):
            continue
        for i, (o, d, w) in enumerate(candidates):
            if _is_coaxial(info, o, d):
                candidates[i] = (o, d, w + f["area"])
                break
        else:
            candidates.append((info["origin"], info["dir"], f["area"]))
    if not candidates:
        return None

    candidates.sort(key=lambda c: -c[2])
    best = None
    for origin, axis, _ in candidates[:5]:
        revolved = 0.0
        has_outer = False
        for f in faces:
            info = f["info"]
            if info["type"] == "plane":
                if abs(_dot(info["dir"], axis)) > PARALLEL_COS and _plane_is_revolved(f["face"], origin, axis):
                    revolved += f["area"]
            elif info["type"] == "sphere":
                if _dist_point_to_line(info["origin"], origin, axis) < AXIS_TOL_MM:
                    revolved += f["area"]
            elif _is_coaxial(info, origin, axis):
                revolved += f["area"]
                if info["type"] == "cylinder" and not f["concave"]:
                    has_outer = True
        # A lathe needs an outer revolved surface to grip and turn.
        if not has_outer:
            continue
        fraction = revolved / total_area if total_area > 0 else 0.0
        if best is None or fraction > best["fraction"]:
            best = {"fraction": fraction, "origin": origin, "axis": axis, "revolved": revolved}

    if best is None:
        return None

    # Envelope in the axis frame: radial extent → bar Ø, axial extent → length.
    trsf = gp_Trsf()
    trsf.SetTransformation(gp_Ax3(gp_Pnt(*best["origin"]), gp_Dir(*best["axis"])))
    local = cq.Shape.cast(BRepBuilderAPI_Transform(solid.wrapped, trsf, True).Shape())
    bb = local.BoundingBox()
    radius = max(abs(bb.xmin), abs(bb.xmax), abs(bb.ymin), abs(bb.ymax))

    return {
        "fraction": best["fraction"],
        "axis": {
            "origin": {"x": round(best["origin"][0], 3), "y": round(best["origin"][1], 3), "z": round(best["origin"][2], 3)},
            "direction": {"x": round(best["axis"][0], 4), "y": round(best["axis"][1], 4), "z": round(best["axis"][2], 4)},
        },
        "maxDiameterMm": round(2 * radius, 3),
        "lengthMm": round(bb.zlen, 3),
        "nonTurnedAreaMm2": round(total_area - best["revolved"], 2),
    }


def _iter_shape_list(shape_list) -> List[Any]:
    # Newer OCP bindings make TopTools_ListOfShape iterable; 7.5 (cadquery 2.1) does not.
    try:
        return list(shape_list)
    except TypeError:
        from OCP.TopTools import TopTools_ListIteratorOfListOfShape

        out = []
        it = TopTools_ListIteratorOfListOfShape(shape_list)
        while it.More():
            out.append(it.Value())
            it.Next()
        return out


def _concave_plane_edges(shape_wrapped, face_map, faces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Straight edges where two planar faces meet at an inside (concave) angle."""
    edge_faces = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape_wrapped, TopAbs_EDGE, TopAbs_FACE, edge_faces)
    out: List[Dict[str, Any]] = []
    for i in range(1, edge_faces.Extent() + 1):
        edge = TopoDS.Edge_s(edge_faces.FindKey(i))
        curve = BRepAdaptor_Curve(edge)
        if curve.GetType() != GeomAbs_Line:
            continue
        idxs = sorted({face_map.FindIndex(f) - 1 for f in _iter_shape_list(edge_faces.FindFromIndex(i))})
        if len(idxs) != 2:
            continue
        f1, f2 = faces[idxs[0]], faces[idxs[1]]
        if f1["info"]["type"] != "plane" or f2["info"]["type"] != "plane":
            continue
        if not f1["samples"] or not f2["samples"]:
            continue
        n1, n2 = f1["samples"][0][1], f2["samples"][0][1]
        if abs(_dot(n1, n2)) > PARALLEL_COS:
            continue

        u_mid = (curve.FirstParameter() + curve.LastParameter()) / 2.0
        m = _pnt(curve.Value(u_mid))
        t = _pnt(curve.Line().Direction())
        to_c1 = _sub(f1["center"], m)
        d1 = _unit(_add_scaled(to_c1, t, -_dot(to_c1, t)))
        # Face 1 extends to the side face 2 looks at → the corner is inside.
        if d1 is not None and _dot(d1, n2) > 1e-3:
            out.append({"normals": [_direction_label(n1), _direction_label(n2)]})
    return out


# ── Entry point ───────────────────────────────────────────────────────────────

def extract_cnc_geometry(workplane: cq.Workplane) -> Dict[str, Any]:
    solids = sorted(workplane.solids().vals(), key=lambda s: -s.Volume())
    if not solids:
        raise ValueError("STEP file contains no solid bodies — CNC costing needs a closed solid.")
    solid = solids[0]
    shape_wrapped = solid.wrapped

    bb = solid.BoundingBox()
    volume = solid.Volume()
    if volume <= 0:
        raise ValueError("Solid has zero or negative volume — check that the model is closed.")

    face_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape_wrapped, TopAbs_FACE, face_map)
    face_count = face_map.Extent()

    caster = _RayCaster(shape_wrapped)

    faces: List[Dict[str, Any]] = []
    total_area = 0.0
    for i in range(1, face_count + 1):
        fw = TopoDS.Face_s(face_map.FindKey(i))
        cf = cq.Face(fw)
        area = cf.Area()
        total_area += area
        c = cf.Center()
        fbb = cf.BoundingBox()
        faces.append({
            "face": fw,
            "area": area,
            "center": (c.x, c.y, c.z),
            "extent": {"X": fbb.xlen, "Y": fbb.ylen, "Z": fbb.zlen},
            "info": _surface_info(fw),
        })

    dense = face_count <= MAX_SAMPLED_FACES
    concave_cyls = []
    for idx, f in enumerate(faces):
        n_samples = 3 if dense and f["area"] > LARGE_FACE_SHARE * total_area else 1
        f["samples"] = _sample_face(f["face"], n_samples)
        f["concave"] = f["info"]["type"] == "cylinder" and _concave_cylinder(f["face"], f["info"], f["samples"])
        if f["concave"]:
            concave_cyls.append((idx, f["face"], f["info"]))

    holes, hole_faces, min_internal_radius = _detect_holes(concave_cyls, caster)

    # Accessibility per sample point for the 3-axis setup planner.
    sample_access: List[Dict[str, Any]] = []
    for idx, f in enumerate(faces):
        if not f["samples"]:
            continue
        share = f["area"] / len(f["samples"])
        for p, n in f["samples"]:
            start = _add_scaled(p, n, RAY_OFFSET_MM)
            reachable, floor = [], []
            for name, d in cnc_model.DIRECTIONS.items():
                c = _dot(n, d)
                if c < -WALL_COS_TOL:
                    continue
                if abs(c) <= WALL_COS_TOL and idx not in hole_faces:
                    if f["extent"][name[1]] > MAX_SIDE_MILL_DEPTH_MM:
                        continue
                if caster.is_clear(start, d):
                    reachable.append(name)
                    if c > FLOOR_COS:
                        floor.append(name)
            sample_access.append({"directions": reachable, "floorDirections": floor, "areaMm2": share})

    # Local wall thickness: inward ray from each face to the opposite skin.
    min_wall: Optional[float] = None
    for f in faces:
        if not f["samples"]:
            continue
        p, n = f["samples"][0]
        inward = (-n[0], -n[1], -n[2])
        hit = caster.first_hit(_add_scaled(p, inward, RAY_OFFSET_MM), inward)
        if hit is None:
            continue
        w, hit_face, u, v = hit
        _, hit_n = _face_normal_at(hit_face, u, v)
        if hit_n is None or _dot(hit_n, n) > -0.9:
            continue  # exited through an adjacent face, not the opposite wall
        thickness = w + RAY_OFFSET_MM
        min_wall = thickness if min_wall is None else min(min_wall, thickness)

    turning = _detect_turning(solid, faces, total_area)
    concave_edges = _concave_plane_edges(shape_wrapped, face_map, faces)

    return {
        "volumeMm3": volume,
        "surfaceAreaMm2": total_area,
        "bbox": (bb.xlen, bb.ylen, bb.zlen),
        "faceCount": face_count,
        "bodyCount": len(solids),
        "sampleAccess": sample_access,
        "holes": holes,
        "minInternalRadiusMm": round(min_internal_radius, 3) if min_internal_radius is not None else None,
        "minWallThicknessMm": round(min_wall, 3) if min_wall is not None else None,
        "concaveEdges": concave_edges,
        "turning": turning,
    }


def analyze_cnc_workplane(workplane: cq.Workplane) -> Dict[str, Any]:
    t0 = time.perf_counter()
    report = cnc_model.build_cnc_report(extract_cnc_geometry(workplane))
    report["analysisMs"] = int((time.perf_counter() - t0) * 1000)
    return report


def analyze_step_cnc(step_bytes: bytes) -> Dict[str, Any]:
    """STEP bytes → /analyze-cnc response body. Raises ValueError on bad input."""
    with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as f:
        f.write(step_bytes)
        step_path = f.name
    try:
        shape = cq.importers.importStep(step_path)
        if shape is None or shape.val() is None:
            raise ValueError("STEP file could not be parsed. Ensure it is a valid AP214/AP242 file.")
        return analyze_cnc_workplane(shape)
    finally:
        try:
            os.unlink(step_path)
        except OSError:
            pass
