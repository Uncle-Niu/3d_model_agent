"""
Tests for CadQuery Assembly handling and manifest generation.
"""

import unittest
from pathlib import Path
import tempfile
import shutil
import cadquery as cq
from backend.cad.engine import process_cadquery_code
from backend.domain.models import HardConstraints

class TestAssemblyHandling(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.constraints = HardConstraints()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_single_part_manifest(self):
        code = """
import cadquery as cq
result = cq.Workplane("XY").box(10, 10, 10)
"""
        res = process_cadquery_code(code, self.test_dir, constraints=self.constraints)
        self.assertTrue(res["success"])
        self.assertIn("assembly", res)
        manifest = res["assembly"]
        self.assertEqual(manifest.total_parts, 1)
        self.assertEqual(manifest.parts[0].name, "part")
        self.assertIsNotNone(manifest.parts[0].geometry_stats)

    def test_multi_part_assembly_manifest(self):
        code = """
import cadquery as cq
box = cq.Workplane("XY").box(10, 10, 10)
sphere = cq.Workplane("XY").sphere(5).translate((20, 0, 0))

result = cq.Assembly()
result.add(box, name="my_box", color=cq.Color("red"))
result.add(sphere, name="my_sphere", color=cq.Color("blue"))
"""
        res = process_cadquery_code(code, self.test_dir, constraints=self.constraints)
        if not res["success"]:
            print(f"DEBUG: message={res['message']}")
            print(f"DEBUG: violations={res['violations']}")
            print(f"DEBUG: warnings={res['warnings']}")
        self.assertTrue(res["success"])
        self.assertIn("assembly", res)
        manifest = res["assembly"]
        self.assertEqual(manifest.total_parts, 2)
        
        part_names = [p.name for p in manifest.parts]
        self.assertIn("my_box", part_names)
        self.assertIn("my_sphere", part_names)
        
        # Check files
        self.assertTrue((self.test_dir / "assembly_manifest.json").exists())
        self.assertTrue((self.test_dir / "model.glb").exists())

    def test_assembly_validation_failure(self):
        # One part exceeds constraints
        code = """
import cadquery as cq
box = cq.Workplane("XY").box(300, 10, 10) # Oversized
sphere = cq.Workplane("XY").sphere(5)

result = cq.Assembly()
result.add(box, name="oversized_box")
result.add(sphere, name="ok_sphere")
"""
        res = process_cadquery_code(code, self.test_dir, constraints=self.constraints)
        self.assertFalse(res["success"])
        self.assertIn("oversized_box", res["message"])
        self.assertEqual(res["failure_type"], "constraint_violation")

if __name__ == "__main__":
    unittest.main()
