"""Face classification, thickness and V2 bend detection in main.py. Needs CadQuery
plus the web dependencies main.py imports — run inside the service image or any
environment with them installed:

    python -m unittest discover -s tests -v

Current OCP only accepts a TopoDS_Face/TopoDS_Edge where older builds took the
generic TopoDS_Shape that explorers return; these guard the downcasts.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import cadquery as cq
    import main
    from test_sheet_analysis import _bent_l_bracket, _plate_with_holes
except Exception:  # pragma: no cover - local dev without OCCT / web deps
    cq = None


def _sharp_l_bracket():
    """2 mm sheet folded with no bend radius: 100 mm base + 40 mm flange, 50 mm wide"""
    base = cq.Workplane("XY").box(100, 50, 2, centered=False)
    return base.union(cq.Workplane("XY").box(2, 50, 40, centered=False))


@unittest.skipIf(cq is None, "CadQuery / OCP or main.py dependencies not installed")
class FaceClassificationTest(unittest.TestCase):
    def test_explorer_faces_are_classified(self):
        plate = main._shape_to_faces(cq.Workplane("XY").box(200, 100, 2).val().wrapped)
        self.assertEqual([main._classify_face_type(f) for f in plate], ["PLANE"] * 6)

        cyl = main._shape_to_faces(cq.Workplane("XY").cylinder(10, 5).val().wrapped)
        self.assertIn("CYLINDER", [main._classify_face_type(f) for f in cyl])

    def test_detect_thickness_of_a_plain_plate(self):
        self.assertEqual(main._detect_thickness(cq.Workplane("XY").box(200, 100, 2)), 2.0)

    def test_bracket_validates_as_sheet_metal(self):
        part = _bent_l_bracket()
        thickness = main._detect_thickness(part)
        self.assertEqual(thickness, 2.0)
        self.assertTrue(main._validate_sheet_metal_shape(part, thickness))


@unittest.skipIf(cq is None, "CadQuery / OCP or main.py dependencies not installed")
class BendDetectionV2Test(unittest.TestCase):
    def _detect_without_fallback(self, part):
        with mock.patch.object(
            main, "_extract_bend_features", wraps=main._extract_bend_features
        ) as legacy:
            bends = main._extract_bend_features_v2(part)
        legacy.assert_not_called()
        return bends

    def test_radiused_bend_is_found_on_the_cylinder(self):
        bends = self._detect_without_fallback(_bent_l_bracket())

        self.assertEqual(len(bends), 1)
        b = bends[0]
        self.assertAlmostEqual(b["angle"], 90.0, delta=1)
        # Inner radius, whichever skin represents the bend
        self.assertAlmostEqual(b["radius"], 1.9, places=2)
        self.assertAlmostEqual(main._line_length_from_bend(b), 50.0, delta=0.5)
        # Line on the bend axis, running across the 50 mm width
        self.assertAlmostEqual(b["start"]["x"], 3.9, places=2)
        self.assertAlmostEqual(b["start"]["z"], 3.9, places=2)

    def test_sharp_bend_is_found_on_the_edge(self):
        bends = self._detect_without_fallback(_sharp_l_bracket())

        self.assertEqual(len(bends), 1)
        self.assertAlmostEqual(bends[0]["angle"], 90.0, delta=1)
        self.assertAlmostEqual(main._line_length_from_bend(bends[0]), 50.0, delta=0.5)

    def test_outline_and_hole_edges_are_not_bends(self):
        self.assertEqual(main._extract_bend_features_v2(_plate_with_holes()), [])


if __name__ == "__main__":
    unittest.main()
