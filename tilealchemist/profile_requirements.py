#!/usr/bin/env python3
"""A profile file's own dependencies, read without importing it."""
import argparse
import re
import tomllib

HELP = """A profile file's own dependencies, one PEP 508 requirement per line.

A profile declares whatever it needs beyond tilealchemist's own dependencies
inline, in a PEP 723 script-metadata block at the top of its .py file:

    # /// script
    # dependencies = ["scipy"]
    # ///

See docs/PROFILES.md ("Writing and distributing your own profile") for why
the block is read without importing the profile.

    tilealchemist-profile-requirements ./my_profile.py > requirements.txt
"""

# PEP 723's own reference regex for a script-metadata block, verbatim.
BLOCK_PATTERN = r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"

BLOCK_TYPE = "script"

# Counted separately: the reference regex swallows a butted-up second block into the first.
OPENER_PATTERN = rf"(?m)^# /// {BLOCK_TYPE}$"

# Matched separately: the reference regex needs a content line, so an empty block matches none.
EMPTY_BLOCK_PATTERN = rf"(?m)^# /// {BLOCK_TYPE}$\s^# ///$"


def read_metadata(source):
    openers = len(re.findall(OPENER_PATTERN, source))
    if openers > 1:
        raise ValueError(f"{openers} `# /// {BLOCK_TYPE}` blocks found; PEP 723 allows one")
    block = next((match for match in re.finditer(BLOCK_PATTERN, source)
                  if match.group("type") == BLOCK_TYPE), None)
    if block is None:
        if openers and not re.search(EMPTY_BLOCK_PATTERN, source):
            raise ValueError(f"`# /// {BLOCK_TYPE}` block has no closing `# ///`")
        return {}
    # "# " on a line with content, bare "#" on a blank one, before TOML sees it.
    content = "".join(line[2:] if line.startswith("# ") else line[1:]
                      for line in block.group("content").splitlines(keepends=True))
    return tomllib.loads(content)


def profile_requirements(path):
    with open(path, encoding="utf-8") as profile_file:
        metadata = read_metadata(profile_file.read())
    requirements = metadata.get("dependencies", [])
    if not isinstance(requirements, list) or not all(
            isinstance(requirement, str) for requirement in requirements):
        raise ValueError("`dependencies` must be an array of requirement strings")
    return requirements


def main():
    parser = argparse.ArgumentParser(
        description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("profile", help="path to the profile's .py file")
    args = parser.parse_args()
    try:
        requirements = profile_requirements(args.profile)
    except (OSError, ValueError) as error:
        parser.error(f"{args.profile}: {error}")
    for requirement in requirements:
        print(requirement)


if __name__ == "__main__":
    main()
