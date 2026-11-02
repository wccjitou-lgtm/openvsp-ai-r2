# OpenVSP GUI Patch

`r2-ui.patch` is the diff of the R2 AI menu/dialog changes against OpenVSP
3.51.3. `source-changes/` contains the corresponding changed files.

Apply to a clean OpenVSP 3.51.3 source tree:

```powershell
git apply patches/r2-ui.patch
```

Then build OpenVSP with CMake/Visual Studio 2022. OpenVSP source and these
derived files are under NASA NOSA 1.3; see the repository `LICENSE` and
`NOTICE.md`. The repository does not include OpenVSP binaries.
