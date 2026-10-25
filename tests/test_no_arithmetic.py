"""The Computation agent may not do arithmetic itself (brief section 3.2).

Static check: the module that builds the lines contains no arithmetic
operator, no augmented assignment and no call to a Python numeric built-in.
Every number must be the return value of a tax_math tool.
"""
import ast
import pathlib

import silverfile.agents.computation as computation

ARITH = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.MatMult)
NUMERIC_BUILTINS = {"sum", "round", "abs", "min", "max", "divmod", "pow"}


def test_computation_module_has_no_arithmetic():
    src = pathlib.Path(computation.__file__).read_text()
    tree = ast.parse(src)
    offences = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ARITH):
            offences.append(f"line {node.lineno}: operator {type(node.op).__name__}")
        if isinstance(node, ast.AugAssign):
            offences.append(f"line {node.lineno}: augmented assignment")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in NUMERIC_BUILTINS:
            offences.append(f"line {node.lineno}: call to {node.func.id}()")
    assert not offences, "arithmetic found in the Computation agent:\n" + "\n".join(offences)
