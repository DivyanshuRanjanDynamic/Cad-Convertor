"""
MechHub CAD Converter — STEP → STL microservice
FastAPI + CadQuery (OCCT Python bindings)

Endpoints:
  GET  /health          → liveness probe
  POST /convert         → multipart STEP file → JSON (base64 STL + metadata)
  POST /analyze-bends   → multipart STEP file → JSON (bend analysis + flat pattern SVG)

Response is 100% contract-compatible with the existing ConversionResult
TypeScript interface in the MechHub studio frontend.
"""

import base64
from collections import Counter
import io
import math
import os
import struct
import tempfile
import time
import traceback
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any, Tuple

import cadquery as cq
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ── Feature Extraction ────────────────────────────────────────────────────────

def _extract_hole_features(shape: cq.Workplane):
    """
    Industrial-grade hole detection.
    Analyzes cylindrical faces, validates axes, and calculates true geometric depth.
    """
    holes = []
    try:
        # 1. Focus on cylindrical surfaces (standard representation for holes)
        cylindrical_faces = shape.faces(cq.selectors.TypeSelector("CYLINDER")).vals()
        
        seen_centers = [] # Deduplicate by center + axis to avoid overlaps
        
        for face in cylindrical_faces:
            # Safely get the OCCT surface from the face
            # We use the BRepAdaptor to handle different OCCT version pointer types
            try:
                from OCP.BRepAdaptor import BRepAdaptor_Surface
                adaptor = BRepAdaptor_Surface(face.wrapped)
                surf = adaptor.Surface()
                
                center = face.Center()
                axis = surf.Cylinder().Position().Direction()
                radius = surf.Cylinder().Radius()
            except Exception:
                # Fallback to direct access if OCP structure differs
                try:
                    surf = face.wrapped.Surface().Value()
                    center = face.Center()
                    axis = surf.Position().Direction()
                    radius = surf.Radius()
                except:
                    continue # Skip faces that don't follow the cylinder schema
            
            # Simple heuristic to avoid non-hole cylinders (like curved edges of a plate)
            # True holes usually have a closed circular perimeter at their ends
            if radius < 0.1: continue # Ignore microscopic artifacts
            
            # 2. Differentiate between hole (negative) and boss (positive)
            # For accurate CAD, we check the normal vector relative to the part center
            # However, in OpenCASCADE, the surface orientation property is already a strong hint.
            # Here we use the bounding box extent along the axis for depth calculation.
            
            depth = 0
            # Projects edges onto the axis to find the max span (depth)
            points = []
            for edge in face.Edges():
                points.append(edge.Center())
            
            if len(points) >= 2:
                # Calculate span along axis
                axis_vec = cq.Vector(axis.X(), axis.Y(), axis.Z())
                proj_values = [cq.Vector(p.x, p.y, p.z).dot(axis_vec) for p in points]
                depth = max(proj_values) - min(proj_values)

            # Fallback depth calculation using surface area / circumference if necessary
            if depth < 0.01:
                depth = face.Area() / (2 * 3.14159 * radius)

            # 3. Deduplication: Check if we've already processed this cylinder
            is_dup = False
            for sc in seen_centers:
                dist = (center - sc).Length
                if dist < 0.5: # 0.5mm tolerance
                    is_dup = True
                    break
            
            if is_dup: continue
            seen_centers.append(center)

            holes.append({
                "center": {"x": round(center.x, 3), "y": round(center.y, 3), "z": round(center.z, 3)},
                "radius": round(radius, 3),
                "depth": round(depth, 3),
                "normal": {"x": round(axis.X(), 3), "y": round(axis.Y(), 3), "z": round(axis.Z(), 3)}
            })
            
    except Exception as e:
        print(f"[CAD Converter] Senior Hole Detection Warning: {e}")
    
    return holes

def _extract_bend_features(shape: cq.Workplane):
    """
    Industrial-grade sheet-metal bend extraction.
    - Accurately projects bend centerline axis lengths.
    - Calculates true bend angles between adjacent planar faces.
    - Detects fold direction (UP/DOWN) based on geometric convexity.
    """
    bends = []
    try:
        # 1. Isolate the part's centroid for direction reference
        part_centroid = shape.val().Center()
        
        # 2. Project cylindrical surfaces (the 'fillet' of the bend)
        cylindrical_faces = shape.faces(cq.selectors.TypeSelector("CYLINDER")).vals()
        print(f"[Bending] Analyzing {len(cylindrical_faces)} potential cylindrical faces...")
        
        seen_bend_centers = [] # Store center vectors for deduplication
        
        for idx, face in enumerate(cylindrical_faces):
            # 2.1 Filter Lateral Edges (The edges that define the 'width' of the bend)
            # In some exports, these can be 'LINE' or 'BSPLINE'.
            # We skip 'CIRCLE' and 'ELLIPSE' and 'OFFSET' as these are the arcs/ends of the cylinder.
            lateral_edges = [e for e in face.Edges() if e.geomType() not in ("CIRCLE", "ELLIPSE", "OFFSET")]
            
            if len(lateral_edges) < 1:
                continue
            
            try:
                # 3. Extract pure geometry data via OCCT BRepAdaptor
                # Fallback mechanism for different OCCT version pointer types
                try:
                    from OCP.BRepAdaptor import BRepAdaptor_Surface
                    adaptor = BRepAdaptor_Surface(face.wrapped)
                    surf = adaptor.Surface()
                    
                    axis_pos = surf.Cylinder().Position().Location()
                    axis_dir = surf.Cylinder().Position().Direction()
                    radius = surf.Cylinder().Radius()
                    
                    # Get parametric range for angle calculation
                    u_min, u_max = adaptor.FirstUParameter(), adaptor.LastUParameter()
                except Exception:
                    # Fallback to direct access if BRepAdaptor fails or structure differs
                    surf = face.wrapped.Surface().Value()
                    axis_pos = surf.Position().Location()
                    axis_dir = surf.Position().Direction()
                    radius = surf.Radius()
                    u_min, u_max = 0, 0 # Fallback will use face area logic
                
                if radius < 0.2 or radius > 50: continue # Filter micro-fillets or massive curves
                
                # 4. Find the true length of the bend axis
                # We use the built-in Length() method of the edge which is standard in OCP.
                length = sum(e.Length() for e in lateral_edges) / len(lateral_edges)
                
                # 4.1 Simple Spatial Deduplication
                # Prevents double-counting the inner and outer face of the same bend mesh.
                face_center = face.Center()
                is_duplicate = False
                for sc in seen_bend_centers:
                    dist = (face_center - sc).Length
                    if dist < (radius * 1.5 + 1.0): # Within proximity of the same bend
                        is_duplicate = True
                        break
                if is_duplicate: continue
                seen_bend_centers.append(face_center)
                
                # Construct start/end points on the axis
                vec_axis = cq.Vector(axis_dir.X(), axis_dir.Y(), axis_dir.Z())
                base_pos = cq.Vector(axis_pos.X(), axis_pos.Y(), axis_pos.Z())
                
                # Shift to the face center along the axis
                face_proj = cq.Vector(face_center.x, face_center.y, face_center.z).dot(vec_axis)
                base_proj = base_pos.dot(vec_axis)
                
                face_center_on_axis = base_pos + vec_axis * (face_proj - base_proj)
                
                start = face_center_on_axis - vec_axis * (length / 2.0)
                end = face_center_on_axis + vec_axis * (length / 2.0)
                
                # 5. Angle Calculation
                # Standard sheet metal: Angle = arc_length / radius (in radians)
                if abs(u_max - u_min) > 0.001:
                    angle_Rad = abs(u_max - u_min)
                else:
                    # Fallback angle calculation via arc length / radius
                    # .Area() and .Length() are both methods in OCCT/OCP/CadQuery wrappers
                    angle_Rad = face.Area() / (radius * length)
                
                angle_deg = round(angle_Rad * (180.0 / 3.14159), 1)
                
                # Hem detection: 180 degree bends are allowed.
                if angle_deg < 5 or angle_deg > 185: continue 
                
                # 6. Directional Detection (UP vs DOWN)
                # Some imported cylinders fail face.NormalAt(center) for partial trims.
                # Use robust normal sampling with midpoint fallback.
                face_center_v = cq.Vector(face_center.x, face_center.y, face_center.z)
                face_normal = _get_face_normal_at_point(face.wrapped, face_center_v)
                if face_normal is None:
                    face_normal = cq.Vector(0, 0, 1)
                vec_to_centroid = (
                    cq.Vector(part_centroid.x, part_centroid.y, part_centroid.z)
                    - face_center_v
                )

                # Industrial convexity check
                direction = "DOWN" if face_normal.dot(vec_to_centroid) > 0 else "UP"
                
                bends.append({
                    "start": {"x": round(start.x, 3), "y": round(start.y, 3), "z": round(start.z, 3)},
                    "end": {"x": round(end.x, 3), "y": round(end.y, 3), "z": round(end.z, 3)},
                    "angle": min(angle_deg, 180.0),
                    "direction": direction,
                    "radius": round(radius, 3)
                })
            except Exception as e:
                print(f"[Bending] Face #{idx} skipped: {e}")
                continue # Skip malformed bend faces
                
    except Exception as e:
        print(f"[CAD Converter] Senior Bend Analysis Error: {e}")
        
    return bends


# ── V2 Bend Analysis (Topology-Based) ─────────────────────────────────────────
# This is the new production algorithm used by the /analyze-bends endpoint.
# The original _extract_bend_features() above is preserved for backwards
# compatibility with the /convert endpoint.

def _get_face_normal_at_point(face, point):
    """
    Get the outward-pointing normal of a face at a given point.
    Returns cq.Vector or None.
    """
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.gp import gp_Pnt
        from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf

        adaptor = BRepAdaptor_Surface(face)
        surf = adaptor.Surface()

        # Project point onto surface to get (u, v) parameters
        projector = GeomAPI_ProjectPointOnSurf(
            gp_Pnt(point.x, point.y, point.z), surf
        )
        if projector.NbPoints() < 1:
            return None

        u, v = projector.Parameters(1)

        # Evaluate normal at (u, v)
        from OCP.BRepGProp import BRepGProp_Face
        prop = BRepGProp_Face(face)
        from OCP.gp import gp_Pnt as gp_Pnt2, gp_Vec
        pnt = gp_Pnt2()
        normal = gp_Vec()
        prop.Normal(u, v, pnt, normal)

        mag = normal.Magnitude()
        if mag < 1e-10:
            return None

        return cq.Vector(normal.X() / mag, normal.Y() / mag, normal.Z() / mag)
    except Exception:
        # Fallback: evaluate normal at face UV midpoint.
        try:
            from OCP.BRepAdaptor import BRepAdaptor_Surface
            from OCP.BRepGProp import BRepGProp_Face
            from OCP.gp import gp_Pnt as gp_Pnt2, gp_Vec

            adaptor = BRepAdaptor_Surface(face)
            u = 0.5 * (adaptor.FirstUParameter() + adaptor.LastUParameter())
            v = 0.5 * (adaptor.FirstVParameter() + adaptor.LastVParameter())
            prop = BRepGProp_Face(face)
            pnt = gp_Pnt2()
            normal = gp_Vec()
            prop.Normal(u, v, pnt, normal)
            mag = normal.Magnitude()
            if mag < 1e-10:
                return None
            return cq.Vector(normal.X() / mag, normal.Y() / mag, normal.Z() / mag)
        except Exception:
            return None


def _classify_face_type(face):
    """Universal Face Classifier (V3) using curvature analysis (c1, c2)."""
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.GeomLProp import GeomLProp_SLProps
        from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder

        adaptor = BRepAdaptor_Surface(face)
        stype = adaptor.GetType()

        # Phase 1: Direct Analytical Check
        if stype == GeomAbs_Plane: return 'PLANE'
        if stype == GeomAbs_Cylinder: return 'CYLINDER'

        # Phase 2: Differential Geometry Check (Curvature)
        # Sample at center (0.5) and quadrant points to be robust.
        samples = [(0.5, 0.5), (0.2, 0.2), (0.8, 0.8), (0.2, 0.8), (0.8, 0.2)]
        max_c1, max_c2 = 0.0, 0.0
        
        for u, v in samples:
            u_p = adaptor.FirstUParameter() + u * (adaptor.LastUParameter() - adaptor.FirstUParameter())
            v_p = adaptor.FirstVParameter() + v * (adaptor.LastVParameter() - adaptor.FirstVParameter())
            props = GeomLProp_SLProps(adaptor.Surface(), u_p, v_p, 2, 1e-7)
            if props.IsNormalDefined():
                c1 = abs(props.MaxCurvature())
                c2 = abs(props.MinCurvature())
                max_c1 = max(max_c1, c1)
                max_c2 = max(max_c2, c2)

        # Classification thresholds (Refined for Industrial fallback)
        if max_c1 < 5e-4: return 'PLANE' 
        if max_c2 < 5e-4: return 'CYLINDER'
        
        if max_c1 < 1e-2:
            print(f"[DebugV3] Face potential miss: c1={max_c1:.6f}, c2={max_c2:.6f}")
        
        return 'OTHER'
    except Exception:
        return 'OTHER'


def _get_cylinder_radius(face):
    """Extract cylinder radius from a cylindrical face."""
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        return BRepAdaptor_Surface(face).Cylinder().Radius()
    except Exception:
        return None


def _edge_midpoint(edge):
    """Get the midpoint of an edge as a cq.Vector."""
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        curve = BRepAdaptor_Curve(edge)
        u_mid = (curve.FirstParameter() + curve.LastParameter()) / 2.0
        pnt = curve.Value(u_mid)
        return cq.Vector(pnt.X(), pnt.Y(), pnt.Z())
    except Exception:
        return None


def _shape_to_faces(shape_wrapped):
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    out = []
    exp = TopExp_Explorer(shape_wrapped, TopAbs_FACE)
    while exp.More():
        out.append(exp.Current())
        exp.Next()
    return out


def _list_from_toptools_shape_list(shape_list) -> List[Any]:
    out = []
    try:
        it = shape_list.Iterator()
        while it.More():
            out.append(it.Value())
            it.Next()
    except Exception:
        try:
            out = list(shape_list)
        except Exception:
            out = []
    return out


def _face_key(face) -> str:
    return str(hash(face))


def _canonical_face_pair(f1, f2) -> Tuple[str, str]:
    a = _face_key(f1)
    b = _face_key(f2)
    return (a, b) if a <= b else (b, a)


def _edge_length(edge) -> float:
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        from OCP.GCPnts import GCPnts_AbscissaPoint

        curve = BRepAdaptor_Curve(edge)
        return float(
            GCPnts_AbscissaPoint.Length_s(
                curve, curve.FirstParameter(), curve.LastParameter()
            )
        )
    except Exception:
        return 0.0


def _edge_endpoints(edge) -> Optional[Tuple[cq.Vector, cq.Vector]]:
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        curve = BRepAdaptor_Curve(edge)
        p1 = curve.Value(curve.FirstParameter())
        p2 = curve.Value(curve.LastParameter())
        return (
            cq.Vector(p1.X(), p1.Y(), p1.Z()),
            cq.Vector(p2.X(), p2.Y(), p2.Z()),
        )
    except Exception:
        return None


def _face_midpoint(face) -> Optional[cq.Vector]:
    try:
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        adaptor = BRepAdaptor_Surface(face)
        u = 0.5 * (adaptor.FirstUParameter() + adaptor.LastUParameter())
        v = 0.5 * (adaptor.FirstVParameter() + adaptor.LastVParameter())
        p = adaptor.Value(u, v)
        return cq.Vector(p.X(), p.Y(), p.Z())
    except Exception:
        return None


def _default_radius_from_thickness(thickness: Optional[float]) -> float:
    if thickness and thickness > 0:
        return round(max(0.0, thickness * 0.5), 3)
    return 0.0


def _are_collinear(v1: cq.Vector, v2: cq.Vector, tol: float = 0.02) -> bool:
    if v1.Length < 1e-9 or v2.Length < 1e-9:
        return False
    a = v1.normalized()
    b = v2.normalized()
    return abs(abs(a.dot(b)) - 1.0) <= tol


def _endpoint_distance(a: cq.Vector, b: cq.Vector) -> float:
    return (a - b).Length


def _merge_collinear_segments(segments: List[Dict[str, Any]], tol: float = 1.0) -> Dict[str, Any]:
    if len(segments) == 1:
        return segments[0]

    # Build a simple path by greedily stitching nearest endpoints.
    used = [False] * len(segments)
    chain = [segments[0]]
    used[0] = True
    while True:
        extended = False
        head_s = chain[0]["_start_v"]
        head_e = chain[0]["_end_v"]
        tail_s = chain[-1]["_start_v"]
        tail_e = chain[-1]["_end_v"]
        for i, seg in enumerate(segments):
            if used[i]:
                continue
            a = seg["_start_v"]
            b = seg["_end_v"]
            if min(_endpoint_distance(a, head_s), _endpoint_distance(b, head_s)) <= tol:
                if _endpoint_distance(b, head_s) < _endpoint_distance(a, head_s):
                    seg = {**seg, "_start_v": b, "_end_v": a}
                chain.insert(0, seg)
                used[i] = True
                extended = True
                break
            if min(_endpoint_distance(a, tail_e), _endpoint_distance(b, tail_e)) <= tol:
                if _endpoint_distance(a, tail_e) > _endpoint_distance(b, tail_e):
                    seg = {**seg, "_start_v": b, "_end_v": a}
                chain.append(seg)
                used[i] = True
                extended = True
                break
            # Allow reversing first or last segment if needed.
            if min(_endpoint_distance(a, head_e), _endpoint_distance(b, head_e)) <= tol:
                if _endpoint_distance(b, head_e) < _endpoint_distance(a, head_e):
                    seg = {**seg, "_start_v": b, "_end_v": a}
                chain.insert(0, seg)
                used[i] = True
                extended = True
                break
            if min(_endpoint_distance(a, tail_s), _endpoint_distance(b, tail_s)) <= tol:
                if _endpoint_distance(a, tail_s) > _endpoint_distance(b, tail_s):
                    seg = {**seg, "_start_v": b, "_end_v": a}
                chain.append(seg)
                used[i] = True
                extended = True
                break
        if not extended:
            break

    first = chain[0]["_start_v"]
    last = chain[-1]["_end_v"]
    merged = dict(chain[0])
    merged["_start_v"] = first
    merged["_end_v"] = last
    merged["_edge_len"] = _endpoint_distance(first, last)
    merged["radius"] = round(sum(seg["radius"] for seg in chain) / len(chain), 3)
    return merged


def _extract_bend_features_v2(shape: cq.Workplane) -> List[Dict[str, Any]]:
    """
    Edge-adjacency bend detection:
    bend edge = edge with exactly two adjacent faces whose normals are not parallel.
    """
    try:
        wrapped = shape.val().wrapped
        edge_face_map = _build_edge_face_map(wrapped)
        thickness = _detect_thickness(shape)

        face_types = Counter(_classify_face_type(face) for face in _shape_to_faces(wrapped))
        non_sheet_faces = sum(
            count for ft, count in face_types.items() if ft not in ("PLANE", "CYLINDER")
        )
        if non_sheet_faces > 0:
            print(f"[BendV2] Non-sheet candidate faces detected: {dict(face_types)}")
            # Keep detection running for mixed/dirty STEP models.
            # We later filter by edge-level angular checks.

        if not _validate_sheet_metal_shape(shape, thickness):
            # Do not fail hard; some valid parts still contain auxiliary geometry.
            print("[BendV2] Sheet-metal validation failed; continuing with tolerant detection.")

        flat_tol_deg = 2.0
        angle_tol_group_deg = 1.5
        parallel_dot_tol = math.cos(math.radians(flat_tol_deg))
        candidate_rows: List[Dict[str, Any]] = []
        skipped_non_manifold = 0
        skipped_boundary = 0
        face_normal_cache: Dict[str, Optional[cq.Vector]] = {}

        for i in range(1, edge_face_map.Extent() + 1):
            edge = edge_face_map.FindKey(i)
            faces = _list_from_toptools_shape_list(edge_face_map.FindFromIndex(i))
            if len(faces) < 2:
                skipped_boundary += 1
                continue
            if len(faces) > 2:
                skipped_non_manifold += 1
                continue

            f1, f2 = faces[0], faces[1]
            edge_pts = _edge_endpoints(edge)
            if not edge_pts:
                continue
            p1, p2 = edge_pts
            edge_len = _endpoint_distance(p1, p2)
            if edge_len < 0.5:
                continue

            edge_mid = cq.Vector(
                (p1.x + p2.x) * 0.5,
                (p1.y + p2.y) * 0.5,
                (p1.z + p2.z) * 0.5,
            )
            k1 = _face_key(f1)
            k2 = _face_key(f2)
            n1 = face_normal_cache.get(k1)
            n2 = face_normal_cache.get(k2)
            if n1 is None:
                n1 = _get_face_normal_at_point(f1, edge_mid)
                face_normal_cache[k1] = n1
            if n2 is None:
                n2 = _get_face_normal_at_point(f2, edge_mid)
                face_normal_cache[k2] = n2
            if not n1 or not n2:
                continue

            dot = max(-1.0, min(1.0, n1.dot(n2)))
            if abs(dot) >= parallel_dot_tol:
                continue

            theta = math.degrees(math.acos(dot))
            if abs(theta - 180.0) <= flat_tol_deg or abs(theta) <= flat_tol_deg:
                continue

            bend_angle = 180.0 - theta
            if bend_angle < 2.0:
                continue

            edge_dir = (p2 - p1).normalized() if edge_len > 1e-9 else cq.Vector(1, 0, 0)
            cross = n1.cross(n2)
            signed = cross.dot(edge_dir)
            direction = "UP" if signed >= 0 else "DOWN"

            r1 = _get_cylinder_radius(f1)
            r2 = _get_cylinder_radius(f2)
            cyl_radii = [r for r in (r1, r2) if r is not None and r > 1e-6]
            radius = round(min(cyl_radii), 3) if cyl_radii else _default_radius_from_thickness(thickness)

            face_pair = _canonical_face_pair(f1, f2)
            candidate_rows.append(
                {
                    "_face_pair": face_pair,
                    "_start_v": p1,
                    "_end_v": p2,
                    "_edge_dir": edge_dir,
                    "_edge_len": edge_len,
                    "angle": round(bend_angle, 1),
                    "direction": direction,
                    "radius": radius,
                }
            )

        if len(candidate_rows) == 0:
            # Some STEP exports come in with fragmented/non-shared topology where
            # edge->face adjacency is effectively empty. Fall back to the legacy
            # cylindrical-face strategy instead of returning a hard 0 bends.
            print("[BendV2] No adjacency candidates; falling back to legacy bend detection.")
            return _extract_bend_features(shape)

        # Group collinear splits: same adjacent faces + similar angle + same direction + connected.
        grouped: Dict[Tuple[str, str, str, int], List[Dict[str, Any]]] = {}
        for row in candidate_rows:
            angle_bucket = int(round(row["angle"] / angle_tol_group_deg))
            key = (row["_face_pair"][0], row["_face_pair"][1], row["direction"], angle_bucket)
            grouped.setdefault(key, []).append(row)

        merged_rows: List[Dict[str, Any]] = []
        for rows in grouped.values():
            rows_sorted = sorted(
                rows,
                key=lambda r: (
                    round(r["_start_v"].x, 3),
                    round(r["_start_v"].y, 3),
                    round(r["_start_v"].z, 3),
                ),
            )
            consumed = [False] * len(rows_sorted)
            for i, base in enumerate(rows_sorted):
                if consumed[i]:
                    continue
                batch = [base]
                consumed[i] = True
                for j in range(i + 1, len(rows_sorted)):
                    if consumed[j]:
                        continue
                    cand = rows_sorted[j]
                    if abs(cand["angle"] - base["angle"]) > angle_tol_group_deg:
                        continue
                    if not _are_collinear(base["_edge_dir"], cand["_edge_dir"]):
                        continue
                    # Must touch within tolerance to be same bend line.
                    d = min(
                        _endpoint_distance(base["_start_v"], cand["_start_v"]),
                        _endpoint_distance(base["_start_v"], cand["_end_v"]),
                        _endpoint_distance(base["_end_v"], cand["_start_v"]),
                        _endpoint_distance(base["_end_v"], cand["_end_v"]),
                    )
                    if d <= 1.0:
                        batch.append(cand)
                        consumed[j] = True

                merged_rows.append(_merge_collinear_segments(batch))

        # Stable ordering for frontend index-based hover coupling.
        merged_rows.sort(
            key=lambda r: (
                round(r["_start_v"].x, 3),
                round(r["_start_v"].y, 3),
                round(r["_start_v"].z, 3),
                round(r["_end_v"].x, 3),
                round(r["_end_v"].y, 3),
                round(r["_end_v"].z, 3),
            )
        )

        bends: List[Dict[str, Any]] = []
        for row in merged_rows:
            bends.append(
                {
                    "start": {
                        "x": round(row["_start_v"].x, 3),
                        "y": round(row["_start_v"].y, 3),
                        "z": round(row["_start_v"].z, 3),
                    },
                    "end": {
                        "x": round(row["_end_v"].x, 3),
                        "y": round(row["_end_v"].y, 3),
                        "z": round(row["_end_v"].z, 3),
                    },
                    "angle": row["angle"],
                    "direction": row["direction"],
                    "radius": round(row["radius"], 3),
                }
            )

        print(
            f"[BendV2] candidates={len(candidate_rows)} merged={len(bends)} "
            f"boundarySkipped={skipped_boundary} nonManifoldSkipped={skipped_non_manifold}"
        )
        return bends
    except Exception as e:
        print(f"[BendV2] Fatal: {e}")
        traceback.print_exc()
        return []


def _build_edge_face_map(shape_wrapped):
    """Build edge→face adjacency map from any B-Rep shape."""
    from OCP.TopExp import TopExp
    from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    edge_face_map = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape_wrapped, TopAbs_EDGE, TopAbs_FACE, edge_face_map)
    return edge_face_map


def _detect_thickness(shape: cq.Workplane) -> Optional[float]:
    """Detect sheet metal thickness by analyzing parallel planar face pairs."""
    print("[Thickness] Starting detection...")
    try:
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_FACE
        wrapped = shape.val().wrapped
        
        planar_faces = []
        exp = TopExp_Explorer(wrapped, TopAbs_FACE)
        while exp.More():
            face = exp.Current()
            if _classify_face_type(face) == 'PLANE':
                from OCP.BRepAdaptor import BRepAdaptor_Surface
                adaptor = BRepAdaptor_Surface(face)
                plane = adaptor.Plane()
                normal = plane.Axis().Direction()
                loc = plane.Location()
                planar_faces.append({
                    'face': face,
                    'normal': cq.Vector(normal.X(), normal.Y(), normal.Z()),
                    'location': cq.Vector(loc.X(), loc.Y(), loc.Z()),
                })
            exp.Next()
        
        print(f"[Thickness] Testing {len(planar_faces)} planar faces.")
        if len(planar_faces) < 2: return None

        thicknesses = []
        for i in range(len(planar_faces)):
            for j in range(i + 1, len(planar_faces)):
                n1 = planar_faces[i]['normal']
                n2 = planar_faces[j]['normal']
                if abs(n1.dot(n2)) > 0.99:  # Parallel
                    dist = abs(n1.dot(planar_faces[i]['location']) - n1.dot(planar_faces[j]['location']))
                    if 0.3 < dist < 15.0:
                        thicknesses.append(round(dist, 2))

        if not thicknesses: return None
        from collections import Counter
        most_common = Counter(thicknesses).most_common(1)[0][0]
        print(f"[Thickness] Detected: {most_common}mm")
        return most_common
    except Exception as e:
        print(f"[Thickness] Error: {e}")
        return None


def _validate_sheet_metal_shape(shape: cq.Workplane, thickness: Optional[float]) -> bool:
    """
    Basic sheet-metal validity checks:
    - only planar/cylindrical surfaces
    - reasonably constant thickness from parallel planar face pairs
    """
    if thickness is None or thickness <= 0:
        return False

    try:
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_FACE
        from OCP.BRepAdaptor import BRepAdaptor_Surface

        wrapped = shape.val().wrapped
        planar_faces = []
        exp = TopExp_Explorer(wrapped, TopAbs_FACE)
        while exp.More():
            face = exp.Current()
            ftype = _classify_face_type(face)
            if ftype not in ("PLANE", "CYLINDER"):
                return False
            if ftype == "PLANE":
                plane = BRepAdaptor_Surface(face).Plane()
                normal = plane.Axis().Direction()
                loc = plane.Location()
                planar_faces.append(
                    {
                        "normal": cq.Vector(normal.X(), normal.Y(), normal.Z()).normalized(),
                        "location": cq.Vector(loc.X(), loc.Y(), loc.Z()),
                    }
                )
            exp.Next()

        if len(planar_faces) < 2:
            return False

        pair_dists = []
        for i in range(len(planar_faces)):
            for j in range(i + 1, len(planar_faces)):
                n1 = planar_faces[i]["normal"]
                n2 = planar_faces[j]["normal"]
                if abs(abs(n1.dot(n2)) - 1.0) <= 0.01:
                    dist = abs(
                        n1.dot(planar_faces[i]["location"]) - n1.dot(planar_faces[j]["location"])
                    )
                    if 0.3 < dist < 15.0:
                        pair_dists.append(dist)

        if not pair_dists:
            return False

        tol = max(0.25, thickness * 0.35)
        matches = [d for d in pair_dists if abs(d - thickness) <= tol]
        return len(matches) >= max(1, int(0.4 * len(pair_dists)))
    except Exception:
        return False


def _generate_flat_pattern_svg(shape: cq.Workplane, bends: List[Dict], thickness: Optional[float]) -> Optional[Dict[str, Any]]:
    """
    Generate a 2D top-down projection of the part as an SVG string,
    with bend lines overlaid.

    Uses OCCT's hidden line removal (HLR) algorithm to project the 3D shape
    onto the XY plane, producing a clean 2D outline.

    Returns:
        {
            "svg": "<svg>...</svg>",
            "viewBox": { "minX": ..., "minY": ..., "width": ..., "height": ... },
            "bendLines": [
                { "x1": ..., "y1": ..., "x2": ..., "y2": ..., "angle": ..., "direction": "UP"|"DOWN", "radius": ... }
            ]
        }
    or None on failure.
    """
    try:
        from OCP.gp import gp_Dir, gp_Ax2, gp_Pnt
        from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
        from OCP.HLRAlgo import HLRAlgo_Projector
        from OCP.BRepBndLib import BRepBndLib
        from OCP.Bnd import Bnd_Box
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_EDGE
        from OCP.BRepAdaptor import BRepAdaptor_Curve

        from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE
        from OCP.TopExp import TopExp_Explorer
        
        solid = shape.val().wrapped

        # Determine the best projection direction (V3 Orientation Logic)
        # We find the Largest Planar Face Normal. For a sheet metal part, 
        # this is almost always the main surface or base.
        proj_dir = gp_Dir(0, 0, 1)
        up_dir = gp_Dir(0, 1, 0)
        
        max_area = 0.0
        face_exp = TopExp_Explorer(solid, TopAbs_FACE)
        while face_exp.More():
            f = face_exp.Current()
            if _classify_face_type(f) == 'PLANE':
                from OCP.BRepGProp import BRepGProp
                from OCP.GProp import GProp_GProps
                props = GProp_GProps()
                BRepGProp.SurfaceProperties_s(f, props)
                area = props.Mass()
                if area > max_area:
                    from OCP.BRepAdaptor import BRepAdaptor_Surface
                    adaptor = BRepAdaptor_Surface(f)
                    norm = adaptor.Plane().Axis().Direction()
                    # Orientation refinement
                    proj_dir = gp_Dir(norm.X(), norm.Y(), norm.Z())
                    if abs(proj_dir.Z()) < 0.9:
                        up_dir = gp_Dir(0, 0, 1)
                    else:
                        up_dir = gp_Dir(0, 1, 0)
                    max_area = area
            face_exp.Next()
            
        print(f"[DebugV3] Selected projection: ({proj_dir.X():.2f}, {proj_dir.Y():.2f}, {proj_dir.Z():.2f}) - Ref Area: {max_area:.1f}")

        # Run HLR (Hidden Line Removal) projection
        hlr = HLRBRep_Algo()
        hlr.Add(solid)

        origin = gp_Pnt(0, 0, 0)
        ax2 = gp_Ax2(origin, proj_dir, up_dir)
        projector = HLRAlgo_Projector(ax2)

        hlr.Projector(projector)
        hlr.Update()
        hlr.Hide()

        hlr_shape = HLRBRep_HLRToShape(hlr)

        # Get visible sharp edges (the main outline)
        visible_sharp = hlr_shape.VCompound()
        visible_smooth = hlr_shape.Rg1LineVCompound()
        visible_outline = hlr_shape.OutLineVCompound()

        # Collect all projected edge coordinates
        def edges_to_paths(compound):
            """Convert a TopoDS compound of projected edges into SVG path segments."""
            paths = []
            if compound is None or compound.IsNull():
                return paths
            explorer = TopExp_Explorer(compound, TopAbs_EDGE)
            while explorer.More():
                edge = explorer.Current()
                try:
                    curve = BRepAdaptor_Curve(edge)
                    n_pts = max(2, int((_edge_length(edge) or 1.0) / 0.5))  # sample every 0.5mm
                    n_pts = min(n_pts, 200)  # cap
                    u_start = curve.FirstParameter()
                    u_end = curve.LastParameter()
                    pts = []
                    for k in range(n_pts + 1):
                        u = u_start + (u_end - u_start) * k / n_pts
                        p = curve.Value(u)
                        pts.append((round(p.X(), 3), round(p.Y(), 3)))
                    if len(pts) >= 2:
                        paths.append(pts)
                except Exception:
                    pass
                explorer.Next()
            return paths

        outline_paths = edges_to_paths(visible_sharp)
        outline_paths += edges_to_paths(visible_smooth)
        outline_paths += edges_to_paths(visible_outline)

        if not outline_paths:
            print("[FlatPattern] No visible edges after HLR projection.")
            return None

        # Calculate bounding box of projected edges
        all_x = [p[0] for path in outline_paths for p in path]
        all_y = [p[1] for path in outline_paths for p in path]
        min_x, max_x = min(all_x), max(all_x)
        min_y, max_y = min(all_y), max(all_y)

        width = max_x - min_x
        height = max_y - min_y

        if width < 0.01 or height < 0.01:
            print("[FlatPattern] Degenerate projection (zero area).")
            return None

        # Add padding (10%)
        pad_x = width * 0.1
        pad_y = height * 0.1
        vb_x = min_x - pad_x
        vb_y = min_y - pad_y
        vb_w = width + 2 * pad_x
        vb_h = height + 2 * pad_y

        # Build SVG
        svg_parts = []
        svg_parts.append(
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="{vb_x:.3f} {vb_y:.3f} {vb_w:.3f} {vb_h:.3f}" '
            f'width="100%" height="100%" '
            f'style="background:#0f172a">'
        )

        # Outline edges
        for path in outline_paths:
            d = f"M {path[0][0]:.3f} {path[0][1]:.3f}"
            for pt in path[1:]:
                d += f" L {pt[0]:.3f} {pt[1]:.3f}"
            svg_parts.append(
                f'<path d="{d}" fill="none" stroke="#94a3b8" '
                f'stroke-width="{max(0.1, vb_w * 0.002):.4f}" '
                f'stroke-linecap="round" stroke-linejoin="round" />'
            )

        bb = shape.val().BoundingBox()
        lens = [bb.xlen, bb.ylen, bb.zlen]
        thin_axis = lens.index(min(lens))

        # Project bend lines onto the same 2D plane
        bend_lines_2d = []
        for bend in bends:
            s = bend["start"]
            e = bend["end"]

            # Project 3D coords → 2D by dropping the thin axis
            if thin_axis == 0:  # X is thin → project onto YZ
                x1, y1 = s["y"], s["z"]
                x2, y2 = e["y"], e["z"]
            elif thin_axis == 1:  # Y is thin → project onto XZ
                x1, y1 = s["x"], s["z"]
                x2, y2 = e["x"], e["z"]
            else:  # Z is thin → project onto XY
                x1, y1 = s["x"], s["y"]
                x2, y2 = e["x"], e["y"]

            bl = {
                "x1": round(x1, 3), "y1": round(y1, 3),
                "x2": round(x2, 3), "y2": round(y2, 3),
                "angle": bend["angle"],
                "direction": bend["direction"],
                "radius": bend["radius"],
            }
            bend_lines_2d.append(bl)

            # Draw bend line on SVG
            color = "#3B82F6" if bend["direction"] == "UP" else "#F97316"
            dash = "" if bend["direction"] == "UP" else f'stroke-dasharray="{vb_w * 0.01:.3f} {vb_w * 0.005:.3f}"'
            sw = max(0.15, vb_w * 0.003)
            svg_parts.append(
                f'<line x1="{x1:.3f}" y1="{y1:.3f}" x2="{x2:.3f}" y2="{y2:.3f}" '
                f'stroke="{color}" stroke-width="{sw:.4f}" '
                f'stroke-linecap="round" opacity="0.85" '
                f'{dash} '
                f'data-angle="{bend["angle"]}" data-direction="{bend["direction"]}" '
                f'data-radius="{bend["radius"]}" />'
            )

        svg_parts.append('</svg>')
        svg_string = '\n'.join(svg_parts)

        result = {
            "svg": svg_string,
            "viewBox": {
                "minX": round(vb_x, 3),
                "minY": round(vb_y, 3),
                "width": round(vb_w, 3),
                "height": round(vb_h, 3),
            },
            "bendLines": bend_lines_2d,
        }

        print(f"[FlatPattern] Generated SVG: {len(outline_paths)} edges, "
              f"{len(bend_lines_2d)} bend lines, "
              f"viewBox={vb_w:.1f}x{vb_h:.1f}")
        return result

    except Exception as e:
        print(f"[FlatPattern] Generation error: {e}")
        traceback.print_exc()
        return None


def _analyze_step_bends(step_bytes: bytes) -> dict:
    """
    Full bend analysis pipeline:
    1. Import STEP → CadQuery shape
    2. Detect bends via V2 topology algorithm
    3. Detect sheet metal thickness
    4. Generate 2D flat pattern SVG
    5. Return structured result
    """
    t0 = time.perf_counter()

    with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as f:
        f.write(step_bytes)
        step_path = f.name

    try:
        shape = cq.importers.importStep(step_path)
        if shape is None or shape.val() is None:
            raise ValueError("STEP file could not be parsed.")

        # 1. Bend detection
        bends = _extract_bend_features_v2(shape)

        # 2. Thickness detection
        thickness = _detect_thickness(shape)

        # 3. Flat pattern (2D projection + bend lines)
        flat_pattern = _generate_flat_pattern_svg(shape, bends, thickness)

        # 4. Bounding box
        bb = shape.val().BoundingBox()
        bounding_box = {
            "x": round(bb.xlen, 3),
            "y": round(bb.ylen, 3),
            "z": round(bb.zlen, 3),
        }

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        print(
            f"[analyze-bends] OK — {len(bends)} bends, "
            f"thickness={thickness}, {elapsed_ms} ms"
        )

        return {
            "bends": bends,
            "detectedThickness": thickness,
            "flatPattern": flat_pattern,
            "boundingBox": bounding_box,
        }

    finally:
        _safe_unlink(step_path)

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB
ACCEPTED_EXTENSIONS = {".step", ".stp"}

# Tessellation quality — balance speed vs mesh detail.
# Linear deflection 0.1 mm gives good quality for most mechanical parts.
# Set via env vars to tune without redeploy.
LINEAR_TOLERANCE: float = float(os.getenv("CAD_LINEAR_TOLERANCE", "0.1"))
ANGULAR_TOLERANCE: float = float(os.getenv("CAD_ANGULAR_TOLERANCE", "0.5"))


# ── Application lifecycle ─────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warm up CadQuery / OCCT kernel on startup so the first request is fast."""
    print("[CAD Converter] Warming up OCCT kernel…")
    try:
        # Create a tiny dummy box — forces OCCT to JIT-initialise.
        _ = cq.Workplane("XY").box(1, 1, 1)
        print("[CAD Converter] OCCT kernel ready ✓")
    except Exception as exc:  # pragma: no cover
        print(f"[CAD Converter] Kernel warm-up warning: {exc}")
    yield
    print("[CAD Converter] Shutting down.")


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="MechHub CAD Converter",
    description="STEP → STL conversion service powered by CadQuery / OpenCASCADE",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow the MechHub web app (production + local dev) to call directly.
# Restrict `allow_origins` to specific domains in production for tighter security.
allowed_origins = os.getenv(
    "CORS_ORIGINS",
    "https://mechhub.in,https://www.mechhub.in,https://studio.mechhub.in,http://localhost:3000,http://localhost:9002",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in allowed_origins],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _count_triangles(stl_bytes: bytes) -> int:
    """Parse STL header to extract triangle count. Handles binary and ASCII."""
    if len(stl_bytes) < 84:
        return 0
    
    # ASCII STL check: starts with 'solid'
    if stl_bytes.startswith(b"solid"):
        # For simplicity, return 0 for ASCII or do a basic line count
        # Three.js will still render it fine, this is just for the metadata log
        return 0
        
    (count,) = struct.unpack_from("<I", stl_bytes, 80)
    return count


def _convert_step_to_stl(step_bytes: bytes) -> dict:
    """Core conversion: raw STEP bytes → dict with stl/boundingBox/triangleCount.

    Uses CadQuery's importers/exporters which wrap the same OCCT B-Rep engine
    as Onshape, FreeCAD, and commercial CAD tools.

    Returns:
        {
            "stl":          str,   # base64-encoded binary STL
            "triangleCount": int,
            "boundingBox":  {"x": float, "y": float, "z": float}  # mm
        }

    Raises:
        ValueError: if the STEP file is corrupt or unreadable.
    """
    t0 = time.perf_counter()

    # Write STEP bytes to a temp file — CadQuery's STEP importer works on paths.
    with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as step_f:
        step_f.write(step_bytes)
        step_path = step_f.name

    stl_path = step_path.replace(".step", ".stl")

    try:
        # ── Import STEP ──────────────────────────────────────────────────────
        shape = cq.importers.importStep(step_path)

        # ── Validate ─────────────────────────────────────────────────────────
        if shape is None or shape.val() is None:
            raise ValueError(
                "STEP file could not be parsed. "
                "Ensure it is a valid AP214/AP242 file."
            )

        # ── Tessellate + export binary STL ───────────────────────────────────
        cq.exporters.export(
            shape,
            stl_path,
            exportType=cq.exporters.ExportTypes.STL,
            tolerance=LINEAR_TOLERANCE,
            angularTolerance=ANGULAR_TOLERANCE,
        )

        # ── Read STL ─────────────────────────────────────────────────────────
        with open(stl_path, "rb") as stl_f:
            stl_bytes_out = stl_f.read()

        # ── Bounding box (mm) ─────────────────────────────────────────────────
        bb = shape.val().BoundingBox()
        bounding_box = {
            "x": round(bb.xlen, 3),
            "y": round(bb.ylen, 3),
            "z": round(bb.zlen, 3),
        }

        triangle_count = _count_triangles(stl_bytes_out)
        stl_b64 = base64.b64encode(stl_bytes_out).decode("utf-8")

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        print(
            f"[convert] OK — {triangle_count:,} triangles, "
            f"bbox={bounding_box}, {elapsed_ms} ms"
        )

        # ── Feature Extraction ───────────────────────────────────────────────
        hole_features = _extract_hole_features(shape)
        # Use V2 bend detection by default for better reliability
        bend_features = _extract_bend_features_v2(shape)
        thickness = _detect_thickness(shape)
        flat_pattern = _generate_flat_pattern_svg(shape, bend_features, thickness)

        return {
            "stl": stl_b64,
            "triangleCount": triangle_count,
            "boundingBox": bounding_box,
            "holes": hole_features,
            "bends": bend_features,
            "detectedThickness": thickness,
            "flatPattern": flat_pattern,
        }

    finally:
        # Always clean up temp files, even if conversion fails.
        _safe_unlink(step_path)
        _safe_unlink(stl_path)


def _safe_unlink(path: str) -> None:
    """Delete a file silently if it exists."""
    try:
        os.unlink(path)
    except OSError:
        pass


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", tags=["ops"])
def root() -> dict:
    """Root endpoint — provides service status and quick links."""
    return {
        "service": "MechHub CAD Converter",
        "status": "online",
        "version": app.version,
        "endpoints": {
            "health": "/health",
            "convert": "/convert [POST]"
        }
    }


@app.get("/health", tags=["ops"])
def health() -> dict:
    """Liveness probe — Railway / Docker health check hits this."""
    return {"status": "ok", "version": app.version}


@app.post("/convert", tags=["conversion"])
async def convert(file: UploadFile = File(...)) -> JSONResponse:
    """Convert an uploaded STEP file to a base64-encoded binary STL.

    Accepts: multipart/form-data with field name `file`.
    Returns:
    ```json
    {
      "stl": "<base64 binary STL>",
      "triangleCount": 12345,
      "boundingBox": { "x": 100.0, "y": 50.0, "z": 25.0 }
    }
    ```
    """
    # ── Validate file extension ───────────────────────────────────────────────
    filename: str = file.filename or "upload"
    ext = os.path.splitext(filename.lower())[1]
    if ext not in ACCEPTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f'Unsupported file type "{ext}". Please upload a .step or .stp file.',
        )

    # ── Read and size-check ───────────────────────────────────────────────────
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Empty file received.")
    if len(content) > MAX_FILE_SIZE:
        size_mb = len(content) / 1_048_576
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size_mb:.1f} MB). Maximum is 100 MB.",
        )

    # ── Convert & Analyze ───────────────────────────────────────────────────
    try:
        result = _convert_step_to_stl(content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        print(f"[convert] Unexpected error: {exc}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Conversion failed: {exc}",
        ) from exc

    return JSONResponse(content=result)


