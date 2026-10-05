---
name: release
description: Cut a release - bump the version, update the changelog, tag.
---
# Release

1. Bump `__version__` in `src/<package>/__init__.py`.
2. Add a dated section to CHANGELOG.md.
3. Run the tests, then `git tag v<version>`.
