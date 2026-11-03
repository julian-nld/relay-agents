// A file as it was before a code task's change: its unified diff (git's, as Relay gives it) taken back from the file
// as it is now, so VSCodium's own diff editor can show the two. Null when that can't be done exactly: a binary file,
// a diff Relay cut short, a file changed again since.
"use strict";

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

// The diff's hunks, and which side has no newline at its end ("\ No newline at end of file" after its last line)
function parse(diff) {
  const hunks = [];
  const ends = { old: false, new: false };
  let current = null;
  let last = null;
  for (const line of diff.split("\n")) {
    const head = HUNK.exec(line);
    if (head) {
      current = { oldStart: +head[1], oldCount: head[2] === undefined ? 1 : +head[2],
        newStart: +head[3], newCount: head[4] === undefined ? 1 : +head[4], lines: [] };
      current.oldLeft = current.oldCount;
      current.newLeft = current.newCount;
      hunks.push(current);
      continue;
    }
    if (line.startsWith("\\")) {
      if (last === "-" || last === " ") ends.old = true;
      if (last === "+" || last === " ") ends.new = true;
      continue;
    }
    if (!current || (current.oldLeft <= 0 && current.newLeft <= 0)) continue;  // (a header, or past the hunk)
    // (an empty line within a hunk: an empty context line whose space was trimmed away)
    const sign = line === "" ? " " : line[0];
    if (sign === " ") {
      current.oldLeft--;
      current.newLeft--;
    } else if (sign === "-") {
      current.oldLeft--;
    } else if (sign === "+") {
      current.newLeft--;
    } else {
      return null;
    }
    current.lines.push(sign + line.slice(1));
    last = sign;
  }
  return { hunks, ends };
}

function before(after, diff) {
  if (typeof diff !== "string") return null;
  const parsed = parse(diff);
  if (!parsed) return null;
  const { hunks, ends } = parsed;
  if (!hunks.length) {
    if (/^(Binary files |GIT binary patch)/m.test(diff)) return null;
    if (/^(new|deleted) file mode/m.test(diff)) return "";  // (an empty file made or removed)
    return after;  // (renamed only, or its mode changed)
  }
  const lines = after === "" ? [] : after.split("\n");
  const ended = after === "" || after.endsWith("\n");
  if (after !== "" && ended) lines.pop();
  for (const hunk of [...hunks].reverse()) {  // (from the last, so the earlier ones' places stay)
    if (hunk.oldLeft !== 0 || hunk.newLeft !== 0) return null;  // (cut short)
    const now = hunk.lines.filter((l) => l[0] !== "-").map((l) => l.slice(1));
    const was = hunk.lines.filter((l) => l[0] !== "+").map((l) => l.slice(1));
    const at = hunk.newCount === 0 ? hunk.newStart : hunk.newStart - 1;
    if (now.some((line, i) => lines[at + i] !== line)) return null;  // (the file changed since)
    lines.splice(at, now.length, ...was);
  }
  if (!lines.length) return "";
  return lines.join("\n") + ((ends.old ? false : ends.new ? true : ended) ? "\n" : "");
}

module.exports = { before, parse };
