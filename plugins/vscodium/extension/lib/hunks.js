// What changed in a file, a piece at a time (Keep/Undo in the editor): a line diff (Myers') of the file before the
// agent and as it is now, each run of changed lines one hunk, and the text with one hunk put back. Lines keep their
// line ends, so putting back is exact. Nothing of VSCodium's here: node --test tries it.
"use strict";

const crypto = require("crypto");

const MOST = 4000;  // edits the diff looks for at most: past that, the whole changed part is one hunk

function lines(text) {
  return text ? text.split(/(?<=\n)/) : [];
}

// The edit script between a and b: [["=" | "-" | "+", line], …], in order
function script(a, b) {
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--;
    endB--;
  }
  const x = a.slice(start, endA);
  const y = b.slice(start, endB);
  const middle = myers(x, y) || [...x.map((l) => ["-", l]), ...y.map((l) => ["+", l])];
  return [...a.slice(0, start).map((l) => ["=", l]), ...middle, ...a.slice(endA).map((l) => ["=", l])];
}

function myers(a, b) {
  const n = a.length;
  const m = b.length;
  const max = n + m;
  if (!max) return [];
  const offset = max + 1;
  const v = new Int32Array(2 * max + 3);
  const trace = [];
  for (let d = 0; d <= Math.min(max, MOST); d++) {
    trace.push(v.slice());
    for (let k = -d; k <= d; k += 2) {
      let x = k === -d || (k !== d && v[offset + k - 1] < v[offset + k + 1])
        ? v[offset + k + 1] : v[offset + k - 1] + 1;
      let y = x - k;
      while (x < n && y < m && a[x] === b[y]) {
        x++;
        y++;
      }
      v[offset + k] = x;
      if (x >= n && y >= m) return back(trace, a, b, offset, d);
    }
  }
  return null;
}

// The way back from the end (Myers' trace: each round's furthest x per diagonal, as the round before left them)
function back(trace, a, b, offset, depth) {
  const ops = [];
  let x = a.length;
  let y = b.length;
  for (let d = depth; d > 0; d--) {
    const v = trace[d];
    const k = x - y;
    const previous = k === -d || (k !== d && v[offset + k - 1] < v[offset + k + 1]) ? k + 1 : k - 1;
    const px = v[offset + previous];
    const py = px - previous;
    while (x > px && y > py) {
      ops.push(["=", a[--x]]);
      y--;
    }
    if (x === px) ops.push(["+", b[--y]]);  // (down: a line added)
    else ops.push(["-", a[--x]]);  // (right: a line taken out)
  }
  while (x > 0 && y > 0) {
    ops.push(["=", a[--x]]);
    y--;
  }
  return ops.reverse();
}

// The hunks between before and now: {oldStart, old: [lines], newStart, new: [lines], key} (0-based line numbers)
function hunks(before, now) {
  const found = [];
  let at = 0;
  let atNew = 0;
  let open = null;
  for (const [sign, line] of script(lines(before), lines(now))) {
    if (sign === "=") {
      open = null;
      at++;
      atNew++;
      continue;
    }
    if (!open) {
      open = { oldStart: at, old: [], newStart: atNew, new: [] };
      found.push(open);
    }
    if (sign === "-") {
      open.old.push(line);
      at++;
    } else {
      open.new.push(line);
      atNew++;
    }
  }
  for (const hunk of found) hunk.key = key(hunk);
  return found;
}

// The same change at the same place of the file before: the same key, whatever was changed around it since
function key(hunk) {
  return crypto.createHash("sha1").update(`${hunk.oldStart}\0${hunk.old.join("")}\0${hunk.new.join("")}`)
    .digest("hex").slice(0, 16);
}

// Where a hunk is in the text now (character offsets), and what goes there to put it back
function place(now, hunk) {
  const all = lines(now);
  const start = all.slice(0, hunk.newStart).join("").length;
  return { start, end: start + hunk.new.join("").length, text: hunk.old.join("") };
}

// The text with one hunk put back as it was
function undo(now, hunk) {
  const { start, end, text } = place(now, hunk);
  return now.slice(0, start) + text + now.slice(end);
}

module.exports = { hunks, lines, place, script, undo };
