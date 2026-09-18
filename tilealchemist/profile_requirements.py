#!/usr/bin/env python3
"""A profile file's own dependencies, one PEP 508 requirement per line.

A profile declares whatever it needs beyond tilealchemist's own dependencies
inline, in a PEP 723 script-metadata block at the top of its .py file:

    # /// script
    # dependencies = ["scipy"]
    # ///

so a profile stays one self-contained file with nothing to distribute
alongside it — the same reason `load_profile()` takes a path and asks only
for `PROFILE`. See docs/PROFILES.md ("Writing and distributing your own
profile").

The block is a comment, and MUST be read without importing the profile.
That is the point of using it rather than a module-level list: the
dependencies have to be installed *before* the profile can be imported at
all, so anything that executed the file first would be useless here.

    tilealchemist-profile-requirements ./my_profile.py > requirements.txt
"""
import argparse
import re
import tomllib

# PEP 723's own reference regex for a script-metadata block, verbatim.
BLOCK_PATTERN = r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"

# The one block type PEP 723 defines, and the only one carrying dependencies.
BLOCK_TYPE = "script"

# A block opener on a line of its own. Counted separately from the matches
# above, because that regex's content class accepts any comment line, `# ///`
# and `# /// script` included. A second block butted straight up against the
# first one's closing delimiter is swallowed whole into the first one's
# content, leaving one match and a TOML syntax error rather than two.
# Counting openers reports the duplicate the same way wherever it sits.
OPENER_PATTERN = rf"(?m)^# /// {BLOCK_TYPE}$"

# An opener closed immediately, declaring nothing. Matched separately because
# the reference regex's content class needs at least one line, so a block
# this empty matches nothing at all. It is valid; it just carries no
# dependencies. Telling it apart from a block that never closed is the only
# reason to look for it.
EMPTY_BLOCK_PATTERN = rf"(?m)^# /// {BLOCK_TYPE}$\s^# ///$"


def read_metadata(source):
    """`source`'s parsed PEP 723 `script` block, empty if it has none."""
    openers = len(re.findall(OPENER_PATTERN, source))
    if openers > 1:
        raise ValueError(f"{openers} `# /// {BLOCK_TYPE}` blocks found; PEP 723 allows one")
    block = next((match for match in re.finditer(BLOCK_PATTERN, source)
                  if match.group("type") == BLOCK_TYPE), None)
    if block is None:
        # An unclosed block MUST be reported, not returned as {}. A profile
        # with a malformed block would install none of what it needs, then
        # fail on the very import the block exists to make possible.
        if openers and not re.search(EMPTY_BLOCK_PATTERN, source):
            raise ValueError(f"`# /// {BLOCK_TYPE}` block has no closing `# ///`")
        return {}
    # Strip the comment prefix off every line before the TOML parser sees it:
    # "# " on a line with content, bare "#" on a blank one.
    content = "".join(line[2:] if line.startswith("# ") else line[1:]
                      for line in block.group("content").splitlines(keepends=True))
    return tomllib.loads(content)


def profile_requirements(path):
    """`path`'s declared dependencies, empty when it needs nothing beyond
    tilealchemist's own.

    Empty is the common case: a profile doing nothing but shapely geometry,
    and shapely is already a tilealchemist dependency."""
    with open(path, encoding="utf-8") as profile_file:
        metadata = read_metadata(profile_file.read())
    requirements = metadata.get("dependencies", [])
    if not isinstance(requirements, list) or not all(
            isinstance(requirement, str) for requirement in requirements):
        raise ValueError("`dependencies` must be an array of requirement strings")
    return requirements


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("profile", help="path to the profile's .py file")
    args = parser.parse_args()
    try:
        requirements = profile_requirements(args.profile)
    except (OSError, ValueError) as error:
        # Not a traceback. This runs as one step of a CI install script,
        # where the useful output is which profile is malformed and why.
        parser.error(f"{args.profile}: {error}")
    for requirement in requirements:
        print(requirement)


if __name__ == "__main__":
    main()