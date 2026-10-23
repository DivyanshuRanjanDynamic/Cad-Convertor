"""FDM print geometry for /internal/analyze-print. The mesh maths is pure Python;
the STEP/T-shape cases need CadQuery and skip without it.

    python -m unittest discover -s tests -v
"""

import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import print_analysis

try:
    import cadquery as cq
except ImportError:  # pragma: no cover
    cq = None


def _cube_triangles(s: float):
    p = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]  # outward
    tris = []
    for a, b, c, d in quads:
        tris.append((p[a], p[b], p[c]))
        tris.append((p[a], p[c], p[d]))
    return tris


def _binary_stl(tris) -> bytes:
    out = bytearray(b"\0" * 80) + struct.pack("<I", len(tris))
    for t in tris:
        out += struct.pack("<3f", 0, 0, 0)
        for v in t:
            out += struct.pack("<3f", *v)
        out += b"\0\0"
    return bytes(out)


def _workplane_stl(wp) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
        path = f.name
    try:
        cq.exporters.export(wp, path, exportType="STL", tolerance=0.01, angularTolerance=0.1)
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(path)


class MeshMetricsTest(unittest.TestCase):
    def test_cube_volume_area_and_no_support(self):
        r = print_analysis.analyze_print_file("cube.stl", _binary_stl(_cube_triangles(20)))
        self.assertAlmostEqual(r["volumeMm3"], 8000, delta=0.5)
        self.assertAlmostEqual(r["surfaceAreaMm2"], 2400, delta=0.5)
        self.assertEqual(len(r["orientations"]), 6)
        for o in r["orientations"]:
            self.assertEqual(o["sizeMm"], {"x": 20, "y": 20, "z": 20})
            self.assertEqual(o["supportVolumeMm3"], 0)

    def test_inward_wound_mesh_measures_the_same(self):
        inward = [(a, c, b) for a, b, c in _cube_triangles(20)]
        r = print_analysis.analyze_mesh(inward)
        self.assertAlmostEqual(r["volumeMm3"], 8000, delta=0.5)

    def test_ascii_stl_and_obj(self):
        ascii_stl = "solid c\n" + "".join(
            "facet normal 0 0 0\nouter loop\n" + "".join(f"vertex {x} {y} {z}\n" for x, y, z in t) + "endloop\nendfacet\n"
            for t in _cube_triangles(10)
        ) + "endsolid c\n"
        self.assertAlmostEqual(print_analysis.analyze_print_file("c.stl", ascii_stl.encode())["volumeMm3"], 1000, delta=0.1)

        obj = (
            "v 0 0 0\nv 10 0 0\nv 10 10 0\nv 0 10 0\nv 0 0 10\nv 10 0 10\nv 10 10 10\nv 0 10 10\n"
            "f 1 4 3 2\nf 5 6 7 8\nf 1 2 6 5\nf 2 3 7 6\nf 3 4 8 7\nf 4 1 5 8\n"
        )
        self.assertAlmostEqual(print_analysis.analyze_print_file("c.obj", obj.encode())["volumeMm3"], 1000, delta=0.1)

    def test_rejects_open_meshes_and_unknown_types(self):
        with self.assertRaises(ValueError):
            print_analysis.analyze_mesh(_cube_triangles(10)[:2])
        with self.assertRaises(ValueError):
            print_analysis.analyze_print_file("part.iges", b"")


@unittest.skipIf(cq is None, "CadQuery / OCP not installed")
class OverhangTest(unittest.TestCase):
    def _t_shape(self):
        stem = cq.Workplane("XY").box(10, 10, 20, centered=(True, True, False))
        cap = cq.Workplane("XY").workplane(offset=20).box(40, 10, 5, centered=(True, True, False))
        return stem.union(cap)

    def test_overhang_needs_support_only_when_it_hangs(self):
        r = print_analysis.analyze_print_file("t.stl", _workplane_stl(self._t_shape()))
        by_up = {o["up"]: o for o in r["orientations"]}
        # Cap underside (40×10 − 10×10 = 300 mm²) hangs 20 mm above the bed
        self.assertAlmostEqual(by_up["+z"]["supportVolumeMm3"], 6000, delta=60)
        # Upside down the cap sits on the bed and the stem points up: nothing hangs
        self.assertLess(by_up["-z"]["supportVolumeMm3"], 1)
        self.assertEqual(by_up["+z"]["sizeMm"]["z"], 25)
        self.assertAlmostEqual(r["volumeMm3"], 10 * 10 * 20 + 40 * 10 * 5, delta=1)

    def test_step_input_is_tessellated(self):
        with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as f:
            path = f.name
        try:
            cq.exporters.export(self._t_shape(), path)
            with open(path, "rb") as fh:
                r = print_analysis.analyze_print_file("t.step", fh.read())
        finally:
            os.unlink(path)
        self.assertAlmostEqual(r["volumeMm3"], 4000, delta=5)


if __name__ == "__main__":
    unittest.main()
