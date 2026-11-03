---
name: code-review
description: Review code changes for bugs, security problems and missing tests, and report the findings ranked by severity. Use when asked for a code review, or to check work before it's merged.
access: read
---

# Code review

Review, don't fix: change no files unless the user asks you to.

## What to review

In this order, take the first that applies:

1. What the user named: files, a commit, a branch, a pull request.
2. The task's changes, when Relay gave you a starting commit: `git diff <commit>`.
3. Changes not committed yet: `git diff HEAD` (and `git status` for new files).
4. The last commit: `git show HEAD`.

Read the changed code *and* enough around it (callers, tests, the data it
handles) to judge it. A diff alone hides most bugs.

## What to look for

- **Correctness**: logic errors, edge cases (empty, huge, missing, concurrent),
  error handling, off-by-one, wrong assumptions about inputs or state.
- **Security**: injection, path traversal, secrets in code or logs, missing
  checks on who may do what.
- **Data**: migrations, lost or corrupted data, compatibility with what's stored.
- **Tests**: is the new behaviour tested; would the tests catch a regression?
  Run them if there's a quick way.
- **Clarity**: only where it hides a real problem. Skip style nits.

If subagents are available and the change is big, you may let one or two
review parts of it independently; check what they report before passing it on.

## Report

Findings first, most severe first. For each:

- **Where**: `path:line`
- **What**: the problem, in one or two sentences
- **Why it matters**: a concrete case that goes wrong
- **Fix**: a suggestion, briefly

Then a one-line verdict (e.g. "Fine to merge after fixing 1 and 2"). If you
found nothing that matters, say so plainly; don't invent findings.
