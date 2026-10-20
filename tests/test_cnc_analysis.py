"""Integration tests for OCCT geometry extraction. Needs CadQuery — run inside the
service image:

    docker run --rm -v "$PWD:/app" -w /app cadquery/cadquery:latest \
        /opt/conda/envs/cq/bin/python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import cadquery as cq
    import cnc_analysis
except ImportError:  # pragma: no cover - local dev without OCCT
    cq = None


@unittest.skipIf(cq is None, "CadQuery / OCP not installed")
class CncAnalysisTest(unittest.TestCase):
    def test_pocketed_plate_with_holes(self):
        pocket = cq.Workplane("XY").box(40, 20, 10).edges("|Z").fillet(3).translate((0, 0, 5))
        part = (
            cq.Workplane("XY").box(100, 60, 20)
            .faces(">Z").workplane().rect(80, 40, forConstruction=True).vertices().hole(6)
            .cut(pocket)
        )
        r = cnc_analysis.analyze_cnc_workplane(part)

        self.assertEqual(r["process"]["recommended"], "cnc_machining_3axis")
        self.assertEqual(r["stock"]["dimensionsMm"], {"x": 105.0, "y": 65.0, "z": 25.0})
        self.assertEqual(r["setups"]["count"], 2)
        self.assertEqual(sorted(r["setups"]["directions"]), ["+Z", "-Z"])
        self.assertEqual(r["features"]["holeCount"], 4)
        hole = r["features"]["holes"][0]
        self.assertAlmostEqual(hole["diameterMm"], 6.0, places=2)
        self.assertAlmostEqual(hole["depthMm"], 20.0, places=2)
        self.assertTrue(hole["through"])
        self.assertAlmostEqual(r["features"]["minInternalRadiusMm"], 3.0, places=2)
        self.assertEqual(r["features"]["sharpInternalCorners"], 0)
        expected_removed = 105 * 65 * 25 - r["part"]["volumeMm3"]
        self.assertAlmostEqual(r["stock"]["removedVolumeMm3"], expected_removed, delta=1)

    def test_sharp_pocket_corners_flagged(self):
        part = cq.Workplane("XY").box(60, 40, 20).faces(">Z").workplane().rect(30, 20).cutBlind(-8)
        r = cnc_analysis.analyze_cnc_workplane(part)
        self.assertEqual(r["features"]["sharpInternalCorners"], 4)
        self.assertIn("SHARP_INTERNAL_CORNER", [i["code"] for i in r["dfm"]])

    def test_stepped_shaft_is_turned(self):
        part = cq.Workplane("XY").circle(15).extrude(50).faces(">Z").workplane().circle(10).extrude(30)
        r = cnc_analysis.analyze_cnc_workplane(part)

        self.assertEqual(r["process"]["recommended"], "cnc_turning")
        self.assertFalse(r["process"]["secondaryMilling"])
        self.assertGreater(r["process"]["turnableFraction"], 0.99)
        self.assertEqual(r["stock"]["shape"], "round_bar")
        self.assertEqual(r["stock"]["dimensionsMm"], {"diameter": 32.0, "length": 85.0})

    def test_plate_with_bore_is_not_turned(self):
        part = cq.Workplane("XY").box(100, 100, 10).faces(">Z").workplane().hole(50)
        r = cnc_analysis.analyze_cnc_workplane(part)
        self.assertEqual(r["process"]["recommended"], "cnc_machining_3axis")

    def test_t_slot_undercut_detected(self):
        slot = cq.Workplane("XY").box(90, 10, 20).translate((0, 0, 10))
        foot = cq.Workplane("XY").box(90, 24, 6).translate((0, 0, 3))
        part = cq.Workplane("XY").box(80, 50, 40).translate((0, 0, -10)).cut(slot).cut(foot.translate((0, 0, -10)))
        r = cnc_analysis.analyze_cnc_workplane(part)
        self.assertGreater(r["setups"]["inaccessibleAreaMm2"], 0)
        self.assertIn("UNDERCUT", [i["code"] for i in r["dfm"]])

    def test_thin_wall_measured(self):
        outer = cq.Workplane("XY").box(40, 40, 30)
        part = outer.faces(">Z").shell(-0.6)
        r = cnc_analysis.analyze_cnc_workplane(part)
        self.assertAlmostEqual(r["features"]["minWallThicknessMm"], 0.6, delta=0.05)


if __name__ == "__main__":
    unittest.main()
