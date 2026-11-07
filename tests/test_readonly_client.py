"""Hard rule: the app never writes to the League client. Phase 2 acceptance: a code search finds
no write calls. This scans every source file's AST, not just lcu/."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
WRITE_METHODS = {"post", "put", "patch", "delete"}


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            yield node


def test_no_write_calls_to_the_client():
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls(tree):
            name = call.func.attr.lower()
            where = f"{path.relative_to(SRC)}:{call.lineno}"
            # connection.request("<method>", ...) must be "get"
            if name == "request":
                method = call.args[0] if call.args else None
                if not (isinstance(method, ast.Constant) and str(method.value).lower() == "get"):
                    offenders.append(f"{where} request() with non-GET method")
            # direct .post/.put/.patch/.delete on anything inside the LCU package
            if name in WRITE_METHODS and "lcu" in path.parts:
                offenders.append(f"{where} .{name}()")
    assert offenders == []


def test_read_only_wrapper_only_exposes_get():
    from draftassist.lcu.connector import ReadOnlyLcu

    public = {n for n in vars(ReadOnlyLcu) if not n.startswith("_")}
    assert public == {"get"}
