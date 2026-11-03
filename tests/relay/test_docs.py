"""The docs' links work: each relative link names a file in the repository, and each #anchor a heading there (as
GitHub makes them)."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# (copies, for agents in a WorkBox: their links are their originals', which test_plugins keeps them the same as)
COPIES = ("src/relay/builtin_skills/relay-plugin-writer/reference/",)


def slug(title: str) -> str:
    return re.sub(r"[^\w\- ]", "", title.strip().lower().replace("`", "")).replace(" ", "-")


def headings(path: Path) -> set[str]:
    found, fenced = set(), False
    for line in path.read_text().split("\n"):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and (heading := re.match(r"#{1,6} (.+)", line)):
            found.add(slug(heading.group(1)))
    return found


def test_links_and_anchors_lead_somewhere():
    listed = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    broken = []
    for name in listed:
        if "/vendor/" in f"/{name}" or name.startswith(COPIES):
            continue
        page = ROOT / name
        text = re.sub(r"```.*?```", "", page.read_text(), flags=re.S)
        for target in re.findall(r"\]\(([^)\s]+)\)", text) + re.findall(r'(?:src|srcset|href)="([^"]+)"', text):
            if re.match(r"[a-z]+:", target) or target.startswith("/"):
                continue
            file, _, anchor = target.partition("#")
            dest = (page.parent / file).resolve() if file else page
            if not dest.exists():
                broken.append(f"{name}: {target} (no such file)")
            elif anchor and dest.suffix == ".md" and anchor not in headings(dest):
                broken.append(f"{name}: {target} (no such heading)")
    assert not broken, "\n".join(broken)
