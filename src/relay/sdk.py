"""Relay's Python SDK, served by Relay itself (`/sdk/`): a wheel made from sdk/python/relay_sdk, with RESL
(sdk/python/resl, Relay's Extended Standard Library, built on it).

pip installs it from this server's address on any machine that reaches it, with no build tools and
no internet: `pip install http://<relay>/sdk/relay_sdk-<version>-py3-none-any.whl`. It's pure Python
(the standard library only), so the wheel is the module and its metadata (PEP 427), made once a
process and kept.
"""

from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from functools import lru_cache
from pathlib import Path

from relay import __version__

SOURCE = Path(__file__).resolve().parents[2] / "sdk" / "python" / "relay_sdk"
PACKAGES = (SOURCE, SOURCE.parent / "resl")  # what the wheel holds
NAME = "relay_sdk"


def wheel_name() -> str:
    return f"{NAME}-{__version__}-py3-none-any.whl"


@lru_cache(maxsize=1)
def wheel() -> bytes:
    """The wheel: the modules (relay_sdk, resl), METADATA, WHEEL, and a RECORD of them with their hashes."""
    info = f"{NAME}-{__version__}.dist-info"
    files = {f"{package.name}/{path.relative_to(package)}": path.read_bytes()
             for package in PACKAGES for path in sorted(package.rglob("*.py"))}
    files[f"{info}/METADATA"] = (f"Metadata-Version: 2.1\nName: relay-sdk\nVersion: {__version__}\n"
                                 "Summary: Relay's Python SDK: conversations with Relay's agents (and RESL)\n"
                                 "License: MIT\nRequires-Python: >=3.9\n").encode()
    files[f"{info}/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: relay\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    record = [f"{path},sha256={_digest(data)},{len(data)}" for path, data in files.items()]
    files[f"{info}/RECORD"] = ("\n".join([*record, f"{info}/RECORD,,"]) + "\n").encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return buffer.getvalue()


def page(base: str) -> str:
    """/sdk/: how to install it from this server, and its start."""
    url = f"{base.rstrip('/')}/sdk/{wheel_name()}"
    return (f"Relay's Python SDK, version {__version__}\n\n"
            f"Install it (Python 3.9+, no other packages needed):\n\n    pip install {url}\n\n"
            "Then:\n\n"
            "    from relay_sdk import *\n\n"
            f"    user = login('username', 'password', url='{base.rstrip('/')}')\n"
            "    # or, for a script, an app password (Settings → Account) instead of your password:\n"
            f"    user = connect('{base.rstrip('/')}', app_password='relay_…')\n"
            "    convo_id = convo.create(user, title='Hello')\n"
            "    message = convo.send(convo_id, 'Hi! What can you do?')\n"
            "    print(message.response)\n\n"
            "Also: scripts (yours, on the Scripts page), tasks (scheduled ones), services (your live ones) and\n"
            "notify(text), to your phone.\n\n"
            "RESL, Relay's Extended Standard Library, comes with it: `from resl import *` gives the SDK, and\n"
            "agent_refused(message), extract(message, fields), best_of(prompt, agents) and more.\n\n"
            "Relay's guide (docs/GUIDE.md, Python SDK) says more.\n")


def _digest(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
