"""Enforces this repository's comment style; see docs/COMMENT_STYLE.md."""
import argparse
import ast
import io
import pathlib
import re
import sys
import tokenize

MAX_COMMENTS_PER_FILE = 5
DIRECTIVE = re.compile(
    r"^#\s*(?:!|-\*-|coding[:=]|noqa\b|type:\s|pragma:|fmt:\s*(?:on|off|skip)\b"
    r"|(?:ruff|flake8|mypy|pylint|pyright|isort|black|nosec|codespell):)"
)


def _is_directive(text):
    return bool(DIRECTIVE.match(text.strip()))


def _docstring_nodes(tree):
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            yield first.value


def _check_docstrings(tree, problems):
    for node in _docstring_nodes(tree):
        if node.end_lineno != node.lineno:
            problems.append((node.lineno, "TA504",
                             f"docstring spans {node.end_lineno - node.lineno + 1} lines; "
                             "keep it to one line and move the rest to docs/"))


def _check_blocks(own_line, lines, problems):
    numbers = {number for number, _ in own_line}
    for index, (number, _text) in enumerate(own_line):
        if number - 1 in numbers:
            continue
        run = 1
        while number + run in numbers:
            run += 1
        if run > 1:
            problems.append((number, "TA501",
                             f"comment block spans {run} lines; comments must be a "
                             "single line, with the rest moved to docs/"))
        following = lines[number + run - 1:]
        if not following or not following[0].strip():
            problems.append((number, "TA502",
                             "comment is not attached to the code it comments on; "
                             "put it directly above that line"))


def check(path):
    source = path.read_text()
    problems = []
    try:
        tree = ast.parse(source, filename=str(path))
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, tokenize.TokenError) as error:
        return [(getattr(error, "lineno", 1) or 1, "TA500", f"cannot parse: {error}")]
    lines = source.splitlines()
    comments = [t for t in tokens
                if t.type == tokenize.COMMENT and not _is_directive(t.string)]
    own_line = [(t.start[0], t.string) for t in comments
                if not lines[t.start[0] - 1][:t.start[1]].strip()]
    _check_blocks(own_line, lines, problems)
    _check_docstrings(tree, problems)
    blocks = len({number for number, _ in own_line}
                 - {number - 1 for number, _ in own_line})
    total = blocks + (len(comments) - len(own_line)) + len(list(_docstring_nodes(tree)))
    if total > MAX_COMMENTS_PER_FILE:
        problems.append((1, "TA503",
                         f"file carries {total} comments and docstrings "
                         f"(max {MAX_COMMENTS_PER_FILE}); the rest belongs in docs/"))
    return sorted(problems)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=["."], type=pathlib.Path)
    arguments = parser.parse_args(argv)
    targets = sorted({file for path in arguments.paths
                      for file in ([path] if path.is_file() else path.rglob("*.py"))
                      if ".claude" not in file.parts and "build" not in file.parts})
    problems = [(file, *problem) for file in targets for problem in check(file)]
    for file, line, code, message in problems:
        print(f"{file}:{line}: {code} {message}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
