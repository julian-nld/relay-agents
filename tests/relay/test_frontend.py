import shutil
import subprocess
from pathlib import Path

import pytest


def test_composer_and_live_settings_regressions():
    node = shutil.which("node")
    if not node:
        pytest.skip("JavaScript regressions need Node.js")
    root = Path(__file__).resolve().parents[2]
    scripts = sorted(str(path.relative_to(root)) for path in (root / "tests/js").glob("*.test.cjs"))
    done = subprocess.run([node, "--test", *scripts], cwd=root,
                          capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
