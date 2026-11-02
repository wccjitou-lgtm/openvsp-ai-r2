# Scripts

These are source utilities, not a one-click installer.

- `freeze_reliable_baseline.py`: write a SHA-256 source manifest to
  `validation/reliable_design_v2/M0/`.
- `package_r2.py`: build the portable R2 package. It expects an OpenVSP 3.51.3
  source tree and Python API; set `OPENVSP_SRC` and `OPENVSP_PYTHONPATH` instead
  of editing local paths.
- `import_profili.ps1`: **optional** import of a user's own licensed Profili
  installation into a local cache. It does not redistribute Profili data.

No script contains API keys. Generated portable builds are intentionally not
committed to this repository.
