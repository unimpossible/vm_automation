"""Build shim: copy the repo-root docs into the package at build time.

Everything else is declared in pyproject.toml. `README.md` and `SKILL.md` are
tracked once, at the repo root (where GitHub and PyPI look for them); a wheel
also needs them *inside* `vm_cli/` so `vm docs` can find them after a pip
install. This copies them into `vm_cli/_docs/` as part of the build instead of
keeping a second checked-in copy -- the generated copies are gitignored.
"""

import os
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGED_DOCS = ("README.md", "SKILL.md")
DOCS_SUBDIR = os.path.join("vm_cli", "_docs")


def stage_docs():
    """Refresh vm_cli/_docs/ from the repo-root originals."""
    dest_dir = os.path.join(HERE, DOCS_SUBDIR)
    if not os.path.isdir(dest_dir):
        os.makedirs(dest_dir)
    for name in PACKAGED_DOCS:
        src = os.path.join(HERE, name)
        if os.path.isfile(src):  # absent when building from a trimmed tree
            shutil.copyfile(src, os.path.join(dest_dir, name))


class BuildPyWithDocs(build_py):
    def run(self):
        stage_docs()
        build_py.run(self)


setup(cmdclass={"build_py": BuildPyWithDocs})
