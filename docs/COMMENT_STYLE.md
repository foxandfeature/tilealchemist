# Comment style

Code is self-documenting. A reader learns *what* the code does by reading
the code, so comments here carry only what the code cannot say for itself.
Everything else — why a design was chosen, what a production run cost, which
edge case forced a workaround — is documentation, and lives in
[`ARCHITECTURE.md`](ARCHITECTURE.md), [`PROFILES.md`](PROFILES.md), or the
[README](../README.md).

`lint/comment_style.py` enforces this. It runs on every push and pull request
(`.github/workflows/lint.yml`) and takes paths to check:

```sh
python3 lint/comment_style.py tilealchemist lint
```

## The rules

| Code | Rule |
| --- | --- |
| `TA501` | A comment is one line. No stacked `#` lines forming a paragraph. |
| `TA502` | A comment is attached to the code it comments on: the next line is that code, with no blank line between them. |
| `TA503` | A file carries at most **5** comments and docstrings, counted together. |
| `TA504` | A docstring is one line. |
| `TA500` | The file could not be parsed. |

A comment block counts once, not once per line. Trailing comments on a line
of code count too. Tool directives — `# noqa`, `# type:`, `# fmt: off`,
`# ruff:`, shebangs, coding declarations — are exempt from every rule; they
are instructions to other programs, not prose.

## What the five are for

Five per file is a budget, not a target. Most files should spend fewer. Spend
them on what the code genuinely cannot state:

- A constant whose value came from measurement rather than derivation.
- A non-obvious ordering or precondition a future edit would silently break.
- A workaround for a specific upstream bug, naming the upstream.
- A pointer into the documentation for the reasoning behind a shape that
  looks wrong at a glance: `# Offset order, not tile order; see
  docs/ARCHITECTURE.md "Fetching".`

Do not spend them restating a name, narrating control flow, or sectioning a
function into paragraphs. A function that needs sectioning wants splitting.

## Comments discuss the code in front of them

A comment describes the code directly below it, and only that code. This is
why `TA502` rejects a blank line after a comment: the blank line is what turns
a comment about the next statement into a floating header about a region.

```python
# Wrong: a header over a region, detached from any one statement.
#
# It also explains a decision, which belongs in docs/.

entries = sorted(...)
```

```python
# Offset order, not tile order; see docs/ARCHITECTURE.md "Fetching".
entries = sorted(raw, key=operator.attrgetter("offset"))
```

## Running out of budget

Hitting `TA503` is a signal about the code, not about the limit. The usual
causes, in the order worth checking:

1. The rationale is documentation. Move it to `ARCHITECTURE.md` and leave a
   one-line pointer, or no pointer at all if the code reads fine without one.
2. The names are carrying less than they could. A comment explaining what a
   variable holds is a variable that wants renaming.
3. The file is doing several jobs. Split it, and each part gets its own five.

Raising `MAX_COMMENTS_PER_FILE` is not one of the causes.
