"""DXF profiles for /internal/analyze-sheet. Needs CadQuery plus the web dependencies
main.py imports — run inside the service image or any environment with them:

    python -m unittest discover -s tests -v
"""

import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import cadquery as cq
    import main
except Exception:  # pragma: no cover - local dev without OCCT / web deps
    cq = None


def _dxf_bytes(shape) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "part.dxf")
        cq.exporters.export(shape, path)
        with open(path, "rb") as f:
            return f.read()


@unittest.skipIf(cq is None, "CadQuery / OCP or main.py dependencies not installed")
class DxfSheetTest(unittest.TestCase):
    def test_plate_profile_measures_area_cut_length_and_pierces(self):
        plate = (
            cq.Workplane("XY").box(200, 100, 2)
            .faces(">Z").workplane().rect(160, 60, forConstruction=True).vertices().hole(4.2)
            .faces(">Z").workplane().rect(20, 10).cutThruAll()
        )
        r = main.analyze_dxf_sheet(_dxf_bytes(plate.section()))

        self.assertTrue(r["profileOnly"])
        self.assertTrue(r["isFlat"])
        self.assertEqual(r["bends"], [])
        hole_area = 4 * math.pi * 2.1 ** 2
        self.assertAlmostEqual(r["flatAreaMm2"], 200 * 100 - hole_area - 20 * 10, delta=1.0)
        self.assertAlmostEqual(r["cutLengthMm"], 600 + 4 * math.pi * 4.2 + 60, delta=1.0)
        self.assertEqual(r["pierces"], 6)
        self.assertEqual(r["holes"], [{"diameterMm": 4.2, "count": 4}])
        self.assertEqual((r["boundingBox"]["x"], r["boundingBox"]["y"]), (200.0, 100.0))

    def test_open_lines_are_rejected_with_a_fix(self):
        lines = cq.Workplane("XY").polyline([(0, 0), (50, 0), (50, 30)])
        with self.assertRaisesRegex(ValueError, "closed"):
            main.analyze_dxf_sheet(_dxf_bytes(lines))

    def test_flat_pattern_layers_join_and_open_bend_lines_are_ignored(self):
        # CAD flat-pattern export: outline and holes on separate layers, open bend lines
        doc = main.ezdxf.new()
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True,
                           dxfattribs={"layer": "OUTER_PROFILES"})
        msp.add_circle((20, 50), 2.1, dxfattribs={"layer": "INTERIOR_PROFILES"})
        # SolidWorks style: drawn with extrusion (0, 0, -1), so the OCS X is mirrored
        msp.add_circle((-180, 50), 2.1, dxfattribs={"layer": "INTERIOR_PROFILES", "extrusion": (0, 0, -1)})
        msp.add_line((0, 30), (200, 30), dxfattribs={"layer": "BEND"})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "flat.dxf")
            doc.saveas(path)
            with open(path, "rb") as f:
                r = main.analyze_dxf_sheet(f.read())

        self.assertAlmostEqual(r["flatAreaMm2"], 200 * 100 - 2 * math.pi * 2.1 ** 2, delta=1.0)
        self.assertEqual(r["pierces"], 3)  # outline + 2 holes
        self.assertEqual(r["holes"], [{"diameterMm": 4.2, "count": 2}])
        self.assertEqual((r["boundingBox"]["x"], r["boundingBox"]["y"]), (200.0, 100.0))

    def test_garbage_is_rejected(self):
        with self.assertRaises(ValueError):
            main.analyze_dxf_sheet(b"not a dxf")

    def test_sheet_endpoint_routes_dxf_to_the_profile_analysis(self):
        self.assertEqual(main._analyze_sheet_file.__name__, "_analyze_sheet_file")
        self.assertIn(".dxf", main.SHEET_EXTENSIONS)
        plate = cq.Workplane("XY").rect(80, 40).extrude(1)
        r = main._analyze_sheet_file("Bracket.DXF", _dxf_bytes(plate.section()))
        self.assertTrue(r["profileOnly"])
        self.assertAlmostEqual(r["flatAreaMm2"], 3200.0, delta=0.5)


if __name__ == "__main__":
    unittest.main()
