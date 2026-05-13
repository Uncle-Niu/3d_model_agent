"""Server-side rendering service for CadQuery shapes."""

from .renderer import RenderService, RenderResult, render_shape_multiangle

__all__ = ["RenderService", "RenderResult", "render_shape_multiangle"]
