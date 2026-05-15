"""
Curated CadQuery examples and API reference for LLM few-shot prompting.

Each example is a working CadQuery script that the LLM can learn from.
These are injected into the system prompt to teach the model correct syntax
and 3D printing best practices.
"""

EXAMPLES = {
    "simple_box": {
        "description": "A simple box with rounded edges",
        "code": '''import cadquery as cq

# Simple box with filleted vertical edges — good for 3D printing
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

# Hollow enclosure with 2mm wall thickness, open top
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
thickness = 4.0
width = 40

# Build L-profile via extrusion
pts = [(0, 0), (width, 0), (width, thickness), (thickness, thickness),
       (thickness, 30), (0, 30)]

result = (
    cq.Workplane("XZ")
    .polyline(pts)
    .close()
    .extrude(width)
    .edges("|Y")
    .fillet(1.5)
    .faces(">Z")
    .workplane()
    .pushPoints([(10, 10), (-10, 10)])
    .hole(4)
    .faces(">X")
    .workplane()
    .pushPoints([(0, 10)])
    .hole(4)
)
''',
    },
    "rounded_plate_with_holes": {
        "description": "A plate with rounded corners and a bolt hole pattern",
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
        "description": "A cylindrical container with flat bottom and wall thickness",
        "code": '''import cadquery as cq

# Cylindrical container with 2mm wall and open top
outer_radius = 25
wall = 2.0
height = 40

result = (
    cq.Workplane("XY")
    .cylinder(height, outer_radius)
    .faces(">Z")
    .workplane()
    .hole((outer_radius - wall) * 2, height - wall)
)
''',
    },
    "hex_nut": {
        "description": "A hexagonal nut shape",
        "code": '''import cadquery as cq

# Hexagonal nut with M10 center hole
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
    "angled_support_stand": {
        "description": "A generic angled support stand with a base, leaning support, retaining ledge, and ribs",
        "code": '''import cadquery as cq

# Generic support stand: base, angled support panel, front ledge,
# and triangular ribs. Adapt dimensions to the object being supported.
stand_width = 90
base_depth = 92
base_thickness = 5
support_height = 95
support_thickness = 6
ledge_height = 14
ledge_depth = 16
fillet_radius = 1.5

base = (
    cq.Workplane("XY")
    .box(stand_width, base_depth, base_thickness)
    .edges("|Z")
    .fillet(4)
)

support_panel = (
    cq.Workplane("XY")
    .box(stand_width - 10, support_thickness, support_height)
    .translate((0, 18, base_thickness + support_height / 2))
    .rotate((0, 18, base_thickness), (stand_width, 18, base_thickness), -12)
    .edges()
    .fillet(fillet_radius)
)

ledge = (
    cq.Workplane("XY")
    .box(stand_width - 18, ledge_depth, ledge_height)
    .translate((0, -base_depth / 2 + ledge_depth / 2, base_thickness + ledge_height / 2))
)

rib_profile = [(0, 0), (32, 0), (32, 45)]
left_rib = (
    cq.Workplane("YZ")
    .polyline(rib_profile)
    .close()
    .extrude(5)
    .translate((-stand_width / 2 + 18, -6, base_thickness))
)
right_rib = left_rib.mirror("YZ")

result = (
    base.union(support_panel)
    .union(ledge)
    .union(left_rib).union(right_rib)
)
''',
    },
    "cable_clip": {
        "description": "A snap-fit cable management clip for a 6mm cable",
        "code": '''import cadquery as cq
import math

# Snap-fit cable clip: C-shaped profile extruded
cable_r = 3.5      # cable radius
wall = 2.0
gap_angle = 60     # opening angle in degrees (snap-fit gap)
height = 12
base_w = 20
base_h = 3

# Build C-ring via shell on a cylinder, then cut the snap gap
ring = (
    cq.Workplane("XY")
    .cylinder(height, cable_r + wall)
    .faces(">Z")
    .workplane()
    .hole(cable_r * 2)
)

# Cut snap-fit opening
gap_w = (cable_r + wall) * 2 * math.sin(math.radians(gap_angle / 2))
gap_cut = (
    cq.Workplane("XY")
    .box(gap_w, cable_r + wall + 2, height + 2)
    .translate([0, cable_r + wall, 0])
)

ring = ring.cut(gap_cut)

# Base plate
base = (
    cq.Workplane("XY")
    .box(base_w, base_w, base_h)
    .edges("|Z")
    .fillet(2)
    .faces(">Z")
    .workplane()
    .hole(3.5)  # screw hole
)

result = base.union(ring.translate([0, 0, base_h]))
''',
    },
    "threaded_hole": {
        "description": "A block with an M5 threaded through-hole (modeled as clearance hole with chamfer)",
        "code": '''import cadquery as cq

# Block with M5 thread-ready hole (5mm drill + 60° countersink)
# For actual thread, use a tap — model represents the drill hole
result = (
    cq.Workplane("XY")
    .box(30, 30, 20)
    .edges("|Z")
    .fillet(2)
    .faces(">Z")
    .workplane()
    .cskHole(5.0, 9.0, 90)  # M5 clearance, 9mm csk, 90deg angle
)
''',
    },
    "multi_body_assembly": {
        "description": "A two-part assembly: base plate and vertical post",
        "code": '''import cadquery as cq

# Base plate
base = (
    cq.Workplane("XY")
    .box(60, 40, 5)
    .edges("|Z")
    .fillet(3)
    .faces(">Z")
    .workplane()
    .rect(40, 20, forConstruction=True)
    .vertices()
    .hole(4)
)

# Vertical post centered on base top
post = (
    cq.Workplane("XY")
    .workplane(offset=5)
    .cylinder(40, 6)
    .edges()
    .chamfer(1)
)

# Combine into a single result (union for printability)
result = base.union(post)
''',
    },
    "living_hinge": {
        "description": "A box with a thin flexible living hinge for lid attachment",
        "code": '''import cadquery as cq

# Box body
box_w, box_d, box_h = 50, 40, 20
wall = 2.5
hinge_thickness = 0.8   # thin enough to flex in PETG/TPU
hinge_width = 5

body = (
    cq.Workplane("XY")
    .box(box_w, box_d, box_h)
    .faces(">Z")
    .shell(-wall)
)

# Lid (separate, connected by hinge)
lid = (
    cq.Workplane("XY")
    .workplane(offset=box_h + hinge_thickness)
    .box(box_w, box_d, wall)
    .edges("|Z")
    .fillet(2)
)

# Hinge strip connecting back edge of body to lid
hinge = (
    cq.Workplane("XZ")
    .workplane(offset=box_d / 2)
    .box(hinge_width, hinge_thickness, box_h + hinge_thickness + wall)
    .translate([0, box_d / 2, (box_h + hinge_thickness + wall) / 2 - wall])
)

result = body.union(lid).union(hinge)
''',
    },
}


def get_examples_text() -> str:
    """Format all examples as a text block for the system prompt."""
    parts = ["## CadQuery Examples (use these as reference)"]
    for name, example in EXAMPLES.items():
        parts.append(f"\n### Example: {example['description']}")
        parts.append(f"```python\n{example['code'].strip()}\n```")
    return "\n".join(parts)


def get_api_reference() -> str:
    """Curated CadQuery API quick reference for the system prompt."""
    return """\
## CadQuery API Quick Reference

**Workplane setup:**
- `cq.Workplane("XY")` — start on XY plane (Z points up — correct for printing)
- `.workplane(offset=N)` — workplane at Z=N above current face
- `.faces(">Z").workplane()` — workplane on top face

**Primitives:**
- `.box(l, w, h)` — centered box
- `.cylinder(height, radius)` — centered cylinder (axis=Z)
- `.sphere(radius)` — sphere
- `.polygon(n_sides, diameter)` — regular polygon on workplane

**Sketch → Solid:**
- `.extrude(dist)` — extrude sketch by distance
- `.revolve(angle, (0,0,0), (0,1,0))` — revolve around axis
- `.shell(thickness)` — hollow (negative = inward, open on selected face)

**Holes:**
- `.hole(diameter)` — through hole centered on workplane
- `.hole(diameter, depth)` — blind hole
- `.cboreHole(d, cbore_d, cbore_depth)` — counterbore
- `.cskHole(d, csk_d, csk_angle)` — countersink

**Edge finishing (essential for FDM printing):**
- `.edges("|Z").fillet(r)` — fillet vertical edges
- `.edges(">Z").fillet(r)` — fillet top edges
- `.edges().chamfer(d)` — chamfer all edges
- `.fillet(r)` on selected edges

**Face/Edge selectors:**
- `">Z"` top, `"<Z"` bottom, `">X"` right, `"<X"` left, `">Y"` front, `"<Y"` back
- `"|Z"` edges parallel to Z, `"#Z"` edges perpendicular to Z
- Combine: `.faces(">Z").edges(">X")`

**Positioning:**
- `.center(x, y)` — shift workplane origin
- `.translate((x, y, z))` — move result
- `.rotate((0,0,0), (0,0,1), angle)` — rotate around Z
- `.pushPoints([(x1,y1), ...])` — multiple feature locations

**Boolean:**
- `.cut(other)` — subtract another shape
- `.union(other)` — add shapes
- `.intersect(other)` — intersection

**Mirror / Pattern:**
- `.mirror("XZ")` — mirror about plane
- `.rect(w, h, forConstruction=True).vertices().hole(d)` — bolt pattern

**Multi-body:**
- Work with separate Workplane objects then `.union()` to combine
- Result must be a single merged solid for best printing results
"""
