---
name: relay-skill-writing
description: Write a new skill for Relay (a folder with a SKILL.md in Relay's own skills folder). Use when asked to make or save a skill, or to teach Relay a reusable way of doing something.
---

# Writing a Relay skill

A skill is a folder in Relay's skills folder (Relay tells you its path) with
a `SKILL.md`, and optionally scripts or reference files next to it:

```
<skills folder>/release-notes/
    SKILL.md
    make_notes.py      (optional)
```

`SKILL.md` starts with a frontmatter:

```markdown
---
name: release-notes
description: Writes release notes from the git log since the last tag. Use when asked for release notes or a changelog.
access: read
---

# Release notes

1. Find the last tag: `git describe --tags --abbrev=0`.
2. …
```

- **name**: lowercase letters, digits and dashes; the same as the folder.
- **description**: what it does *and when to use it*, in one or two
  sentences. Agents only see this until they decide to use the skill, so
  make it specific.
- **access** (optional): `read` if following the skill never changes files;
  leave it out otherwise.
- The body: the steps, as you'd explain them to a capable colleague. Keep it
  short; put long reference material in separate files and say when to read
  them. Scripts are referred to by their path relative to the skill folder.

Every agent in Relay can use the skill from the next message on. The user
can see and edit it under Settings → Skills.
