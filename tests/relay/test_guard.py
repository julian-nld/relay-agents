import json
import subprocess
import sys

import pytest

from relay import guard

BLOCKED = [
    "rm -rf /", "rm -rf /*", "rm -fr / ", "sudo rm -rf --no-preserve-root /", "rm -rf ~", "rm -rf ~/",
    "rm -rf $HOME", "rm -rf \"$HOME\"/*", "rm -r /home/pi", "rm -rf /usr", "rm -rf /etc/*",
    "rm -rf $S/*", "rm -rf ${S}/e2e $S/dav", "cd /tmp && rm -rf $DIR/", "echo hi; rm -rf /",
    "sudo -u root rm -rf /", "bash -c 'rm -rf /'", "sh -lc \"rm -rf ~/*\"", "find / -name x -delete",
    "find / -exec rm -rf {} \\;", "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda bs=1M",
    "cat x > /dev/nvme0n1", ":(){ :|:& };:", "chmod -R 777 /", "chown -R me /", "eval 'rm -rf /'",
    "format c:", "rd /s /q C:\\", "del /f /s /q C:\\*", "Remove-Item -Recurse -Force C:\\",
    "Remove-Item C:\\ -Recurse", "Remove-Item -Recurse $env:USERPROFILE", "diskpart", "wipefs -a /dev/sdb",
    "xargs rm -rf /",
]
ALLOWED = [
    "rm -rf build", "rm -rf ./node_modules", "rm -rf /tmp/relay-test", "rm -f ~/notes.txt", "rm -rf ~/project/build",
    "rm -rf \"${S:?}\"/*.py", "rm -rf ${TMPDIR:?}/x", "rm file.txt", "ls /", "git clean -fdx",
    "echo 'rm -rf /' > notes.md", "git commit -m 'no rm -rf / please'", "rm -rf $HOME/.cache/foo",
    "python3 -m pytest", "dd if=/dev/zero of=disk.img bs=1M count=10", "del notes.txt", "rmdir empty",
    "Remove-Item -Recurse C:\\Users\\me\\build", "find . -name '*.pyc' -delete", "chmod -R 755 ./dist",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_disasters_are_refused(command):
    assert guard.check(command), command


@pytest.mark.parametrize("command", ALLOWED)
def test_ordinary_work_is_not(command):
    assert guard.check(command) is None, (command, guard.check(command))


def test_as_a_claude_code_hook():
    def hook(command):
        data = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        return subprocess.run([sys.executable, guard.__file__, "--hook"], input=data, capture_output=True, text=True)
    blocked = hook("rm -rf /")
    assert blocked.returncode == 2 and "Relay blocked this command" in blocked.stderr
    assert hook("ls").returncode == 0

