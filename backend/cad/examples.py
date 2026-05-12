"""
Curated CadQuery examples for LLM few-shot prompting.

Each example is a working CadQuery script that the LLM can learn from.
These are injected into the system prompt to teach the model correct syntax.
"""

EXAMPLES = {
    "simple_box": {
        "description": "A simple box with rounded edges",
        "code": '''import cadquery as cq

# Simple box with filleted edges
result = (
    cq.Workplane("XY")
    .box(50, 30, 10)
    .edges("|Z")
    .fillet(2)
)
''',
    },
    "box_with_hole": {
        "description": "A box with a centered through-hole",
        "code": '''import cadquery as cq

# Box with a centered through-hole
result = (
    cq.Workplane("XY")
    .box(40, 40, 10)
    .faces(">Z")
    .workplane()
    .hole(10)
)
''',
    },
    "cylinder_with_chamfer": {
        "description": "A cylinder with chamfered edges",
        "code": '''import cadquery as cq

# Cylinder with chamfered top and bottom edges
result = (
    cq.Workplane("XY")
    .cylinder(30, 15)
    .edges()
    .chamfer(1.5)
)
''',
    },
    "enclosure": {
        "description": "A hollow box enclosure with wall thickness",
        "code": '''import cadquery as cq

# Hollow enclosure with 2mm wall thickness
wall = 2.0
outer_x, outer_y, outer_z = 60, 40, 25

result = (
    cq.Workplane("XY")
    .box(outer_x, outer_y, outer_z)
    .faces(">Z")
    .shell(-wall)
)
''',
    },
    "bracket": {
        "description": "An L-shaped mounting bracket with holes",
        "code": '''import cadquery as cq

# L-shaped bracket with mounting holes
thickness = 3.0

result = (
    cq.Workplane("XY")
    .box(40, thickness, 30)
    .faces(">Z")
    .workplane()
    .box(40, 20, thickness, centered=(True, False, False))
    .edges("|X")
    .fillet(2)
    .faces("<Y")
    .workplane()
    .pushPoints([(10, 10), (-10, 10)])
    .hole(4)
    .faces(">Z")
    .workplane()
    .pushPoints([(10, 5), (-10, 5)])
    .hole(4)
)
''',
    },
    "rounded_plate_with_holes": {
        "description": "A plate with rounded corners and a bolt pattern",
        "code": '''import cadquery as cq

# Rounded plate with 4 corner mounting holes
result = (
    cq.Workplane("XY")
    .box(80, 50, 5)
    .edges("|Z")
    .fillet(8)
    .faces(">Z")
    .workplane()
    .rect(60, 30, forConstruction=True)
    .vertices()
    .hole(5)
)
''',
    },
    "cylindrical_container": {
        "description": "A cylindrical container with a flat bottom",
        "code": '''import cadquery as cq

# Cylindrical container
outer_radius = 25
wall = 2.0
height = 40

result = (
    cq.Workplane("XY")
    .cylinder(height, outer_radius)
    .faces(">Z")
    .workplane()
    .hole(outer_radius - wall, height - wall)
)
''',
    },
    "hex_nut": {
        "description": "A hexagonal nut shape",
        "code": '''import cadquery as cq

# Hexagonal nut
result = (
    cq.Workplane("XY")
    .polygon(6, 20)
    .extrude(8)
    .faces(">Z")
    .workplane()
    .hole(10)
    .edges()
    .chamfer(0.5)
)
''',
    },
    "phone_stand": {
        "description": "A simple angled phone stand",
        "code": '''import cadquery as cq
import math

# Phone stand with angled support
base_width = 80
base_depth = 60
base_height = 5
support_height = 50
support_angle = 75  # degrees from horizontal
thickness = 4

result = (
    cq.Workplane("XY")
    .box(base_width, base_depth, base_height)
    .edges("|Z")
    .fillet(3)
    .faces(">Z")
    .workplane()
    .center(0, -base_depth / 2 + thickness / 2)
    .box(base_width - 10, thickness, support_height)
    .edges("|X").edges(">Z")
    .fillet(2)
)
''',
    },
    "cable_clip": {
        "description": "A snap-on cable management clip",
        "code": '''import cadquery as cq

# Cable clip with screw hole
cable_diameter = 8
wall = 2.0
base_width = 20
base_height = 3

# Base plate
result = (
    cq.Workplane("XY")
    .box(base_width, base_width, base_height)
    .edges("|Z")
    .fillet(2)
    .faces(">Z")
    .workplane()
    .cylinder(cable_diameter / 2 + wall, cable_diameter / 2 + wall)
    .faces(">Z")
    .workplane()
    .hole(cable_diameter)
    .faces("<Z[-2]")
    .workplane()
    .hole(3.5)
)
''',
    },
}


def get_examples_text() -> str:
    """Format all examples as a text block for the system prompt."""
    parts = []
    for name, example in EXAMPLES.items():
        parts.append(f"### Example: {example['description']}")
        parts.append(f"```python\n{example['code'].strip()}\n```")
        parts.append("")
    return "\n".join(parts)


def get_api_reference() -> str:
    """Curated CadQuery API quick reference for the system prompt."""
    return """### CadQuery API Quick Reference

**Creating Workplanes:**
- `cq.Workplane("XY")` — start on XY plane
- `.workplane()` — create workplane on selected face

**Primitives:**
- `.box(length, width, height)` — centered box
- `.cylinder(height, radius)` — centered cylinder
- `.sphere(radius)` — sphere
- `.polygon(n_sides, diameter)` — regular polygon

**2D to 3D:**
- `.extrude(distance)` — extrude 2D sketch
- `.revolve(angleDegrees)` — revolve sketch
- `.loft()` — loft between sections

**Hole Operations:**
- `.hole(diameter)` — through hole
- `.hole(diameter, depth)` — blind hole
- `.cboreHole(hole_d, cbore_d, cbore_depth)` — counterbore
- `.cskHole(hole_d, csk_d, csk_angle)` — countersink

**Edge Operations:**
- `.fillet(radius)` — fillet selected edges
- `.chamfer(distance)` — chamfer selected edges

**Shell:**
- `.shell(thickness)` — hollow out (negative = inward)

**Face/Edge Selectors:**
- `.faces(">Z")` — top face
- `.faces("<Z")` — bottom face
- `.faces(">X")` — right face
- `.edges("|Z")` — edges parallel to Z
- `.edges(">Z")` — topmost edges

**Positioning:**
- `.center(x, y)` — move workplane center
- `.pushPoints([(x1,y1), (x2,y2)])` — multiple points
- `.rect(x, y, forConstruction=True)` — construction rectangle
- `.vertices()` — select vertices of construction geometry

**Boolean:**
- `.cut(other)` — subtract
- `.union(other)` — add
- `.intersect(other)` — intersect

**Export (handled by engine, not in user code):**
- Result must be assigned to `result` variable
"""
