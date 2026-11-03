"""Relay's Chrome extension (plugins/chrome/extension) in Chromium, against tests/chrome/mock_relay.py: a stand-in for
Relay's API with the chrome plugin loaded by Relay's own plugin host. The checks themselves are Node scripts
(tests/chrome/e2e*.cjs, with Python Playwright's own Node package and its full Chromium: its headless shell loads no
extensions). tests/chrome/real*.cjs do it against a real Relay and agent, by hand (their README: plugins/chrome)."""

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKS = ROOT / "tests" / "chrome"


def chromium():
    """The full Chromium Python Playwright installed (CHROMIUM when given)."""
    if os.environ.get("CHROMIUM"):
        return os.environ["CHROMIUM"]
    browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    found = sorted(browsers.glob("chromium-*/chrome-linux*/chrome"))
    return str(found[-1]) if found else None


def playwright_core():
    try:
        import playwright
    except ImportError:
        return None
    found = Path(playwright.__file__).parent / "driver" / "package"
    return str(found) if (found / "package.json").exists() else None


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def checks(tmp_path):
    """Runs one of tests/chrome's scripts against a mock Relay of its own: its output, after it passed."""
    node, browser, core = shutil.which("node"), chromium(), playwright_core()
    if not (node and browser and core):
        pytest.skip("needs Node, Python Playwright and its Chromium")
    env = {**os.environ, "CHROMIUM": browser, "PLAYWRIGHT_CORE": core,
           "RELAY_CHROME_SHOTS": str(tmp_path / "shots"), "RELAY_MOCK_HOME": str(tmp_path / "mock")}
    (tmp_path / "mock").mkdir()
    started = []

    def run(script):
        port = free_port()
        with open(tmp_path / f"mock-{port}.log", "w") as log:
            mock = subprocess.Popen([sys.executable, str(CHECKS / "mock_relay.py"), str(port)], env=env, stdout=log,
                                    stderr=subprocess.STDOUT)
        started.append(mock)
        deadline = time.monotonic() + 20
        while "mock Relay at" not in (tmp_path / f"mock-{port}.log").read_text():
            assert mock.poll() is None and time.monotonic() < deadline, (tmp_path / f"mock-{port}.log").read_text()
            time.sleep(0.1)
        done = subprocess.run([node, str(CHECKS / script), str(port)], env=env, capture_output=True, text=True,
                              timeout=600)
        said = done.stdout + done.stderr
        failed = [line for line in said.splitlines() if line.startswith("FAIL")]
        assert done.returncode == 0 and "all passed" in said, "\n".join(failed) or said[-3000:]
        return said

    yield run
    for mock in started:
        mock.terminate()
        mock.wait(10)


def test_the_extension_with_relays_api(checks):
    """Its settings, a chat with a page, a selection and a screenshot going along, permissions, Stop, the queue,
    history, its scripts, what the right-click menu hands over, and the setup screen."""
    said = checks("e2e.cjs")
    assert said.count("\nok ") > 60


def test_agents_use_this_browser_through_the_plugin(checks):
    """The plugin's tools, called as agents call them, asked on cards in the panel, done in the tab and answered."""
    started = time.monotonic()
    said = checks("e2e_agents.cjs")
    assert said.count("\nok ") > 40, f"took {time.monotonic() - started:.0f} s"
