"""Sheet-metal geometry for /internal/analyze-sheet. Needs CadQuery — run inside the
service image (see test_cnc_analysis.py) or any environment with cadquery installed:

    python -m unittest discover -s tests -v
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import cadquery as cq
    import sheet_analysis
except ImportError:  # pragma: no cover - local dev without OCCT
    cq = None


def _plate_with_holes(slot: bool = False):
    plate = (
        cq.Workplane("XY").box(200, 100, 2)
        .faces(">Z").workplane().rect(160, 60, forConstruction=True).vertices().hole(4.2)
    )
    if slot:
        plate = plate.faces(">Z").workplane().rect(20, 10).cutThruAll()
    return plate


def _bent_l_bracket():
    """2 mm sheet: 100 mm base + 40 mm flange, one 90° bend (inner R1.9), 50 mm wide"""
    base = cq.Workplane("XY").box(100, 50, 2, centered=False)
    flange = cq.Workplane("XY").box(2, 50, 40, centered=False)
    part = base.union(flange)
    part = part.edges(cq.selectors.NearestToPointSelector((0, 25, 0))).fillet(3.9)
    return part.edges(cq.selectors.NearestToPointSelector((2, 25, 2))).fillet(1.9)


@unittest.skipIf(cq is None, "CadQuery / OCP not installed")
class SheetAnalysisTest(unittest.TestCase):
    def test_flat_plate_area_cut_length_pierces_and_holes(self):
        r = sheet_analysis.analyze_sheet_workplane(_plate_with_holes(), [])

        self.assertAlmostEqual(r["thicknessMm"], 2.0, places=2)
        hole_area = 4 * math.pi * 2.1 ** 2
        self.assertAlmostEqual(r["flatAreaMm2"], 200 * 100 - hole_area, delta=1.0)
        self.assertAlmostEqual(r["cutLengthMm"], 600 + 4 * math.pi * 4.2, delta=1.0)
        self.assertEqual(r["pierces"], 5)
        self.assertEqual(r["holes"], [{"diameterMm": 4.2, "count": 4}])
        self.assertTrue(r["isFlat"])
        self.assertEqual(r["bodyCount"], 1)
        self.assertEqual(r["boundingBox"], {"x": 200.0, "y": 100.0, "z": 2.0})

    def test_internal_cutout_adds_a_pierce_and_its_perimeter(self):
        base = sheet_analysis.analyze_sheet_workplane(_plate_with_holes(), [])
        slotted = sheet_analysis.analyze_sheet_workplane(_plate_with_holes(slot=True), [])
        self.assertEqual(slotted["pierces"], base["pierces"] + 1)
        self.assertAlmostEqual(slotted["cutLengthMm"] - base["cutLengthMm"], 60.0, delta=1.0)
        # A rectangular cut-out is not a round hole
        self.assertEqual(slotted["holes"], base["holes"])

    def test_bends_are_reported_with_their_line_length(self):
        bends = [{
            "start": {"x": 0, "y": 0, "z": 0},
            "end": {"x": 0, "y": 100, "z": 0},
            "angle": 90.0,
            "radius": 2.0,
        }]
        r = sheet_analysis.analyze_sheet_workplane(_plate_with_holes(), bends)
        self.assertEqual(r["bends"], [{"angleDeg": 90.0, "radiusMm": 2.0, "lengthMm": 100.0}])
        self.assertFalse(r["isFlat"])

    def test_bent_bracket_through_the_real_bend_detector(self):
        try:
            import main  # needs the web dependencies too
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"main.py not importable here: {exc}")
        part = _bent_l_bracket()
        r = sheet_analysis.analyze_sheet_workplane(part, main._extract_bend_features_v2(part))

        self.assertAlmostEqual(r["thicknessMm"], 2.0, places=2)
        self.assertEqual(len(r["bends"]), 1)
        self.assertAlmostEqual(r["bends"][0]["angleDeg"], 90.0, delta=1)
        self.assertAlmostEqual(r["bends"][0]["lengthMm"], 50.0, delta=0.5)
        # Developed blank ≈ (flat legs + neutral-axis arc) × width
        legs = (100 - 3.9) + (40 - 3.9)
        arc = math.pi / 2 * (1.9 + 1.0)
        self.assertAlmostEqual(r["flatAreaMm2"], (legs + arc) * 50, delta=10)
        self.assertAlmostEqual(r["cutLengthMm"], 2 * (legs + arc) + 2 * 50, delta=2)
        self.assertEqual(r["pierces"], 1)

    def test_rejects_parts_that_are_not_sheet(self):
        with self.assertRaises(ValueError):
            sheet_analysis.analyze_sheet_workplane(cq.Workplane("XY").sphere(20), [])
        # 1 mm is far thinner than the solid → edge area would be negative
        with self.assertRaises(ValueError):
            sheet_analysis.analyze_sheet_workplane(_plate_with_holes(), [], thickness_mm=1.0)


if __name__ == "__main__":
    unittest.main()
