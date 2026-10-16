# Project conventions

## Versioning

**Bump `custom_components/samsungtv_smart/manifest.json` on every change, without
being asked.** Every PR that touches the integration carries its own version
bump — the maintainer should never have to ask for it.

- Bug fix / logging / docs-only change affecting the integration → patch
  (`8.12.3` → `8.12.4`).
- New feature or user-visible option → minor (`8.12.x` → `8.13.0`).
- Put the bump in the same PR as the change. A separate `chore: bump` commit
  inside that PR is fine.

Releases themselves are published by the maintainer.
