"""Unit tests for the kernel-free CNC costing model. Run: python -m unittest discover -s tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cnc_model  # noqa: E402


class StockSelectionTest(unittest.TestCase):
    def test_block_adds_allowance_and_snaps_to_standard_sizes(self):
        stock = cnc_model.select_block_stock((100.0, 60.0, 20.0))
        self.assertEqual(stock["shape"], "block")
        self.assertEqual(stock["dimensionsMm"], {"x": 105.0, "y": 65.0, "z": 25.0})
        self.assertAlmostEqual(stock["volumeMm3"], 105 * 65 * 25)

    def test_block_thin_axis_uses_next_plate_thickness(self):
        stock = cnc_model.select_block_stock((50.0, 8.0, 50.0))
        # 8 + 5 = 13 → next stocked plate is 16 mm
        self.assertEqual(stock["dimensionsMm"]["y"], 16.0)

    def test_bar_rounds_up_diameter_and_adds_length_allowance(self):
        bar = cnc_model.select_bar_stock(30.0, 80.0)
        self.assertEqual(bar["dimensionsMm"], {"diameter": 32.0, "length": 85.0})

    def test_bar_beyond_catalogue_is_none(self):
        self.assertIsNone(cnc_model.select_bar_stock(400.0, 50.0))


class SetupPlanningTest(unittest.TestCase):
    def test_minimum_cover(self):
        samples = [
            {"directions": ["+Z"], "areaMm2": 10},
            {"directions": ["-Z"], "areaMm2": 10},
            {"directions": ["+Z", "-Z", "+X"], "areaMm2": 10},
        ]
        plan = cnc_model.plan_setups(samples)
        self.assertEqual(plan["count"], 2)
        self.assertEqual(sorted(plan["directions"]), ["+Z", "-Z"])
        self.assertEqual(plan["inaccessibleAreaMm2"], 0)

    def test_ties_prefer_face_milling_over_side_milling(self):
        samples = [
            {"directions": ["+Z"], "floorDirections": ["+Z"], "areaMm2": 2400},
            {"directions": ["-Z", "+Y"], "floorDirections": ["-Z"], "areaMm2": 2400},
            {"directions": ["+Y", "+Z", "-Z"], "floorDirections": ["+Y"], "areaMm2": 1200},
        ]
        self.assertEqual(sorted(cnc_model.plan_setups(samples)["directions"]), ["+Z", "-Z"])

    def test_unreachable_samples_reported(self):
        plan = cnc_model.plan_setups([
            {"directions": ["+Z"], "areaMm2": 10},
            {"directions": [], "areaMm2": 4},
        ])
        self.assertEqual(plan["count"], 1)
        self.assertEqual(plan["inaccessibleAreaMm2"], 4)


class ProcessChoiceTest(unittest.TestCase):
    def test_turning_thresholds(self):
        self.assertEqual(cnc_model.choose_process(0.95, True, 0, 100),
                         {"recommended": "cnc_turning", "secondaryMilling": False})
        self.assertEqual(cnc_model.choose_process(0.7, True, 0, 100),
                         {"recommended": "cnc_turning", "secondaryMilling": True})
        self.assertEqual(cnc_model.choose_process(0.95, False, 0, 100)["recommended"],
                         "cnc_machining_3axis")

    def test_undercuts_push_to_five_axis(self):
        self.assertEqual(cnc_model.choose_process(0.0, False, 5, 100)["recommended"],
                         "cnc_machining_5axis")


class MachiningTimeTest(unittest.TestCase):
    def _times(self, **overrides):
        args = dict(
            process="cnc_machining_3axis", secondary_milling=False,
            removed_volume_mm3=50000.0, surface_area_mm2=20000.0, non_turned_area_mm2=0.0,
            holes=[], min_internal_radius_mm=None, setups=2, tool_count=2,
        )
        args.update(overrides)
        return cnc_model.estimate_machining_time(**args)

    def test_harder_materials_take_longer(self):
        t = self._times()
        self.assertLess(t["aluminium"]["cycleMin"], t["mild_steel"]["cycleMin"])
        self.assertLess(t["mild_steel"]["cycleMin"], t["stainless"]["cycleMin"])

    def test_aluminium_roughing_matches_mrr(self):
        t = self._times()
        self.assertAlmostEqual(t["aluminium"]["roughingMin"], 50.0 / 40.0, places=2)

    def test_holes_add_drilling_and_are_not_double_counted(self):
        holes = [{"diameterMm": 6.0, "depthMm": 20.0, "through": True}] * 4
        base = self._times()["aluminium"]
        drilled = self._times(holes=holes)["aluminium"]
        self.assertGreater(drilled["drillingMin"], 0)
        self.assertLess(drilled["roughingMin"], base["roughingMin"])

    def test_small_radius_slows_finishing(self):
        slow = self._times(min_internal_radius_mm=0.5)["aluminium"]["finishingMin"]
        fast = self._times(min_internal_radius_mm=5.0)["aluminium"]["finishingMin"]
        self.assertGreater(slow, fast)


class DfmTest(unittest.TestCase):
    def _dfm(self, **overrides):
        args = dict(
            process="cnc_machining_3axis", bbox=(100, 60, 20), holes=[],
            min_internal_radius_mm=None, min_wall_thickness_mm=None, sharp_internal_corners=0,
            inaccessible_area_mm2=0.0, total_area_mm2=1000.0, body_count=1,
            stock={"dimensionsMm": {"x": 105, "y": 65, "z": 25}},
        )
        args.update(overrides)
        return {i["code"]: i for i in cnc_model.run_cnc_dfm(**args)}

    def test_clean_part_has_no_issues(self):
        self.assertEqual(self._dfm(), {})

    def test_deep_hole_and_thin_wall(self):
        issues = self._dfm(
            holes=[{"diameterMm": 2.0, "depthMm": 25.0, "through": False}],
            min_wall_thickness_mm=0.3,
        )
        self.assertEqual(issues["DEEP_HOLE"]["severity"], "error")
        self.assertTrue(issues["THIN_WALL"]["blocking"])

    def test_oversize_part_is_blocking(self):
        self.assertTrue(self._dfm(bbox=(1200, 100, 100))["EXCEEDS_MACHINE_ENVELOPE"]["blocking"])

    def test_sharp_corners_only_when_no_floor_setup(self):
        edges = [{"normals": ["+X", "+Y"]}, {"normals": ["+Z", "+X"]}]
        self.assertEqual(cnc_model.count_sharp_internal_corners(edges, ["+Z", "-Z"]), 1)


class ReportTest(unittest.TestCase):
    def test_block_report_shape(self):
        geom = {
            "volumeMm3": 100 * 60 * 20 - 4000,
            "surfaceAreaMm2": 17000.0,
            "bbox": (100.0, 60.0, 20.0),
            "faceCount": 14,
            "bodyCount": 1,
            "sampleAccess": [
                {"directions": ["+Z"], "areaMm2": 6000},
                {"directions": ["-Z"], "areaMm2": 6000},
                {"directions": ["+Z", "-Z"], "areaMm2": 5000},
            ],
            "holes": [{"diameterMm": 6.0, "depthMm": 20.0, "through": True, "axis": "+Z"}] * 4,
            "minInternalRadiusMm": 3.0,
            "minWallThicknessMm": 10.0,
            "concaveEdges": [],
            "turning": None,
        }
        report = cnc_model.build_cnc_report(geom)
        self.assertEqual(report["process"]["recommended"], "cnc_machining_3axis")
        self.assertEqual(report["setups"]["count"], 2)
        self.assertEqual(report["features"]["holes"][0]["count"], 4)
        self.assertGreater(report["stock"]["removedVolumeMm3"], 0)
        self.assertIn("aluminium", report["machiningTime"])
        self.assertEqual(report["dfm"], [])


if __name__ == "__main__":
    unittest.main()
