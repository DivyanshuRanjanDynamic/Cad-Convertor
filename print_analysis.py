"""
MechHub FDM print geometry — /internal/analyze-print.

From a closed mesh (STL / OBJ, or a STEP tessellated with CadQuery):

  • volume and surface area (divergence theorem / triangle areas)
  • for each of the six axis-up orientations: the part's size on the bed and
    the support volume a slicer would need — every face overhanging more than
    45° and not resting on the bed needs support down to the bed:
        support ≈ Σ projected area × height above the bed

Pure Python (no numpy) so it runs in the service image as-is.
"""

import math
import os
import struct
import tempfile
import time
from typing import Dict, List, Sequence, Tuple

VERSION = "print-1"
OVERHANG_COS = math.cos(math.radians(45))  # steeper than 45° from vertical needs support
BED_CONTACT_MM = 0.2                       # faces within one layer of the bed sit on it
MAX_TRIANGLES = 3_000_000

Vec3 = Tuple[float, float, float]
Triangle = Tuple[Vec3, Vec3, Vec3]


# ── Parsers ──────────────────────────────────────────────────────────────────

def parse_stl(data: bytes) -> List[Triangle]:
    if len(data) >= 84:
        (count,) = struct.unpack_from("<I", data, 80)
        if 84 + 50 * count == len(data):
            if count > MAX_TRIANGLES:
                raise ValueError("Mesh is too detailed to price instantly.")
            tris: List[Triangle] = []
            for i in range(count):
                v = struct.unpack_from("<9f", data, 84 + 50 * i + 12)
                tris.append(((v[0], v[1], v[2]), (v[3], v[4], v[5]), (v[6], v[7], v[8])))
            return tris
    # ASCII: every three "vertex" lines make a facet
    verts: List[Vec3] = []
    for line in data.decode("utf-8", errors="ignore").splitlines():
        parts = line.strip().split()
        if len(parts) == 4 and parts[0].lower() == "vertex":
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
    if len(verts) < 3:
        raise ValueError("STL file contains no triangles.")
    return [(verts[i], verts[i + 1], verts[i + 2]) for i in range(0, len(verts) - 2, 3)]


def parse_obj(data: bytes) -> List[Triangle]:
    verts: List[Vec3] = []
    tris: List[Triangle] = []
    for line in data.decode("utf-8", errors="ignore").splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        if parts[0] == "v" and len(parts) >= 4:
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif parts[0] == "f" and len(parts) >= 4:
            idx = []
            for token in parts[1:]:
                i = int(token.split("/")[0])
                idx.append(i - 1 if i > 0 else len(verts) + i)
            for k in range(1, len(idx) - 1):  # fan-triangulate polygons
                tris.append((verts[idx[0]], verts[idx[k]], verts[idx[k + 1]]))
    if not tris:
        raise ValueError("OBJ file contains no faces.")
    return tris


def tessellate_step(step_bytes: bytes, tolerance: float = 0.1) -> List[Triangle]:
    import cadquery as cq  # only needed for STEP input

    with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as f:
        f.write(step_bytes)
        path = f.name
    try:
        shape = cq.importers.importStep(path)
        if shape is None or shape.val() is None:
            raise ValueError("STEP file could not be parsed. Ensure it is a valid AP214/AP242 file.")
        solids = sorted(shape.solids().vals(), key=lambda s: -s.Volume())
        if not solids:
            raise ValueError("STEP file contains no solid bodies.")
        vertices, triangles = solids[0].tessellate(tolerance, 0.5)
        pts = [(v.x, v.y, v.z) for v in vertices]
        return [(pts[a], pts[b], pts[c]) for a, b, c in triangles]
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ── Metrics ──────────────────────────────────────────────────────────────────

def analyze_mesh(triangles: Sequence[Triangle]) -> Dict:
    t0 = time.perf_counter()
    if len(triangles) < 4:
        raise ValueError("The model has too few faces to be a closed solid.")

    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for tri in triangles:
        for p in tri:
            for a in range(3):
                if p[a] < lo[a]:
                    lo[a] = p[a]
                if p[a] > hi[a]:
                    hi[a] = p[a]

    signed_volume = 0.0
    for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in triangles:
        signed_volume += (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)) / 6.0
    # Inward-wound meshes give a negative volume — flip their normals to point out.
    volume = abs(signed_volume)
    if volume <= 1e-6:
        raise ValueError("The model is not a closed solid (zero volume).")
    outward = 1.0 if signed_volume > 0 else -1.0

    area = 0.0
    # support_up[a] / support_down[a]: support volume with axis a pointing up / down
    support_up = [0.0, 0.0, 0.0]
    support_down = [0.0, 0.0, 0.0]
    for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in triangles:
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        double_area = math.sqrt(nx * nx + ny * ny + nz * nz)
        if double_area < 1e-12:
            continue
        tri_area = double_area / 2.0
        area += tri_area
        centroid = ((ax + bx + cx) / 3.0, (ay + by + cy) / 3.0, (az + bz + cz) / 3.0)
        scale = outward / double_area
        normal = (nx * scale, ny * scale, nz * scale)
        for a in range(3):
            n_a = normal[a]
            if n_a < -OVERHANG_COS:  # faces downward when axis a points up
                h = centroid[a] - lo[a]
                if h > BED_CONTACT_MM:
                    support_up[a] += tri_area * -n_a * h
            elif n_a > OVERHANG_COS:  # faces downward when axis a points down
                h = hi[a] - centroid[a]
                if h > BED_CONTACT_MM:
                    support_down[a] += tri_area * n_a * h

    size = [hi[a] - lo[a] for a in range(3)]
    names = ("x", "y", "z")
    orientations = []
    for a in range(3):
        others = [size[b] for b in range(3) if b != a]
        for sign, support in (("+", support_up[a]), ("-", support_down[a])):
            orientations.append({
                "up": sign + names[a],
                "sizeMm": {"x": round(others[0], 3), "y": round(others[1], 3), "z": round(size[a], 3)},
                "supportVolumeMm3": round(support, 1),
            })

    return {
        "version": VERSION,
        "units": "mm",
        "volumeMm3": round(volume, 1),
        "surfaceAreaMm2": round(area, 1),
        "triangleCount": len(triangles),
        "orientations": orientations,
        "analysisMs": int((time.perf_counter() - t0) * 1000),
    }


def analyze_print_file(file_name: str, content: bytes) -> Dict:
    """File bytes → /internal/analyze-print response body. Raises ValueError on bad input."""
    ext = os.path.splitext(file_name.lower())[1]
    if ext in (".step", ".stp"):
        triangles = tessellate_step(content)
    elif ext == ".stl":
        triangles = parse_stl(content)
    elif ext == ".obj":
        triangles = parse_obj(content)
    else:
        raise ValueError(f'Unsupported file type "{ext}". Upload STEP, STL or OBJ.')
    return analyze_mesh(triangles)
