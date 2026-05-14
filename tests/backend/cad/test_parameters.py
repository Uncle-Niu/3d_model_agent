"""
Unit tests for CAD parameter extraction and injection.
"""

from backend.cad.parameters import extract_parameters, inject_parameters, extract_features


def test_extract_parameters_basic():
    code = """
length = 100
width = 50.5
is_closed = True
name = "box"
"""
    params = extract_parameters(code)
    assert len(params) == 4
    
    names = {p.name for p in params}
    assert "length" in names
    assert "width" in names
    assert "is_closed" in names
    assert "name" in names
    
    p_map = {p.name: p for p in params}
    assert p_map["length"].value == 100
    assert p_map["length"].type == "int"
    assert p_map["width"].value == 50.5
    assert p_map["width"].type == "float"
    assert p_map["is_closed"].value == True
    assert p_map["is_closed"].type == "bool"
    assert p_map["name"].value == "box"
    assert p_map["name"].type == "str"


def test_extract_parameters_ignore_complex():
    code = """
x = 10 + 5
y = [1, 2, 3]
z = {"a": 1}
"""
    params = extract_parameters(code)
    # We currently only extract literals, so x=10+5 is complex and ignored
    assert len(params) == 0


def test_inject_parameters_basic():
    code = """
length = 100
width = 50
result = cq.Workplane().box(length, width, 10)
"""
    new_values = {"length": 150, "width": 75}
    new_code = inject_parameters(code, new_values)
    
    assert "length = 150" in new_code
    assert "width = 75" in new_code
    assert "result = cq.Workplane().box(length, width, 10)" in new_code


def test_inject_parameters_bool_and_str():
    code = """
is_active = False
label = "old"
"""
    new_values = {"is_active": True, "label": "new"}
    new_code = inject_parameters(code, new_values)
    
    assert "is_active = True" in new_code
    assert "label = 'new'" in new_code


def test_inject_parameters_preserve_other_lines():
    code = """
# comment
length = 100
# another comment
width = 50
"""
    new_values = {"length": 120}
    new_code = inject_parameters(code, new_values)
    
    assert "# comment" in new_code
    assert "length = 120" in new_code
    assert "# another comment" in new_code
    assert "width = 50" in new_code


def test_extract_features_basic():
    code = """
import cadquery as cq
result = cq.Workplane("XY").box(10, 20, 30).fillet(2)
result = result.faces(">Z").hole(5)
"""
    features = extract_features(code)
    # Expected features: box, fillet, hole, workplane (maybe?)
    # Method calls: Workplane, box, fillet, faces, hole
    # My cq_methods has: box, fillet, hole, workplane (lowercase)
    
    types = {f.type for f in features}
    assert "box" in types
    assert "fillet" in types
    assert "hole" in types
    
    # Check line numbers
    box_feat = next(f for f in features if f.type == "box")
    assert box_feat.line_start == 3
    
    hole_feat = next(f for f in features if f.type == "hole")
    assert hole_feat.line_start == 4
