import sys
from pathlib import Path

# app.py / sim.py are flat modules in the parent dir (they import each other as
# `import sim as _sim`), so the package root has to be importable by that name.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
