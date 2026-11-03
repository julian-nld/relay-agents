"""A last line of defence: commands no agent in Relay may run.

Agents run shell commands without asking, in a container (relay.containers):
most mistakes are cheap there, and git has the project's history. Some aren't,
as the project and the agent's home are mounted in it: wiping the whole disk,
the home folder or the system (`rm -rf /`, `rm -rf ~`, `rm -rf $EMPTY/*`),
formatting a drive, a fork bomb. Those are refused, whatever the agent, mode
or setting, everywhere Relay can step in:

- every agent's shell: Relay's, which hands commands to the container;
- Claude Code: a PreToolUse hook (this file, run as a script) for Bash and PowerShell;
- opencode and ACP agents: their permission requests for shell commands;
- chat models: Relay's own run_command tool;
- your own computers: every command before it's sent there.

Only the standard library is used, so Claude Code can run this file directly.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import sys

# Folders whose loss is a disaster (and their contents).
SYSTEM = {"/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64", "/opt", "/proc", "/root",
          "/sbin", "/srv", "/sys", "/usr", "/var", "/mnt", "/media", "/snap", "/Users", "/System", "/Library",
          "/Applications", "/private", "/Volumes"}
HOME = {"~", "$HOME", "${HOME}", "~/"}
SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")", "{", "}", "|&"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
REMOVERS = {"rm", "rmdir", "srm"}

WINDOWS_PATTERNS = [
    (re.compile(r"\bformat(\.com)?\s+[a-z]:", re.I), "formats a drive"),
    (re.compile(r"\b(rd|rmdir)\s+(/[sq]\s+)+[\"']?[a-z]:\\?[\"']?\s*$", re.I), "removes a whole drive"),
    (re.compile(r"\b(rd|rmdir)\s+(/[sq]\s+)+[\"']?[a-z]:\\(windows|users|program files)[\\\"']?\s*$", re.I),
     "removes a system folder"),
    (re.compile(r"\bdel\s+(/[fsqa]\S*\s+)*[\"']?[a-z]:\\(\*(\.\*)?)?[\"']?(\s|$)", re.I), "deletes a whole drive"),
    (re.compile(r"remove-item\b.*-recurse\b.*[\"'\s][a-z]:\\?(\*)?[\"']?(\s|$)", re.I), "removes a whole drive"),
    (re.compile(r"remove-item\b.*[\"'\s][a-z]:\\?(\*)?[\"']?\s.*-recurse", re.I), "removes a whole drive"),
    # (-Recurse before or after the folder)
    (re.compile(r"remove-item\b(?=.*\s-recurse\b).*(\$env:(userprofile|systemroot|windir)|[a-z]:\\(windows|users))\\?[\"']?(\s|$)",
                re.I), "removes the home or system folder"),
    (re.compile(r"\b(format-volume|clear-disk|initialize-disk)\b", re.I), "wipes a disk"),
    (re.compile(r"\bdiskpart\b", re.I), "changes disk partitions"),
    (re.compile(r"\bbcdedit\b.*/delete", re.I), "breaks the boot configuration"),
]

OTHER_PATTERNS = [
    (re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:\s*&[^}]*\}\s*;\s*:"), "is a fork bomb"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "formats a file system"),
    (re.compile(r"\b(wipefs|sgdisk\s+(-Z|--zap-all)|blkdiscard)\b"), "wipes a disk"),
    (re.compile(r"\bdd\b[^;|&]*\bof=/dev/(sd|hd|vd|xvd|nvme|mmcblk|disk|rdisk)"), "overwrites a disk"),
    (re.compile(r">\s*/dev/(sd|hd|vd|xvd|nvme|mmcblk|disk|rdisk)\w*"), "overwrites a disk"),
    (re.compile(r"\bshred\b[^;|&]*\s/dev/(sd|hd|vd|nvme|mmcblk|disk)"), "wipes a disk"),
    (re.compile(r"--no-preserve-root"), "removes the root folder"),
    (re.compile(r"\bchmod\s+(-\w*R\w*|--recursive)\s+\S+\s+/(\s|$|\*)"), "changes permissions of the whole system"),
    (re.compile(r"\bchown\s+(-\w*R\w*|--recursive)\s+\S+\s+/(\s|$|\*)"), "changes owners of the whole system"),
    (re.compile(r"\bmv\s+(-\w+\s+)*/(\*|\s)"), "moves the root folder away"),
    (re.compile(r"\bfind\s+/(\s|$)[^;|&]*(-delete\b|-exec\s+rm\b)"), "deletes files all over the system"),
]


def check(command: str) -> str | None:
    """Why `command` must not run, or None when it may."""
    if not command or not command.strip():
        return None
    for pattern, why in OTHER_PATTERNS + WINDOWS_PATTERNS:
        if pattern.search(command):
            return f"This command {why}."
    return _check_shell(command, depth=0)


def _check_shell(command: str, depth: int) -> str | None:
    if depth > 3:
        return None
    try:
        lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        words = command.split()  # unbalanced quotes: judge the plain words
    for simple in _split(words):
        found = _check_simple(simple, depth)
        if found:
            return found
    return None


def _split(words: list[str]) -> list[list[str]]:
    parts, current = [], []
    for word in words:
        if word in SEPARATORS or (word and set(word) <= set(";&|(){}")):
            if current:
                parts.append(current)
            current = []
        else:
            current.append(word)
    if current:
        parts.append(current)
    return parts


def _check_simple(words: list[str], depth: int) -> str | None:
    # Look at every word, not just the first: `sudo -u root rm …`, `xargs rm …`,
    # `find … -exec rm …` and `FOO=1 rm …` all remove things too.
    for index, word in enumerate(words):
        program = word.rsplit("/", 1)[-1]
        if program in SHELLS:
            rest = words[index + 1:]
            for at, flag in enumerate(rest):
                if re.fullmatch(r"-\w*c\w*", flag) and at + 1 < len(rest):
                    found = _check_shell(rest[at + 1], depth + 1)
                    if found:
                        return found
                    break
        elif program == "eval":
            return _check_shell(" ".join(words[index + 1:]), depth + 1)
        elif program in REMOVERS:
            found = _check_rm(words[index:])
            if found:
                return found
    return None


def _check_rm(words: list[str]) -> str | None:
    flags = "".join(w.lstrip("-") for w in words[1:] if w.startswith("-") and w != "--")
    recursive = "r" in flags.lower() or "recursive" in flags
    targets = [w for w in words[1:] if not w.startswith("-") or w == "-"]
    for target in targets:
        reason = _dangerous_target(target, recursive)
        if reason:
            return f"`{' '.join(words)}` would remove {reason}. Relay never runs that."
    return None


def _dangerous_target(target: str, recursive: bool) -> str | None:
    raw = target
    # A variable that may be empty turns `$DIR/*` into `/*`; ${DIR:?} would stop instead.
    if re.match(r"^\$\{?\w+\}?(/|$)", raw) and not re.match(r"^\$\{\w+:\?", raw) and raw.split("/")[0] not in HOME:
        if recursive or raw.endswith("*"):
            return (f"whatever {raw} expands to; if the variable is empty that's the root folder "
                    f"(write \"${{VAR:?}}\"/… so an empty variable stops the command)")
    path = raw.rstrip("/") or "/"
    if path.startswith("/"):
        # /etc/../home/alice is /home/alice, and //, /./ are /
        glob = "/*" if path.endswith("/*") else ""
        stem = re.sub(r"^/+", "/", posixpath.normpath((path[:-2] if glob else path) or "/"))
        path = (stem.rstrip("/") + glob) or "/"
    if path in ("/*", "/.*") or path == "/":
        return "the whole file system"
    if path in HOME or path in ("~/*", "$HOME/*", "${HOME}/*", "~/.*"):
        return "the home folder"
    if re.fullmatch(r"/home/[^/]+(/\*)?|/Users/[^/]+(/\*)?|/root(/\*)?", path):
        return "a home folder"
    base = path[:-2] if path.endswith("/*") else path
    if base in SYSTEM and (recursive or path.endswith("/*")):
        return f"the system folder {base}"
    return None


# As a Claude Code PreToolUse hook: the tool call as JSON on stdin; exit 2 blocks it.
def _hook() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = data.get("tool_input") or {}
    command = tool.get("command") if isinstance(tool, dict) else None
    reason = check(command) if isinstance(command, str) else None
    if reason:
        print(f"Relay blocked this command: {reason} Find a safer way, or ask the user to do it themselves.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--hook"]:
        sys.exit(_hook())
    if len(sys.argv) == 3 and sys.argv[1] == "--command":
        reason = check(sys.argv[2])
        if reason:
            print(f"Relay blocked this command: {reason}", file=sys.stderr)
        sys.exit(2 if reason else 0)
    for line in sys.argv[1:]:
        print(f"{line!r}: {check(line) or 'fine'}")
