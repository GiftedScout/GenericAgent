"""Regression: the settlement spinner branch must render without NameError.

Bug: cddcc5d introduced a "记忆结算中…" branch in _spinner_annotation that
referenced C_YELLOW — a constant that does NOT exist in tuiapp_v2 (module
palette has C_AMBER/C_DIM/...).  Result: _spinner_tick's try/except swallowed
the NameError every 0.1s, so the stale gerund ("Threading…") stayed frozen on
screen for the whole settlement, and an unguarded call path (widget mount in
_sync_spinner_widget) crashed Textual with the traceback.

Guards:
  1. no undefined names in _spinner_annotation (ast scan — would catch any
     future C_FOO typo that py_compile cannot)
  2. the settling branch renders and contains the settlement label
"""
import ast, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontends"))
import tuiapp_v2 as t

def check(name, cond, detail=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        raise SystemExit(1)

print("[1] _spinner_annotation has no undefined module-level names")
src_path = os.path.join(os.path.dirname(os.path.abspath(t.__file__)), "tuiapp_v2.py")
tree = ast.parse(open(src_path).read())
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "_spinner_annotation")
local_stores = {n.id for n in ast.walk(fn)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
loaded = set(dir(t))
bad = [n.id for n in ast.walk(fn)
       if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
       and n.id not in loaded and n.id not in local_stores
       and n.id not in dir(__builtins__)
       and n.id not in ("self", "m", "out", "elapsed", "bits", "tok",
                        "last_in", "last_out", "last_cache")]
check("no undefined names", not bad, repr(bad))

print("[2] settling branch renders the settlement label")
app = t.GenericAgentTUI.__new__(t.GenericAgentTUI)
app._spinner_frame = 0
m = t.ChatMessage(role="assistant", content="x", task_id=1, done=False)
m.settling = True
m._settlement_turn = 2
m._settlement_started_at = time.time()
ann = t.GenericAgentTUI._spinner_annotation(app, m)
check("label present", "记忆结算中" in ann.plain, repr(ann.plain))
check("turn shown", "第 2 轮" in ann.plain, repr(ann.plain))
print("\n=== 2 passed ===")
