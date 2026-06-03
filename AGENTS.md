# Repository Memory

This repository is `Color2333/automodel`.

## Branch Layout

- `main`: Meshy AutoModel plugin.
- `logoslot`: Meshy AutoGLB LogoSlot plugin.
- Do not add `LogoSlot_Boolean_Fixer` until explicitly requested.

## Git Sync Rule

Any future plugin adjustment in this repository must be synchronized to git:

- Commit the change on the correct branch.
- Push the branch to `origin`.
- Keep `README.md` and `changelog.txt` updated when behavior changes.

## Packaging Rule

- Do not commit zip archives.
- Do not commit `__pycache__` or `*.pyc`.
- Source files should remain installable as Blender add-on source.

