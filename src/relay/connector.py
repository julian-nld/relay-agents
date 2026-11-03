#!/usr/bin/env python3
"""Relay's connector for this computer ("@NAME@").

While this runs, agents in Relay can run commands on this computer and read
and write its files, as the user who started it. Every command is shown
here. Stop it with Ctrl+C (or close the window); then they can't.

    python3 relay-computer.py          (Windows: py relay-computer.py)
    python3 relay-computer.py --ask    confirm every command and file change here
    python3 relay-computer.py --url https://name:password@host
                                       Relay at another address, with a proxy's
                                       own sign-in in front of it (or RELAY_URL)

Only Python's standard library is needed (Python 3.8 or newer).
"""

import argparse
import base64
import getpass
import json
import locale
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RELAY = "@RELAY_URL@"
TOKEN = "@TOKEN@"
NAME = "@NAME@"
VERSION = 1
MAX_OUTPUT = 60000
MAX_FILE = 20 * 1024 * 1024
WINDOWS = platform.system() == "Windows"

console = threading.Lock()
proxy = {}  # a proxy's own sign-in in front of Relay (Caddy's basic_auth): its Authorization header


def say(text):
    with console:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {text}"
        try:
            print(line, flush=True)
        except UnicodeEncodeError:  # a console that can't show every character
            print(line.encode("ascii", "replace").decode(), flush=True)


def address(url):
    """Relay's address without a name and password in it, which go to the proxy in front of it instead."""
    global RELAY
    parts = urllib.parse.urlsplit(url.strip())
    if parts.username is not None:
        sign_in(urllib.parse.unquote(parts.username), urllib.parse.unquote(parts.password or ""))
        parts = parts._replace(netloc=parts.netloc.rpartition("@")[2])
    RELAY = urllib.parse.urlunsplit(parts).rstrip("/")


def sign_in(name, password):
    proxy["Authorization"] = "Basic " + base64.b64encode(f"{name}:{password}".encode()).decode()


def post(path, data, timeout):
    request = urllib.request.Request(f"{RELAY}/computer/{TOKEN}/{path}", data=json.dumps(data).encode(),
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": f"relay-computer/{VERSION}", **proxy})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def about():
    return {
        "os": platform.system(), "release": platform.release(), "version": platform.version()[:120],
        "machine": platform.machine(), "hostname": socket.gethostname(), "user": getpass.getuser(),
        "home": str(Path.home()), "python": platform.python_version(), "connector": VERSION,
        "shell": os.environ.get("COMSPEC", "cmd.exe") if WINDOWS else os.environ.get("SHELL", "/bin/sh"),
    }


def decode(data):
    for encoding in ("utf-8", locale.getpreferredencoding(False) or "utf-8"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def where(path):
    return Path(os.path.expandvars(os.path.expanduser(path or "~")))


def run(job):
    command = job["command"]
    cwd = where(job.get("cwd"))
    timeout = job.get("timeout") or 120
    say(f"{NAME} > {command}" + (f"   (in {cwd})" if job.get("cwd") else ""))
    # Its own process group, so a time-out also ends what the command started (a program
    # left running would otherwise keep the output open, and this waiting for ever).
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}
    try:
        process = subprocess.Popen(command, shell=True, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **group)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        end_group(process)
        stdout, _ = process.communicate()
        output = decode(stdout or b"")
        return {"ok": False, "error": f"Stopped after {timeout} seconds", "output": output[-MAX_OUTPUT:]}
    output = decode(stdout or b"")
    if len(output) > MAX_OUTPUT:
        output = f"[… {len(output) - MAX_OUTPUT} characters left out]\n" + output[-MAX_OUTPUT:]
    return {"ok": True, "exit": process.returncode, "output": output}


def end_group(process):
    """Stop a command and everything it started."""
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
        else:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def read(job):
    path = where(job["path"])
    say(f"{NAME} reads {path}")
    if not path.is_file():
        return {"ok": False, "error": f"No file {path}"}
    size = path.stat().st_size
    if size > MAX_FILE:
        return {"ok": False, "error": f"{path} is {size} bytes; at most {MAX_FILE} can be read"}
    return {"ok": True, "path": str(path), "size": size, "content_b64": base64.b64encode(path.read_bytes()).decode()}


def write(job):
    path = where(job["path"])
    data = base64.b64decode(job["content_b64"])
    say(f"{NAME} writes {path} ({len(data)} bytes)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"ok": True, "path": str(path), "size": len(data)}


def confirmed(job):
    what = job.get("command") or f"write {job.get('path')}"
    with console:
        answer = input(f"Allow {what!r}? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def handle(job, ask):
    try:
        if ask and job["kind"] in ("run", "write") and not confirmed(job):
            result = {"ok": False, "error": "The user refused this on the computer itself"}
        else:
            result = {"run": run, "read": read, "write": write}[job["kind"]](job)
    except Exception as exc:  # noqa: BLE001 - whatever it is, Relay hears about it
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    for attempt in range(5):
        try:
            post("result", {"id": job["id"], **result}, timeout=60)
            return
        except (urllib.error.URLError, OSError):
            time.sleep(2 * (attempt + 1))
    say("Couldn't send a result back to Relay")


def asked(again):
    """A proxy in front of Relay asks a sign-in of its own (Caddy's basic_auth): its name and password, typed here."""
    if again:
        say("The proxy in front of Relay didn't take that name and password.")
    else:
        say("Relay's address asks for a sign-in of its own (a proxy in front of Relay).")
    if not sys.stdin.isatty():
        say("Start this with its name and password in the address: --url https://name:password@host (or RELAY_URL).")
        return False
    with console:
        name = input("Name: ").strip()
        password = getpass.getpass("Password: ")
    sign_in(name, password)
    return True


def main():
    parser = argparse.ArgumentParser(description=f"Relay's connector for this computer ({NAME}).")
    parser.add_argument("--ask", action="store_true", help="confirm every command and file change here")
    parser.add_argument("--url", default=os.environ.get("RELAY_URL") or RELAY,
                        help="Relay's address, as this computer reaches it; behind a proxy's own sign-in: "
                             "https://name:password@host (default: RELAY_URL, else the address it came with)")
    options = parser.parse_args()
    address(options.url)
    say(f"Connecting {NAME} to Relay at {RELAY}. Press Ctrl+C to stop.")
    connected = False
    wait = 2
    while True:
        try:
            answer = post("poll", {"info": about()}, timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and "basic" in (exc.headers.get("WWW-Authenticate") or "").lower():
                if not asked(bool(proxy)):
                    return 1
                continue
            if exc.code in (401, 403, 404):
                say("Relay doesn't know this connector (any more). Download a new one under Settings → Computers.")
                return 1
            say(f"Relay answered {exc.code}; trying again in {wait} s")
        except (urllib.error.URLError, OSError) as exc:
            if connected:
                say(f"Lost Relay ({getattr(exc, 'reason', exc)}); trying again")
            elif wait == 2:
                say(f"Can't reach Relay yet ({getattr(exc, 'reason', exc)}); trying again")
            connected = False
        else:
            if not connected:
                say("Connected. Agents in Relay can work on this computer now.")
                connected = True
            wait = 2
            for job in answer.get("jobs", []):
                threading.Thread(target=handle, args=(job, options.ask), daemon=True).start()
            continue
        time.sleep(wait)
        wait = min(wait * 2, 30)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("Stopped. Relay can't work on this computer until you start this again.")
