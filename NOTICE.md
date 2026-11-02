# NOTICE

This repository is an overlay/extension around OpenVSP 3.51.3.

## OpenVSP

OpenVSP source and binaries are released under the NASA Open Source Agreement
(NOSA) 1.3. The root `LICENSE` is the OpenVSP license. Files under
`patches/source-changes/` are modifications of OpenVSP files and remain
subject to NOSA. No OpenVSP binary or full OpenVSP source tree is included.

## Original overlay

Original AI backend, MCP server, documentation and build scripts in this
repository are offered under the MIT license in `LICENSE-AI`, except where a
file states otherwise or is derived from OpenVSP.

## Third-party dependencies

Python packages declared in `ai/requirements.txt` are installed separately and
remain under their own licenses.

## Profili and proprietary data

Profili is commercial software. No Profili database, airfoil export, serial
number or local cache is included. The optional `scripts/import_profili.ps1`
only reads a user's own licensed local installation and creates a local cache;
users are responsible for complying with Profili's license.

## No warranty

This is research/concept-design assistance, not an airworthiness, structural,
flight-release or test-flight tool.
