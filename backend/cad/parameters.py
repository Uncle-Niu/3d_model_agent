"""
Module for extracting and injecting editable parameters from CadQuery source code.
"""

import ast
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel


class CadParameter(BaseModel):
    name: str
    value: Union[float, int, str, bool]
    type: str  # 'float', 'int', 'str', 'bool'
    description: Optional[str] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None


def extract_parameters(code: str) -> List[CadParameter]:
    """
    Extracts top-level variable assignments that look like parameters.
    Example: 
    length = 100
    width = 50
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    parameters = []
    
    # We look for top-level assignments of literals
    for node in tree.body:
        if isinstance(node, ast.Assign):
            # Only handle single target assignments like 'x = 10'
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                
                # Check if value is a literal
                value = None
                param_type = None
                
                if isinstance(node.value, ast.Constant):
                    value = node.value.value
                    if isinstance(value, bool):
                        param_type = 'bool'
                    elif isinstance(value, (int, float)):
                        param_type = 'float' if isinstance(value, float) else 'int'
                    elif isinstance(value, str):
                        param_type = 'str'
                
                if param_type:
                    parameters.append(CadParameter(
                        name=name,
                        value=value,
                        type=param_type
                    ))
                    
    return parameters


def inject_parameters(code: str, param_values: Dict[str, Any]) -> str:
    """
    Injects new values for parameters into the source code.
    Replaces the top-level assignments.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code

    lines = code.splitlines()
    
    # We iterate in reverse to avoid messing up line numbers if we were to insert lines,
    # but here we just replace content on existing lines.
    # Actually, it's safer to just rebuild the lines or use a transformer.
    
    # Simple replacement strategy: find the line with the assignment and replace it.
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in param_values:
                    val = param_values[name]
                    # Format value based on type
                    if isinstance(val, bool):
                        val_str = str(val)
                    elif isinstance(val, str):
                        val_str = f"'{val}'"
                    else:
                        val_str = str(val)
                    
                    # Update the line. Note: node.lineno is 1-indexed.
                    line_idx = node.lineno - 1
                    # Basic regex-free replacement of the assignment
                    # This assumes the assignment is on a single line
                    if "=" in lines[line_idx]:
                        prefix = lines[line_idx].split("=")[0].rstrip()
                        lines[line_idx] = f"{prefix} = {val_str}"

    return "\n".join(lines)
