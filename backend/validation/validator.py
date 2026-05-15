"""
Enhanced geometry validation and analysis.

Provides:
1. Geometry analysis (bounding box, volume, surface area, face/edge count, etc.)
2. Enhanced validation checks (manifold, degenerate dims, wall thickness estimate)
3. Manufacturability heuristics for 3D printing

All checks use CadQuery + OpenCascade (OCC) APIs — no external tools.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import cadquery as cq

from ..domain.models import (
    HardConstraints, 
    ManufacturabilityIssue, 
    ManufacturabilityReport
)


# ---------------------------------------------------------------------------
# Geometry analysis result
# ---------------------------------------------------------------------------

@dataclass
class GeometryAnalysis:
    """Measurements extracted from a CadQuery shape."""

    # Bounding box
    bbox_x_mm: Optional[float] = None
    bbox_y_mm: Optional[float] = None
    bbox_z_mm: Optional[float] = None

    # Volume & area
    volume_mm3: Optional[float] = None
    surface_area_mm2: Optional[float] = None

    # Topology
    solid_count: int = 0
    face_count: int = 0
    edge_count: int = 0
    vertex_count: int = 0

    # Center of mass (approximate)
    center_of_mass_x: Optional[float] = None
    center_of_mass_y: Optional[float] = None
    center_of_mass_z: Optional[float] = None

    # Estimated print weight (PLA density ≈ 1.24 g/cm³)
    estimated_mass_g: Optional[float] = None

    # Computed validity
    is_closed: bool = False
    has_solids: bool = False

    # Manufacturability
    small_feature_count: int = 0
    tiny_face_count: int = 0
    sharp_corner_count: int = 0
    thin_pin_count: int = 0

    def to_stats_dict(self) -> dict:
        """Convert to a flat dict for injection into vision/repair prompts."""
        d: dict = {}
        if self.bbox_x_mm is not None:
            d["bounding_box"] = f"{self.bbox_x_mm:.1f} × {self.bbox_y_mm:.1f} × {self.bbox_z_mm:.1f} mm"
        if self.volume_mm3 is not None:
            d["volume"] = f"{self.volume_mm3:.1f} mm³ ({self.volume_mm3 / 1000:.2f} cm³)"
        if self.surface_area_mm2 is not None:
            d["surface_area"] = f"{self.surface_area_mm2:.1f} mm²"
        if self.estimated_mass_g is not None:
            d["estimated_mass_pla"] = f"{self.estimated_mass_g:.1f} g"
        d["solid_count"] = self.solid_count
        d["face_count"] = self.face_count
        d["edge_count"] = self.edge_count
        d["is_closed_shell"] = self.is_closed
        d["small_feature_count"] = self.small_feature_count
        d["tiny_face_count"] = self.tiny_face_count
        d["sharp_corner_count"] = self.sharp_corner_count
        d["thin_pin_count"] = self.thin_pin_count
        return d


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------

@dataclass
class AnalysisResult:
    """Combined validation + analysis result."""

    # Validation
    is_valid: bool = True
    violations: list[str] = field(default_factory=list)  # hard constraint failures
    warnings: list[str] = field(default_factory=list)    # soft issues

    # Analysis
    analysis: Optional[GeometryAnalysis] = None
    manufacturability: Optional[ManufacturabilityReport] = None

    # Failure types for repair routing
    has_geometry_errors: bool = False    # non-manifold, open shell, zero volume
    has_constraint_violations: bool = False  # print volume exceeded


# ---------------------------------------------------------------------------
# OCC helpers
# ---------------------------------------------------------------------------

def _get_occ_shape(shape: cq.Workplane):
    """Extract the underlying OCC TopoDS_Shape from a CadQuery Workplane."""
    try:
        return shape.val().wrapped
    except Exception:
        return None


def _compute_properties(occ_shape) -> tuple[float, float]:
    """
    Compute volume and surface area from an OCC shape.
    Returns (volume_mm3, surface_area_mm2).
    """
    try:
        from OCC.Core.BRepGProp import brepgprop_VolumeProperties, brepgprop_SurfaceProperties
        from OCC.Core.GProp import GProp_GProps

        # Volume
        v_props = GProp_GProps()
        brepgprop_VolumeProperties(occ_shape, v_props)
        volume = abs(v_props.Mass())

        # Surface area
        s_props = GProp_GProps()
        brepgprop_SurfaceProperties(occ_shape, s_props)
        surface_area = s_props.Mass()

        return volume, surface_area
    except Exception:
        return 0.0, 0.0


def _compute_center_of_mass(occ_shape) -> tuple[float, float, float]:
    """Compute center of mass from an OCC shape."""
    try:
        from OCC.Core.BRepGProp import brepgprop_VolumeProperties
        from OCC.Core.GProp import GProp_GProps

        props = GProp_GProps()
        brepgprop_VolumeProperties(occ_shape, props)
        cg = props.CentreOfMass()
        return cg.X(), cg.Y(), cg.Z()
    except Exception:
        return 0.0, 0.0, 0.0


def _is_closed_shell(occ_shape) -> bool:
    """
    Check if the OCC shape is a closed (watertight) solid.
    Uses BRep_Builder / BRepCheck_Analyzer.
    """
    try:
        from OCC.Core.BRepCheck import BRepCheck_Analyzer

        analyzer = BRepCheck_Analyzer(occ_shape)
        return analyzer.IsValid()
    except Exception:
        # If we can't check, assume OK
        return True


def _count_topology(shape: cq.Workplane) -> tuple[int, int, int, int]:
    """Count solids, faces, edges, vertices."""
    try:
        solids = len(shape.solids().vals())
        faces = len(shape.faces().vals())
        edges = len(shape.edges().vals())
        vertices = len(shape.vertices().vals())
        return solids, faces, edges, vertices
    except Exception:
        return 0, 0, 0, 0


# ---------------------------------------------------------------------------
# Degenerate dimension check
# ---------------------------------------------------------------------------

def _check_degenerate_dims(
    bbox_x: float, bbox_y: float, bbox_z: float
) -> list[str]:
    """Flag dimensions that are suspiciously small (< 0.1 mm)."""
    warnings = []
    threshold = 0.1
    for name, val in [("X", bbox_x), ("Y", bbox_y), ("Z", bbox_z)]:
        if val < threshold:
            warnings.append(
                f"Dimension {name} = {val:.3f}mm is nearly flat (< {threshold}mm) — "
                "this may indicate a degenerate shape"
            )
    return warnings


# ---------------------------------------------------------------------------
# Minimum wall thickness heuristic (approximate)
# ---------------------------------------------------------------------------

def _estimate_min_wall_thickness(shape: cq.Workplane) -> Optional[float]:
    """
    Rough estimate of minimum wall thickness using bounding box vs volume ratio.

    This is a heuristic: actual wall thickness check requires ray-casting or
    offset-based methods which are expensive. We use a simpler proxy.

    Returns estimated minimum thickness in mm, or None if unable to compute.
    """
    try:
        bb = shape.val().BoundingBox()
        x = bb.xmax - bb.xmin
        y = bb.ymax - bb.ymin
        z = bb.zmax - bb.zmin

        # Shell-like objects have volume much less than bbox volume
        occ_shape = _get_occ_shape(shape)
        if occ_shape is None:
            return None

        volume, _ = _compute_properties(occ_shape)
        bbox_volume = x * y * z

        if bbox_volume <= 0:
            return None

        fill_ratio = volume / bbox_volume

        # For a hollow shell, fill_ratio ≈ wall_thickness / half_shortest_dim
        shortest_dim = min(x, y, z)

        # Very rough: if fill ratio is very low and object is large, walls are thin
        estimated_thickness = fill_ratio * shortest_dim * 0.5
        return max(estimated_thickness, 0.0)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Small feature detection
# ---------------------------------------------------------------------------

def _detect_small_features(shape: cq.Workplane, threshold: float = 0.4) -> int:
    """
    Detect edges that are smaller than a threshold (e.g., nozzle size).
    Returns count of such edges.
    """
    try:
        small_edges = 0
        for edge in shape.edges().vals():
            if edge.Length() < threshold:
                small_edges += 1
        return small_edges
    except Exception:
        return 0


def _detect_tiny_faces(shape: cq.Workplane, threshold: float = 1.0) -> int:
    """
    Detect faces with area smaller than a threshold (mm²).
    Returns count of such faces.
    """
    try:
        tiny_faces = 0
        for face in shape.faces().vals():
            if face.Area() < threshold:
                tiny_faces += 1
        return tiny_faces
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Sharp internal corner detection
# ---------------------------------------------------------------------------

def _detect_sharp_internal_corners(shape: cq.Workplane, threshold_deg: float = 45.0) -> int:
    """
    Detect internal (concave) corners that are sharper than the threshold.
    Returns count of such edges.
    """
    # This is a heuristic: check if an edge is concave and has a sharp angle.
    # We look for edges where the faces meet at a steep angle.
    try:
        sharp_corners = 0
        for edge in shape.edges().vals():
            # Only check linear edges for simplicity
            if edge.geomType() != "LINE":
                continue
            
            # Find faces sharing this edge
            faces = [f for f in shape.faces().vals() if any(e.isSame(edge) for e in f.Edges())]
            if len(faces) < 2:
                continue
            
            f1, f2 = faces[0], faces[1]
            pnt = edge.Center()
            try:
                n1 = f1.normalAt(pnt)
                n2 = f2.normalAt(pnt)
            except Exception:
                continue
            
            # Dihedral angle between outward normals
            dot = n1.dot(n2)
            dot = max(-1.0, min(1.0, dot))
            angle_deg = math.degrees(math.acos(dot))
            
            # If the angle between normals is very sharp (e.g. > 135 deg)
            # this indicates an acute corner (either convex or concave).
            if angle_deg > (180.0 - threshold_deg):
                sharp_corners += 1
                    
        return sharp_corners
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Thin pin / fragile feature detection
# ---------------------------------------------------------------------------

def _detect_thin_pins(shape: cq.Workplane, threshold_mm: float = 1.0) -> int:
    """
    Detect features that are thin and relatively long (fragile pins/tabs).
    Returns count of such features.
    """
    try:
        thin_pins = 0
        solids = shape.solids().vals()
        for solid in solids:
            bb = solid.BoundingBox()
            dx = bb.xmax - bb.xmin
            dy = bb.ymax - bb.ymin
            dz = bb.zmax - bb.zmin
            
            # Sort dimensions to find the smallest cross-section
            dims = sorted([dx, dy, dz])
            
            # If the two smallest dimensions are below threshold, it's a 'pin' or 'thin part'
            if dims[0] < threshold_mm and dims[1] < threshold_mm:
                # And the longest dimension is significant
                if dims[2] > threshold_mm * 3:
                    thin_pins += 1
        return thin_pins
    except Exception:
        return 0

def compute_geometry_analysis(shape: cq.Workplane) -> GeometryAnalysis:
    """
    Compute comprehensive geometry analysis from a CadQuery Workplane.
    Never raises — returns whatever it can compute.
    """
    analysis = GeometryAnalysis()

    # Bounding box
    try:
        bb = shape.val().BoundingBox()
        analysis.bbox_x_mm = round(bb.xmax - bb.xmin, 3)
        analysis.bbox_y_mm = round(bb.ymax - bb.ymin, 3)
        analysis.bbox_z_mm = round(bb.zmax - bb.zmin, 3)
    except Exception:
        pass

    # Topology counts
    solids, faces, edges, vertices = _count_topology(shape)
    analysis.solid_count = solids
    analysis.face_count = faces
    analysis.edge_count = edges
    analysis.vertex_count = vertices
    analysis.has_solids = solids > 0

    # OCC-based properties
    occ_shape = _get_occ_shape(shape)
    if occ_shape is not None:
        volume, surface_area = _compute_properties(occ_shape)
        if volume > 0:
            analysis.volume_mm3 = round(volume, 2)
            # PLA density: 1.24 g/cm³ = 1.24e-3 g/mm³
            analysis.estimated_mass_g = round(volume * 1.24e-3, 2)
        if surface_area > 0:
            analysis.surface_area_mm2 = round(surface_area, 2)

        # Center of mass
        cx, cy, cz = _compute_center_of_mass(occ_shape)
        analysis.center_of_mass_x = round(cx, 3)
        analysis.center_of_mass_y = round(cy, 3)
        analysis.center_of_mass_z = round(cz, 3)

        # Closed shell check
        analysis.is_closed = _is_closed_shell(occ_shape)

    # Manufacturability checks
    analysis.small_feature_count = _detect_small_features(shape)
    analysis.tiny_face_count = _detect_tiny_faces(shape)
    analysis.sharp_corner_count = _detect_sharp_internal_corners(shape)
    analysis.thin_pin_count = _detect_thin_pins(shape)

    return analysis


# ---------------------------------------------------------------------------
# Enhanced validation
# ---------------------------------------------------------------------------

def validate_geometry_enhanced(
    shape: cq.Workplane,
    constraints: HardConstraints,
) -> AnalysisResult:
    """
    Full geometry validation + analysis pipeline.

    Checks:
    1. Shape is not null/empty
    2. Has solid bodies
    3. Bounding box within hard constraints
    4. No degenerate dimensions
    5. Is a closed (watertight) shell
    6. Minimum wall thickness warning (heuristic)

    Returns AnalysisResult with violations, warnings, and analysis.
    """
    result = AnalysisResult()

    # Compute analysis first
    try:
        analysis = compute_geometry_analysis(shape)
        result.analysis = analysis
    except Exception as e:
        result.is_valid = False
        result.violations.append(f"Cannot analyze geometry: {e}")
        result.has_geometry_errors = True
        return result

    # Check solid bodies
    if not analysis.has_solids or analysis.solid_count == 0:
        result.is_valid = False
        result.violations.append("Shape has no solid bodies — geometry is empty or degenerate")
        result.has_geometry_errors = True
        return result

    # Bounding box checks
    if analysis.bbox_x_mm is not None:
        x, y, z = analysis.bbox_x_mm, analysis.bbox_y_mm, analysis.bbox_z_mm

        if x > constraints.max_x_mm:
            result.violations.append(
                f"X dimension {x:.1f}mm exceeds print volume limit {constraints.max_x_mm}mm"
            )
            result.has_constraint_violations = True
        if y > constraints.max_y_mm:
            result.violations.append(
                f"Y dimension {y:.1f}mm exceeds print volume limit {constraints.max_y_mm}mm"
            )
            result.has_constraint_violations = True
        if z > constraints.max_z_mm:
            result.violations.append(
                f"Z dimension {z:.1f}mm exceeds print volume limit {constraints.max_z_mm}mm"
            )
            result.has_constraint_violations = True

        # Degenerate dimension warnings
        degen_warnings = _check_degenerate_dims(x, y, z)
        result.warnings.extend(degen_warnings)
        if degen_warnings:
            result.has_geometry_errors = True

    # Closed shell check
    if not analysis.is_closed:
        result.warnings.append(
            "Shape may not be a fully closed (watertight) solid — this could cause "
            "issues with STL export and 3D printing. Consider using .shell() or ensuring "
            "all booleans produce closed geometry."
        )
        result.has_geometry_errors = True

    # Wall thickness heuristic
    estimated_thickness = _estimate_min_wall_thickness(shape)

    # Deterministic printability score
    # Start at 1.0, subtract for issues
    score = 1.0
    issues = []

    # Wall thickness
    if estimated_thickness is not None and estimated_thickness < constraints.min_wall_thickness_mm:
        msg = f"Estimated minimum wall thickness ({estimated_thickness:.2f}mm) may be below the FDM minimum of {constraints.min_wall_thickness_mm}mm."
        result.warnings.append(msg)
        issues.append(ManufacturabilityIssue(
            issue_type="thin_wall",
            severity="warning",
            description=msg
        ))
        score -= 0.2

    # Small features
    if analysis.small_feature_count > 0:
        msg = f"Detected {analysis.small_feature_count} small features/edges (< 0.4mm). These may not be printable with a standard 0.4mm nozzle."
        result.warnings.append(msg)
        issues.append(ManufacturabilityIssue(
            issue_type="small_feature",
            severity="warning",
            description=msg
        ))
        score -= 0.1

    # Tiny faces
    if analysis.tiny_face_count > 0:
        msg = f"Detected {analysis.tiny_face_count} tiny faces (< 1.0mm²). These might be degenerate geometry."
        result.warnings.append(msg)
        issues.append(ManufacturabilityIssue(
            issue_type="tiny_face",
            severity="warning",
            description=msg
        ))
        score -= 0.1

    # Sharp internal corners
    if analysis.sharp_corner_count > 0:
        msg = f"Detected {analysis.sharp_corner_count} sharp internal corners. Consider adding fillets."
        result.warnings.append(msg)
        issues.append(ManufacturabilityIssue(
            issue_type="sharp_corner",
            severity="warning",
            description=msg
        ))
        score -= 0.1

    # Thin pins
    if analysis.thin_pin_count > 0:
        msg = f"Detected {analysis.thin_pin_count} potentially fragile thin pins/vertical features."
        result.warnings.append(msg)
        issues.append(ManufacturabilityIssue(
            issue_type="thin_pin",
            severity="warning",
            description=msg
        ))
        score -= 0.2

    # Closed shell check
    if not analysis.is_closed:
        score -= 0.5 # Major issue for printability

    result.manufacturability = ManufacturabilityReport(
        issues=issues,
        is_printable=analysis.is_closed and score > 0.4,
        score=max(0.0, score)
    )

    # Determine overall validity
    if result.violations:
        result.is_valid = False

    return result
