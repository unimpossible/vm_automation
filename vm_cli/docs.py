"""Packaged documentation - the README and the vm-recovery skill.

`README.md` and `SKILL.md` are tracked once, at the repo root, where GitHub and
PyPI look for them. A pip install only puts `vm_cli/` on disk, though, so an
agent told to "read the README" would have nothing to open -- setup.py copies
both into `vm_cli/_docs/` when building the wheel (those copies are gitignored,
never a second checked-in original). This module resolves whichever of the two
locations exists and exposes them through `vm docs`.
"""

import os
import shutil

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
# Wheel/sdist install: staged inside the package by setup.py at build time.
PACKAGED_DIR = os.path.join(_PKG_DIR, "_docs")
# Source checkout or editable install: the tracked originals, one level up.
SOURCE_DIR = os.path.dirname(_PKG_DIR)

# doc name -> filename
DOCS = {
    "readme": "README.md",
    "skill": "SKILL.md",
}

SKILL_INSTALL_REL = os.path.join(".claude", "skills", "vm-recovery", "SKILL.md")


def doc_path(name):
    """Absolute path of a doc: the packaged copy, else the repo-root original.

    Returns None if neither exists (a build that skipped the staging step).
    """
    fname = DOCS.get(name)
    if not fname:
        return None
    for d in (PACKAGED_DIR, SOURCE_DIR):
        path = os.path.join(d, fname)
        if os.path.isfile(path):
            return path
    return None


def read_doc(name):
    path = doc_path(name)
    if path is None:
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def install_skill(base_dir):
    """Copy SKILL.md to <base_dir>/.claude/skills/vm-recovery/.

    Returns the destination path. Raises IOError if the doc can't be found.
    """
    src = doc_path("skill")
    if src is None:
        raise IOError("SKILL.md is not available in this install")
    dest = os.path.join(base_dir, SKILL_INSTALL_REL)
    dest_dir = os.path.dirname(dest)
    if not os.path.isdir(dest_dir):
        os.makedirs(dest_dir)
    shutil.copyfile(src, dest)
    return dest
