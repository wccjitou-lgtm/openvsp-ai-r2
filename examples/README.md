# Examples

`offline_config.json` is a no-key, offline demo config. Paths are relative to the
repository root. It requires an OpenVSP 3.51.3 installation; set:

```powershell
$env:OPENVSP_ROOT = "C:\OpenVSP-3.51.3-win64"
python ai\ai_design_agent.py --offline --config examples\offline_config.json
```

Generated models and reports go to `examples/offline_run/` and are ignored by Git.
