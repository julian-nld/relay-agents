// Inline editing (Ctrl+I): what the agent is asked about the lines picked in the editor, and the code its answer
// holds. Nothing of VSCodium's here: node --test tries it.
"use strict";

const AROUND = 40;  // lines of the file before and after the picked ones that go along

// What the temporary chat's agent is told (its instructions): one code block back, nothing else
const RULES = "You edit code in the user's editor. They picked some lines of a file and say what should change. Answer "
  + "with one code block holding only the new code for those lines, nothing before or after it: it replaces them as "
  + "it is, so keep their indentation, and don't repeat the lines around them. With no lines picked, the code goes "
  + "where the cursor is.";

function fence(text) {
  let ticks = "```";
  while (text.includes(ticks)) ticks += "`";
  return ticks;
}

// The message: the file's name and language, the lines around, the picked ones marked, and what should change
function prompt({ file, language, lines, from, to, instruction }) {
  const before = lines.slice(Math.max(0, from - AROUND), from);
  const picked = lines.slice(from, to + 1);
  const after = lines.slice(to + 1, to + 1 + AROUND);
  const blank = picked.every((l) => !l.trim());
  const shown = [...before, blank ? "<<<THE CURSOR: THE NEW CODE GOES HERE>>>" : "<<<PICKED LINES START>>>",
    ...(blank ? [] : picked), ...(blank ? [] : ["<<<PICKED LINES END>>>"]), ...after].join("\n");
  const ticks = fence(shown);
  return `${instruction.trim()}\n\nIn ${file}${blank ? `, at line ${from + 1}` : `, lines ${from + 1}-${to + 1}`}:\n`
    + `${ticks}${language || ""}\n${shown}\n${ticks}\n\n`
    + (blank ? "Answer with only the new code for that place, in one code block."
      : "Answer with only the new code for the picked lines, in one code block (without the markers).");
}

// The code in an answer: its first code block, else all of it (a model that left the fence out)
function code(answer) {
  const block = /(^|\n)(`{3,}|~{3,})[^\n]*\n([\s\S]*?)\n?\2[ \t]*(?=\n|$)/.exec(String(answer || ""));
  const found = block ? block[3] : String(answer || "").replace(/^\s*\n|\s+$/g, "");  // (its indentation kept)
  return found.replace(/^<<<(PICKED LINES (START|END)|THE CURSOR[^>]*)>>>\n?/gm, "").replace(/\n+$/, "");
}

module.exports = { AROUND, RULES, code, prompt };
