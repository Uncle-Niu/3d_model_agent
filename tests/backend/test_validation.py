"""
Tests for the enhanced geometry validation module.
"""

import tempfile
import unittest
from pathlib import Path

import cadquery as cq

from backend.domain.models import HardConstraints
from backend.validation.validator import (
    GeometryAnalysis,
    AnalysisResult,
    compute_geometry_analysis,
    validate_geometry_enhanced,
)


def _make_box(x=50, y=30, z=10) -> cq.Workplane:
    return cq.Workplane("XY").box(x, y, z)


def _make_thin_wall_box() -> cq.Workplane:
    """Box with 0.5mm shell — thin walls."""
    return cq.Workplane("XY").box(30, 20, 10).faces(">Z").shell(-0.5)


def _make_cylinder(h=20, r=10) -> cq.Workplane:
    return cq.Workplane("XY").cylinder(h, r)


class TestComputeGeometryAnalysis(unittest.TestCase):

    def test_box_has_solids(self):
        shape = _make_box()
        analysis = compute_geometry_analysis(shape)
        self.assertTrue(analysis.has_solids)
        self.assertEqual(analysis.solid_count, 1)

    def test_box_bounding_box(self):
        shape = _make_box(50, 30, 10)
        analysis = compute_geometry_analysis(shape)
        self.assertAlmostEqual(analysis.bbox_x_mm, 50.0, places=1)
        self.assertAlmostEqual(analysis.bbox_y_mm, 30.0, places=1)
        self.assertAlmostEqual(analysis.bbox_z_mm, 10.0, places=1)

    def test_box_volume(self):
        # 50 * 30 * 10 = 15000 mm³
        shape = _make_box(50, 30, 10)
        analysis = compute_geometry_analysis(shape)
        if analysis.volume_mm3 is not None:  # requires OCC BRepGProp
            self.assertAlmostEqual(analysis.volume_mm3, 15000.0, delta=50.0)
        # Bounding box is always available
        self.assertAlmostEqual(analysis.bbox_x_mm, 50.0, places=1)

    def test_cylinder_volume(self):
        import math
        # pi * r² * h = pi * 100 * 20 ≈ 6283
        shape = _make_cylinder(h=20, r=10)
        analysis = compute_geometry_analysis(shape)
        if analysis.volume_mm3 is not None:  # requires OCC BRepGProp
            expected = math.pi * 10**2 * 20
            self.assertAlmostEqual(analysis.volume_mm3, expected, delta=100.0)
        # Bounding box always available
        self.assertAlmostEqual(analysis.bbox_z_mm, 20.0, places=1)

    def test_mass_estimate(self):
        # PLA density 1.24e-3 g/mm³ — requires OCC BRepGProp
        shape = _make_box(50, 30, 10)
        analysis = compute_geometry_analysis(shape)
        if analysis.estimated_mass_g is not None:
            expected = 15000 * 1.24e-3
            self.assertAlmostEqual(analysis.estimated_mass_g, expected, delta=1.0)

    def test_face_count_box(self):
        shape = _make_box()
        analysis = compute_geometry_analysis(shape)
        # A box has 6 faces
        self.assertEqual(analysis.face_count, 6)

    def test_is_closed(self):
        shape = _make_box()
        analysis = compute_geometry_analysis(shape)
        self.assertTrue(analysis.is_closed)

    def test_stats_dict_keys(self):
        shape = _make_box()
        analysis = compute_geometry_analysis(shape)
        d = analysis.to_stats_dict()
        # Always-present keys
        self.assertIn("bounding_box", d)
        self.assertIn("solid_count", d)
        self.assertIn("face_count", d)
        self.assertIn("is_closed_shell", d)
        # OCC-dependent keys — present only when BRepGProp is available
        if analysis.volume_mm3 is not None:
            self.assertIn("volume", d)
            self.assertIn("estimated_mass_pla", d)

    def test_complex_shape_face_count(self):
        # Cylinder has 3 faces: top, bottom, lateral
        shape = _make_cylinder()
        analysis = compute_geometry_analysis(shape)
        self.assertGreaterEqual(analysis.face_count, 3)


class TestValidateGeometryEnhanced(unittest.TestCase):

    def setUp(self):
        self.constraints = HardConstraints(
            max_x_mm=200, max_y_mm=200, max_z_mm=200,
            min_wall_thickness_mm=1.2,
        )

    def test_valid_box_passes(self):
        shape = _make_box(50, 30, 10)
        result = validate_geometry_enhanced(shape, self.constraints)
        self.assertTrue(result.is_valid)
        self.assertEqual(result.violations, [])

    def test_oversized_x_fails(self):
        shape = _make_box(300, 30, 10)  # 300 > 200
        result = validate_geometry_enhanced(shape, self.constraints)
        self.assertFalse(result.is_valid)
        self.assertTrue(result.has_constraint_violations)
        self.assertTrue(any("X dimension" in v for v in result.violations))

    def test_oversized_z_fails(self):
        shape = _make_box(50, 30, 250)  # 250 > 200
        result = validate_geometry_enhanced(shape, self.constraints)
        self.assertFalse(result.is_valid)
        self.assertTrue(any("Z dimension" in v for v in result.violations))

    def test_valid_result_has_analysis(self):
        shape = _make_box()
        result = validate_geometry_enhanced(shape, self.constraints)
        self.assertIsNotNone(result.analysis)
        # Bounding box always computed
        self.assertGreater(result.analysis.bbox_x_mm, 0)
        # Volume computed when OCC BRepGProp available
        if result.analysis.volume_mm3 is not None:
            self.assertGreater(result.analysis.volume_mm3, 0)

    def test_warnings_not_violations(self):
        # Thin-wall box should warn but may not hard-fail depending on heuristic
        shape = _make_thin_wall_box()
        result = validate_geometry_enhanced(shape, self.constraints)
        # Should not have constraint violations (dimensions are fine)
        self.assertFalse(result.has_constraint_violations)

    def test_result_is_analysis_result_type(self):
        shape = _make_box()
        result = validate_geometry_enhanced(shape, self.constraints)
        self.assertIsInstance(result, AnalysisResult)
        self.assertIsInstance(result.analysis, GeometryAnalysis)


class TestManufacturabilityChecks(unittest.TestCase):
    
    def test_small_feature_detection(self):
        # Create a box with a tiny 0.1mm peg on top
        shape = cq.Workplane("XY").box(10, 10, 10).faces(">Z").workplane().circle(0.05).extrude(0.1)
        analysis = compute_geometry_analysis(shape)
        # The peg has edges of length 0.1mm (around the circle/extrusion)
        self.assertGreater(analysis.small_feature_count, 0)
    
    def test_tiny_face_detection(self):
        # Create a box with a very thin sliver extrude
        shape = cq.Workplane("XY").box(10, 10, 10).faces(">Z").workplane().rect(0.1, 0.1).extrude(0.1)
        analysis = compute_geometry_analysis(shape)
        # The tiny extrude faces are 0.1 * 0.1 = 0.01 mm²
        self.assertGreater(analysis.tiny_face_count, 0)

    def test_validation_warnings_small_features(self):
        constraints = HardConstraints()
        shape = cq.Workplane("XY").box(10, 10, 10).faces(">Z").workplane().circle(0.05).extrude(0.1)
        result = validate_geometry_enhanced(shape, constraints)
        self.assertTrue(any("small features" in w for w in result.warnings))

    def test_sharp_internal_corner_detection(self):
        # Create a block with a sharp 45-degree internal cutout
        # A 90-degree internal corner (normal dot product 0) shouldn't trigger default 45 threshold
        # unless it's very sharp.
        # Let's create an L-shape block. Internal corner is 90 deg.
        shape = cq.Workplane("XY").box(20, 20, 10).faces(">Z").workplane().rect(10, 10, centered=False).cutThruAll()
        analysis = compute_geometry_analysis(shape)
        # Default threshold is 45. 90 deg (dot=0) is sharper than 45?
        # angle_deg = math.degrees(acos(dot))
        # If dot = 0, angle = 90. 90 > (180 - 45) = 135? No.
        # Wait, if angle_deg > 180 - threshold.
        # For 90 deg corner, normals are (1,0,0) and (0,1,0). Dot is 0. acos(0) = 90.
        # 90 is NOT > 135.
        self.assertEqual(analysis.sharp_corner_count, 0)
        
        # Now 30 degree internal corner (very sharp)
        # Normals will have angle 150 deg. 150 > 135.
        # We can simulate this by a wedge cut.
        shape = (cq.Workplane("XY").box(20, 20, 10)
                 .faces(">Z").workplane()
                 .moveTo(0,0).lineTo(10, 0).lineTo(10, 2).close()
                 .cutThruAll())
        analysis = compute_geometry_analysis(shape)
        self.assertGreater(analysis.sharp_corner_count, 0)

    def test_thin_pin_detection(self):
        # Create a tall thin peg, DON'T combine with base so it stays as a separate solid
        # for our heuristic to catch it easily.
        base = cq.Workplane("XY").box(20, 20, 2)
        pin = base.faces(">Z").workplane().circle(0.4).extrude(10, combine=False)
        analysis = compute_geometry_analysis(pin)
        # height 10, radius 0.4 (width 0.8). 
        # Thickness t approx 2*V/A = 2*(pi*r^2*h) / (2*pi*r*h + 2*pi*r^2) ≈ 2*r/2 = r = 0.4.
        # 0.4 < 1.0 (default threshold).
        self.assertGreater(analysis.thin_pin_count, 0)


if __name__ == "__main__":
    unittest.main()
