#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenVSP R2 MCP server (stdio, JSON-RPC 2.0, newline-delimited).

A dependency-free (standard library only) MCP server that exposes the OpenVSP
Python API (`D:\\Zcoding\\OpenVSP-3.51.3-win64\\python\\openvsp`) and VSPAERO as
MCP tools.

Design notes
------------
* stdout is reserved for protocol frames only.  At start-up the real stdout
  file descriptor is duplicated and fd 1 is redirected to stderr, so any
  C++ `printf` produced by the OpenVSP API / VSPAERO cannot corrupt the MCP
  stream.  stdout is likewise replaced by /dev/null so child processes that
  inherit fd 0 cannot steal protocol lines from the client.
* The server implements the **legacy** MCP lifecycle (`initialize` ->
  `notifications/initialized` -> `tools/list` / `tools/call`) with
  protocolVersion ``2025-11-25`` (also accepts ``2025-06-18`` /
  ``2024-11-05``).  The modern (2026-07-28) era is detected and answered with
  an explicit degradation error, see ``MODERN_PROTOCOL``.
* Heavy VSPAERO work runs in a short-lived worker subprocess (same file with
  ``--worker``) so a hung solver can be killed by timeout and the protocol
  loop stays responsive.  ``OPENVSP_PYTHON`` selects the interpreter used for
  that worker (defaults to ``sys.executable``).
* If the OpenVSP API cannot be imported in-process, all VSP tools are routed
  to the worker subprocess automatically (file-based state), so the server can
  be launched by an interpreter that only has the MCP server itself.
* Optional AI backend: ``../ai`` (package ``ai_design``) is imported when
  available and used for report generation; otherwise a local Word report is
  generated (python-docx when installed, otherwise a minimal built-in .docx
  writer).

Usage
-----
    python openvsp_mcp_server.py            # run MCP stdio server
    python openvsp_mcp_server.py --status   # print JSON environment status
    python openvsp_mcp_server.py --self-test# run a short tool smoke test
    python openvsp_mcp_server.py --version
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Identity / protocol constants
# ---------------------------------------------------------------------------

SERVER_NAME = "openvsp-mcp"
SERVER_TITLE = "OpenVSP R2 MCP Server"
SERVER_VERSION = "0.1.0"

#: Protocol version returned by this server (legacy MCP lifecycle).
PROTOCOL_VERSION = "2025-11-25"
#: Legacy versions that may be echoed back when a client asks for them.
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2024-11-05")
#: Modern era that is *not* implemented (detected to answer with a clear hint).
MODERN_PROTOCOL = "2026-07-28"

DEFAULT_OPENVSP_ROOT = os.environ.get("OPENVSP_ROOT", r"C:\OpenVSP-3.51.3-win64")

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Tool error codes (string, returned inside result.isError content)
ERR_INVALID_PARAMS = "invalid_params"
ERR_VSP_UNAVAILABLE = "vsp_unavailable"
ERR_NO_MODEL = "no_model"
ERR_ANALYSIS_FAILED = "analysis_failed"
ERR_EXPORT_FAILED = "export_failed"
ERR_REPORT_FAILED = "report_failed"
ERR_WORKER_FAILED = "worker_failed"
ERR_WORKER_TIMEOUT = "worker_timeout"
ERR_IO = "io_error"
ERR_INTERNAL = "internal"

INSTRUCTIONS = (
    "OpenVSP R2 MCP server (legacy stdio MCP, protocolVersion 2025-11-25). "
    "Tools: openvsp_status, openvsp_new_model, openvsp_build_aircraft, "
    "openvsp_get_geometry, openvsp_run_vspaero, openvsp_export, "
    "openvsp_generate_report, openvsp_list_files. "
    "Call openvsp_status first to verify the OpenVSP installation. "
    "Typical flow: openvsp_build_aircraft -> openvsp_get_geometry -> "
    "openvsp_run_vspaero -> openvsp_export / openvsp_generate_report. "
    "VSPAERO runs take seconds to minutes. All generated files live under the "
    "server workspace directory (see openvsp_status)."
)

# ---------------------------------------------------------------------------
# Logging (stderr only)
# ---------------------------------------------------------------------------

_DEBUG, _INFO, _WARN, _ERROR = 10, 20, 30, 40
_LOG_LEVEL = _INFO
_LEVEL_NAMES = {_DEBUG: "DEBUG", _INFO: "INFO", _WARN: "WARN", _ERROR: "ERROR"}


def _set_log_level(name: str) -> None:
    global _LOG_LEVEL
    key = (name or "INFO").upper()
    _LOG_LEVEL = {"DEBUG": _DEBUG, "INFO": _INFO, "WARN": _WARN,
                  "WARNING": _WARN, "ERROR": _ERROR}.get(key, _INFO)


def _log(level: int, message: str, *args) -> None:
    if level < _LOG_LEVEL:
        return
    try:
        text = message % args if args else message
    except Exception:  # noqa: BLE001 - logging must never break the server
        text = message
    stamp = datetime.now().strftime("%H:%M:%S")
    try:
        sys.stderr.write("[%s] %s %s\n" % (_LEVEL_NAMES.get(level, "INFO"), stamp, text))
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _truthy(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _safe_name(name: str, fallback: str = "model") -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(name or "").strip())
    cleaned = cleaned.strip("._")
    return cleaned[:80] or fallback


def _tail(data, limit: int = 4000) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    text = str(data).strip()
    if len(text) > limit:
        return "..." + text[-limit:]
    return text


def _json_safe(obj, depth: int = 0):
    """Recursively make an object JSON-safe (NaN/Inf -> null, Path -> str)."""
    if depth > 60:
        return None
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json_safe(v, depth + 1) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


def _as_float(value, field: str, default=None, lo=None, hi=None, required=False):
    if value is None:
        if required:
            raise ToolError(ERR_INVALID_PARAMS, "缺少必需参数: %s" % field)
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ToolError(ERR_INVALID_PARAMS, "%s 必须是数字，收到: %r" % (field, value))
    if not math.isfinite(number):
        raise ToolError(ERR_INVALID_PARAMS, "%s 必须是有限数字" % field)
    if lo is not None and number < lo:
        raise ToolError(ERR_INVALID_PARAMS, "%s 必须 >= %s（收到 %s）" % (field, lo, number))
    if hi is not None and number > hi:
        raise ToolError(ERR_INVALID_PARAMS, "%s 必须 <= %s（收到 %s）" % (field, hi, number))
    return number


def _parse_triple(value, default, field: str, lo: float, hi: float):
    """Normalise a scalar / [a,b] / [a,b,n] parameter into (start, end, npts)."""
    if value is None:
        return tuple(default)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = _as_float(value, field, lo=lo, hi=hi)
        return (v, v, 1)
    if isinstance(value, (list, tuple)):
        values = [_as_float(v, "%s[%d]" % (field, i), lo=lo, hi=hi)
                  for i, v in enumerate(value)]
        if len(values) == 1:
            return (values[0], values[0], 1)
        if len(values) == 2:
            start, end, npts = values[0], values[1], 3
        elif len(values) == 3:
            start, end = values[0], values[1]
            npts = int(round(values[2]))
            if npts < 1 or npts > 51:
                raise ToolError(ERR_INVALID_PARAMS, "%s 点数必须在 1..51 之间" % field)
        else:
            raise ToolError(ERR_INVALID_PARAMS, "%s 数组长度必须为 1..3" % field)
        if start > end:
            start, end = end, start
        return (start, end, npts)
    raise ToolError(ERR_INVALID_PARAMS, "%s 必须是数字或数组" % field)


def _linreg(xs, ys):
    """Least-squares slope/intercept of ys = slope*xs + intercept."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-18:
        return None
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    if not (math.isfinite(slope) and math.isfinite(intercept)):
        return None
    return slope, intercept


def _interp(xs, ys, target):
    """Linear interpolation through sorted (xs, ys)."""
    pairs = sorted(zip(xs, ys), key=lambda p: p[0])
    if not pairs:
        return None
    if target <= pairs[0][0]:
        return pairs[0][1]
    if target >= pairs[-1][0]:
        return pairs[-1][1]
    for (x0, y0), (x1, y1) in zip(pairs, pairs[1:]):
        if x0 <= target <= x1:
            if abs(x1 - x0) < 1e-12:
                return y0
            return y0 + (y1 - y0) * (target - x0) / (x1 - x0)
    return None


def _find_header_index(lines, needles):
    for idx, line in enumerate(lines):
        tokens = line.split()
        if all(n in tokens for n in needles):
            return idx, tokens
    return None, None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """A tool execution error -> returned as result.isError content."""

    def __init__(self, code: str, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self):
        payload = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = _json_safe(self.data)
        return payload


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self):
        payload = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = _json_safe(self.data)
        return payload


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class Config:
    def __init__(self, root, workdir, python, pythonpath, ai_dir, aero_set,
                 force_subprocess, analysis_subprocess, worker_timeout,
                 analysis_timeout, log_level):
        self.root = root
        self.workdir = workdir
        self.python = python
        self.pythonpath = pythonpath
        self.ai_dir = ai_dir
        self.aero_set = aero_set
        self.force_subprocess = force_subprocess
        self.analysis_subprocess = analysis_subprocess
        self.worker_timeout = worker_timeout
        self.analysis_timeout = analysis_timeout
        self.log_level = log_level

    # -- OpenVSP python API search path (order matters!) ---------------------
    def python_api_paths(self):
        derived = [
            os.path.join(self.root, "python", "openvsp"),
            os.path.join(self.root, "python", "utilities"),
            os.path.join(self.root, "python", "degen_geom"),
            os.path.join(self.root, "python", "openvsp_config"),
            os.path.join(self.root, "python", "openvsp", "openvsp"),
        ]
        extra = [p for p in self.pythonpath if p and p not in derived]
        return derived + extra

    def ensure_workdir(self) -> Path:
        path = Path(self.workdir)
        path.mkdir(parents=True, exist_ok=True)
        return path


def resolve_config() -> Config:
    env = os.environ
    here = Path(__file__).resolve().parent

    root = (env.get("OPENVSP_ROOT") or "").strip()
    if not root:
        for candidate in (DEFAULT_OPENVSP_ROOT, r"C:\OpenVSP-3.51.3-win64",
                          r"C:\Program Files\OpenVSP"):
            if Path(candidate).is_dir():
                root = candidate
                break
    if not root:
        root = DEFAULT_OPENVSP_ROOT

    workdir = (env.get("OPENVSP_MCP_WORKDIR") or "").strip() or str(here / "workspace")
    python = (env.get("OPENVSP_PYTHON") or "").strip() or sys.executable

    pp_raw = env.get("OPENVSP_PYTHONPATH") or ""
    pythonpath = [p.strip() for p in pp_raw.split(os.pathsep) if p.strip()]

    ai_dir = (env.get("OPENVSP_MCP_AI_DIR") or "").strip() or str(here.parent / "ai")

    try:
        aero_set = int(env.get("OPENVSP_MCP_AERO_SET") or 3)
    except ValueError:
        aero_set = 3
    if aero_set < 3:
        aero_set = 3

    try:
        worker_timeout = float(env.get("OPENVSP_MCP_WORKER_TIMEOUT") or 1800)
    except ValueError:
        worker_timeout = 1800.0
    try:
        analysis_timeout = float(env.get("OPENVSP_MCP_ANALYSIS_TIMEOUT") or 900)
    except ValueError:
        analysis_timeout = 900.0

    return Config(
        root=root,
        workdir=workdir,
        python=python,
        pythonpath=pythonpath,
        ai_dir=ai_dir,
        aero_set=aero_set,
        force_subprocess=_truthy(env.get("OPENVSP_MCP_FORCE_SUBPROCESS")),
        analysis_subprocess=_truthy(env.get("OPENVSP_MCP_ANALYSIS_SUBPROCESS", "1")),
        worker_timeout=worker_timeout,
        analysis_timeout=analysis_timeout,
        log_level=(env.get("OPENVSP_MCP_LOG_LEVEL") or "INFO").strip().upper(),
    )


# ---------------------------------------------------------------------------
# Optional AI backend (<REPO_ROOT>\ai, package ai_design)
# ---------------------------------------------------------------------------


def _load_ai_design(cfg: Config):
    ai_dir = Path(cfg.ai_dir)
    if not ai_dir.is_dir():
        return None
    try:
        resolved = str(ai_dir.resolve())
        if resolved not in sys.path:
            sys.path.insert(0, resolved)
        import ai_design  # noqa: F401 - optional dependency
        return ai_design
    except Exception as exc:  # noqa: BLE001 - optional dependency
        _log(_DEBUG, "ai_design 不可用: %s", exc)
        return None


def _ai_design_modules():
    """Report which optional ai_design submodules are importable (no side effects)."""
    import importlib.util

    found = []
    for name in ("report", "vsp_bridge", "designer", "tools", "agent"):
        try:
            if importlib.util.find_spec("ai_design." + name) is not None:
                found.append(name)
        except (ImportError, ValueError, AttributeError):
            continue
    return found


def _import_ai_report_module(ai):
    """Import ``ai_design.report`` explicitly (a package may not export it eagerly)."""
    module = getattr(ai, "report", None)
    if module is not None:
        return module
    try:
        import importlib

        return importlib.import_module("ai_design.report")
    except Exception:  # noqa: BLE001 - optional dependency
        return None


_AI_ROW_ALIASES = (
    ("AoA", "alpha_deg"), ("Alpha", "alpha_deg"), ("alpha", "alpha_deg"),
    ("Beta", "beta_deg"), ("beta", "beta_deg"), ("Mach", "mach"),
    ("CLtot", "CL"), ("CDtot", "CD"), ("CStot", "CS"),
    ("CMytot", "Cm"), ("CMxtot", "Cl"), ("CMztot", "Cn"),
)


def _adapt_analysis_for_ai(analysis):
    """Add VSPAERO/OpenVSP column aliases expected by ai_design.report.

    Our tool rows use ``alpha_deg/CL/CD/...`` while ``ai_design.report`` was
    written against ``AoA/CLtot/CDtot/...`` column names.  Supplying both keeps
    the optional AI report path (tables + matplotlib figures) working without
    touching the AI backend.
    """
    if not isinstance(analysis, dict):
        return analysis
    try:
        adapted = json.loads(json.dumps(_json_safe(analysis)))
    except (TypeError, ValueError):
        return analysis
    rows = adapted.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            for alias, key in _AI_ROW_ALIASES:
                if alias not in row and row.get(key) is not None:
                    row[alias] = row[key]
    return adapted


def _call_flexible(func, payload: dict):
    """Call ``func`` with the subset of payload that matches its signature."""
    import inspect

    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return func(**payload)
    kwargs = {}
    has_var_kw = False
    for name, param in signature.parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            has_var_kw = True
            continue
        if param.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                              inspect.Parameter.KEYWORD_ONLY):
            continue
        if name in payload:
            kwargs[name] = payload[name]
        elif param.default is inspect.Parameter.empty:
            raise TypeError("ai_design 报告接口需要未知参数 %r" % name)
    if has_var_kw:
        for key, value in payload.items():
            kwargs.setdefault(key, value)
    return func(**kwargs)


# ---------------------------------------------------------------------------
# Polar / stability result parsing
# ---------------------------------------------------------------------------

_STAB_COEFS = ("CFx", "CFy", "CFz", "CMx", "CMy", "CMz",
               "CL", "CD", "CS", "CMl", "CMm", "CMn")
_STAB_DERIV_KEYS = ("base", "alpha", "beta", "p", "q", "r", "mach", "u")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def parse_polar(path: Path):
    """Parse a VSPAERO ``.polar`` file into (header, rows[dict[str,float]])."""
    lines = _read_text(path).splitlines()
    header_idx, header = _find_header_index(lines, ("AoA", "CLtot", "CDtot"))
    if header_idx is None:
        raise ToolError(ERR_ANALYSIS_FAILED,
                        "无法解析 .polar：未找到 AoA/CLtot/CDtot 表头（%s）" % path)
    rows = []
    for line in lines[header_idx + 1:]:
        tokens = line.split()
        if len(tokens) < len(header):
            continue
        try:
            values = [float(t) for t in tokens[:len(header)]]
        except ValueError:
            continue
        if any(not math.isfinite(v) for v in values):
            continue
        rows.append(dict(zip(header, values)))
    return header, rows


def _normalise_polar_rows(rows):
    out = []
    for row in rows:
        cl = row.get("CLtot")
        cd = row.get("CDtot")
        ld = row.get("L/D")
        if ld is None or not math.isfinite(ld):
            ld = (cl / cd) if (cd not in (None, 0.0) and cl is not None) else None
        out.append({
            "alpha_deg": row.get("AoA"),
            "beta_deg": row.get("Beta"),
            "mach": row.get("Mach"),
            "re_million": row.get("Re/1e6"),
            "CL": cl,
            "CD": cd,
            "LD": ld,
            "CS": row.get("CStot"),
            "Cm": row.get("CMytot"),
            "Cl": row.get("CMxtot"),
            "Cn": row.get("CMztot"),
            "CL_induced": row.get("CLi"),
            "CD_induced": row.get("CDi"),
            "CD_zero_lift": row.get("CDo"),
        })
    return out


def _polar_summary(rows, refs: dict):
    """Derived aerodynamic metrics from polar rows (all quantities SI/deg)."""
    valid = [r for r in rows if r.get("alpha_deg") is not None and r.get("CL") is not None]
    if not valid:
        return {}
    valid.sort(key=lambda r: r["alpha_deg"])
    summary = {
        "n_points": len(valid),
        "alpha_min_deg": valid[0]["alpha_deg"],
        "alpha_max_deg": valid[-1]["alpha_deg"],
    }
    cl_max_row = max(valid, key=lambda r: r["CL"])
    summary["CL_max"] = cl_max_row["CL"]
    summary["alpha_at_CL_max_deg"] = cl_max_row["alpha_deg"]

    ld_rows = [r for r in valid if r.get("LD") is not None and math.isfinite(r["LD"])]
    if ld_rows:
        ld_max_row = max(ld_rows, key=lambda r: r["LD"])
        summary["LD_max"] = ld_max_row["LD"]
        summary["alpha_at_LD_max_deg"] = ld_max_row["alpha_deg"]
        summary["CD_at_LD_max"] = ld_max_row["CD"]

    # Lift curve slope / zero-lift angle
    alpha = [r["alpha_deg"] for r in valid]
    cl = [r["CL"] for r in valid]
    if len(set(alpha)) >= 2:
        reg = _linreg(alpha, cl)
        if reg:
            slope_deg = reg[0]
            summary["CL_alpha_per_deg"] = slope_deg
            summary["CL_alpha_per_rad"] = slope_deg * 180.0 / math.pi
            summary["CL_at_alpha0"] = reg[1]
            if abs(slope_deg) > 1e-9:
                summary["alpha_zero_lift_deg"] = -reg[1] / slope_deg

    # Drag polar: CD = CD0 + k*CL^2 -> CD0, induced factor k, Oswald e
    if len(valid) >= 3:
        cl2 = [r["CL"] * r["CL"] for r in valid]
        cd = [r["CD"] for r in valid]
        if max(cl2) - min(cl2) > 1e-6:
            reg = _linreg(cl2, cd)
            if reg and reg[0] > 0:
                summary["CD0_estimated"] = reg[1]
                summary["induced_drag_factor_k"] = reg[0]
                s_ref = refs.get("S_ref")
                b_ref = refs.get("b_ref")
                if s_ref and b_ref:
                    aspect_ratio = (b_ref * b_ref) / s_ref
                    summary["oswald_e_estimated"] = 1.0 / (math.pi * aspect_ratio * reg[0])

    # Longitudinal static stability from Cm(CL) regression
    cm_rows = [r for r in valid if r.get("Cm") is not None and math.isfinite(r["Cm"])]
    if len(cm_rows) >= 2 and len(set(alpha)) >= 2:
        reg = _linreg([r["CL"] for r in cm_rows], [r["Cm"] for r in cm_rows])
        if reg:
            dcm_dcl = reg[0]
            summary["dCm_dCL"] = dcm_dcl
            summary["SM_estimated"] = -dcm_dcl
            c_ref = refs.get("c_ref")
            xcg = refs.get("xcg")
            if c_ref is not None and xcg is not None:
                summary["X_np_estimated_m"] = xcg - c_ref * dcm_dcl
    return summary


def _beta_summary(rows):
    """Directional/lateral derivatives from a beta sweep (per rad)."""
    valid = [r for r in rows if r.get("beta_deg") is not None
             and all(r.get(k) is not None for k in ("CS", "Cn", "Cl", "CL"))]
    if len(valid) < 2:
        return {"n_points": len(valid)}
    beta = [r["beta_deg"] for r in valid]
    out = {"n_points": len(valid),
           "beta_min_deg": min(beta), "beta_max_deg": max(beta)}
    for source, target in (("CS", "CY_beta_per_rad"), ("Cn", "Cn_beta_per_rad"),
                           ("Cl", "Cl_beta_per_rad"), ("CL", "CL_beta_per_rad")):
        reg = _linreg(beta, [r[source] for r in valid])
        if reg:
            out[target] = reg[0] * 180.0 / math.pi
    return out


def parse_stab(path: Path):
    """Parse a VSPAERO ``.stab`` file into a list of per-alpha blocks."""
    text = _read_text(path)
    blocks = []
    for chunk in re.split(r"\*{10,}", text):
        if "Case" not in chunk and "Base_Aero" not in chunk:
            continue
        meta = {}
        base = None
        derivatives = {}
        for line in chunk.splitlines():
            tokens = line.split()
            if not tokens:
                continue
            first = tokens[0]
            try:
                if first.endswith("_") and len(tokens) >= 2 and first not in _STAB_COEFS:
                    meta[first.rstrip("_")] = float(tokens[1])
                elif first == "Base_Aero":
                    values = [float(t) for t in tokens[3:15]]
                    if len(values) == 12:
                        base = dict(zip(_STAB_COEFS, values))
                elif first in _STAB_COEFS and len(tokens) >= 9:
                    values = [float(t) for t in tokens[1:9]]
                    derivatives[first] = dict(zip(_STAB_DERIV_KEYS, values))
                elif first == "SM" and len(tokens) >= 2:
                    meta["SM"] = float(tokens[1])
                elif first == "X_np" and len(tokens) >= 2:
                    meta["X_np"] = float(tokens[1])
            except (ValueError, IndexError):
                continue
        if base is None and not derivatives:
            continue
        blocks.append({
            "alpha_deg": meta.get("AoA"),
            "beta_deg": meta.get("Beta"),
            "mach": meta.get("Mach"),
            "reference": {key: meta.get(key) for key in
                          ("Sref", "Cref", "Bref", "Xcg", "Ycg", "Zcg",
                           "Vinf", "Rho")},
            "base": base,
            "derivatives": derivatives,
            "SM": meta.get("SM"),
            "X_np": meta.get("X_np"),
        })
    return blocks


def _stability_payload(blocks, rows, refs, flow, mass_kg, inertia):
    """Condense parsed .stab blocks into a tool-friendly stability payload."""
    if not blocks:
        return {"available": False, "note": "VSPAERO 未生成 .stab（稳定导数）；"
                                           "已返回由极曲线回归估计的 SM/Xnp。"}
    first = blocks[0]
    derivatives = first.get("derivatives") or {}
    base = first.get("base") or {}

    def deriv(coef, key):
        entry = derivatives.get(coef) or {}
        value = entry.get(key)
        return value if isinstance(value, (int, float)) and math.isfinite(value) else None

    condensed = {
        "CL_alpha_per_rad": deriv("CL", "alpha"),
        "Cm_alpha_per_rad": deriv("CMm", "alpha"),
        "CY_beta_per_rad": deriv("CFy", "beta"),
        "Cn_beta_per_rad": deriv("CMn", "beta"),
        "Cl_beta_per_rad": deriv("CMl", "beta"),
        "CL_q_per_rad": deriv("CL", "q"),
        "Cm_q_per_rad": deriv("CMm", "q"),
        "CL_p_per_rad": deriv("CL", "p"),
        "Cl_p_per_rad": deriv("CMl", "p"),
        "Cn_r_per_rad": deriv("CMn", "r"),
        "CL_mach": deriv("CL", "mach"),
        "Cm_mach": deriv("CMm", "mach"),
        "CL_u": deriv("CL", "u"),
        "Cm_u": deriv("CMm", "u"),
    }
    for key in ("CL_alpha_per_rad", "Cm_alpha_per_rad"):
        if condensed.get(key) is not None:
            condensed[key] = condensed[key]

    block_rows = []
    for block in blocks:
        entry = {"alpha_deg": block.get("alpha_deg"),
                 "mach": block.get("mach"),
                 "SM": block.get("SM"),
                 "X_np": block.get("X_np")}
        if block.get("base"):
            entry["base"] = {key: block["base"].get(key) for key in
                             ("CL", "CD", "CMy", "CMx", "CMz", "CS")}
        block_rows.append(entry)

    modes = _estimate_modes(condensed, rows, refs, flow, mass_kg, inertia)
    payload = {
        "available": True,
        "source": ".stab",
        "notes": "导数单位：Alpha/Beta/p/q/r 均为 per rad；Mach/u 为 per 单位。"
                 "SM/Xnp 由 VSPAERO 按参考量计算，符号约定与 VSPAERO 7.2.2 一致。",
        "derivatives": condensed,
        "SM": first.get("SM"),
        "X_np_m": first.get("X_np"),
        "blocks": block_rows,
        "modes": modes,
    }
    return payload


def _estimate_modes(derivatives, rows, refs, flow, mass_kg, inertia):
    """Estimate phugoid (and short-period if mass/inertia are given) modes."""
    modes = {}
    vinf = flow.get("vinf")
    rho = flow.get("rho")
    g = 9.80665
    ld_rows = [r for r in rows if r.get("LD") is not None and math.isfinite(r["LD"])]
    ld0 = None
    if ld_rows:
        nearest = min(ld_rows, key=lambda r: abs((r.get("alpha_deg") or 0.0)))
        ld0 = nearest.get("LD")
        if ld0 is not None and ld0 <= 0:
            ld0 = max(ld_rows, key=lambda r: r["LD"])["LD"]
    if vinf and vinf > 0:
        entry = {
            "omega_n_rad_s": math.sqrt(2.0) * g / vinf,
            "method": "phugoid 近似: wn = sqrt(2)*g/V",
            "approximate": True,
        }
        if ld0 and ld0 > 0:
            entry["zeta_est"] = 1.0 / (math.sqrt(2.0) * ld0)
            entry["L_D_at_alpha0"] = ld0
        modes["phugoid"] = entry
    if mass_kg and isinstance(inertia, dict) and inertia.get("Iyy") and vinf and rho:
        cl_a = derivatives.get("CL_alpha_per_rad")
        cm_a = derivatives.get("Cm_alpha_per_rad")
        cl_q = derivatives.get("CL_q_per_rad")
        cm_q = derivatives.get("Cm_q_per_rad")
        if cl_a and cm_a:
            s_ref = refs.get("S_ref") or 1.0
            c_ref = refs.get("c_ref") or 1.0
            qbar = 0.5 * rho * vinf * vinf
            iyy = float(inertia["Iyy"])
            za = -qbar * s_ref * cl_a / mass_kg
            zq = (-qbar * s_ref * c_ref * (cl_q or 0.0)) / (2.0 * mass_kg * vinf)
            ma = qbar * s_ref * c_ref * cm_a / iyy
            mq = (qbar * s_ref * c_ref * c_ref * (cm_q or 0.0)) / (2.0 * iyy * vinf)
            a11 = za / vinf
            a12 = 1.0 + zq / vinf
            a21 = ma
            a22 = mq
            trace = a11 + a22
            det = a11 * a22 - a12 * a21
            if det > 0:
                entry = {
                    "omega_n_rad_s": math.sqrt(det),
                    "zeta": -trace / (2.0 * math.sqrt(det)),
                    "method": "短周期 2-DoF 近似（线性化 A 矩阵特征值）",
                    "approximate": True,
                    "assumptions": {"mass_kg": mass_kg,
                                    "Iyy": iyy,
                                    "CL_alpha_per_rad": cl_a,
                                    "Cm_alpha_per_rad": cm_a,
                                    "CL_q_per_rad": cl_q,
                                    "Cm_q_per_rad": cm_q},
                }
                modes["short_period"] = entry
            else:
                modes["short_period"] = {"available": False,
                                         "note": "特征值判别式不满足 det>0，无法估计短周期模态"}
    if not modes:
        modes["note"] = "未提供 mass_kg/inertia.Iyy 且缺少 L/D，无法估计模态"
    return modes


# ---------------------------------------------------------------------------
# Word report generation (python-docx optional, built-in minimal docx fallback)
# ---------------------------------------------------------------------------


def _xml_escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _write_minimal_docx(path: Path, blocks, title: str):
    """Write a valid .docx with paragraphs only (no third-party dependency)."""
    import zipfile

    def para(text: str, style: str = None, bold: bool = False) -> str:
        props = []
        if style:
            props.append('<w:pStyle w:val="%s"/>' % style)
        run_props = "<w:b/>" if bold else ""
        return ('<w:p><w:pPr>%s</w:pPr><w:r><w:rPr>%s</w:rPr>'
                '<w:t xml:space="preserve">%s</w:t></w:r></w:p>'
                % ("".join(props), run_props, _xml_escape(text)))

    body = [para(title, style="Heading1")]
    for block in blocks:
        kind = block[0]
        if kind == "h1":
            body.append(para(block[1], style="Heading1"))
        elif kind == "h2":
            body.append(para(block[1], style="Heading2"))
        else:
            body.append(para(block[1]))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>%s<w:sectPr/>' % "".join(body)
        + '</w:body></w:document>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/><w:rPr><w:sz w:val="22"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1">'
        '<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
        '<w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading2">'
        '<w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>'
        '<w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>'
        '</w:styles>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )
    with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", styles)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
    return "builtin-docx"


def _write_report_docx(path: Path, report: dict, title: str):
    """Write an aerodynamic report; python-docx when available."""
    try:
        from docx import Document  # optional dependency
    except Exception:  # noqa: BLE001
        blocks = []
        for item in _report_paragraphs(report):
            blocks.append(item)
        return _write_minimal_docx(path, blocks, title)

    doc = Document()
    doc.add_heading(title, level=0)
    doc.add_paragraph("生成时间: %s  |  服务器: %s %s" % (
        report.get("generated_at", ""), SERVER_NAME, SERVER_VERSION))
    for block in _report_blocks(report):
        kind = block[0]
        if kind == "h1":
            doc.add_heading(block[1], level=1)
        elif kind == "h2":
            doc.add_heading(block[1], level=2)
        elif kind == "p":
            doc.add_paragraph(str(block[1]))
        elif kind == "table":
            headers, rows = block[1], block[2]
            table = doc.add_table(rows=1, cols=len(headers))
            try:
                table.style = "Table Grid"
            except Exception:  # noqa: BLE001
                pass
            for idx, header in enumerate(headers):
                cell = table.rows[0].cells[idx]
                cell.text = str(header)
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.bold = True
            for row in rows:
                cells = table.add_row().cells
                for idx, value in enumerate(row[:len(headers)]):
                    cells[idx].text = "" if value is None else str(value)
    doc.save(str(path))
    return "python-docx"


def _report_blocks(report: dict):
    """Structured report content: list of (kind, ...) tuples."""
    blocks = []
    spec = report.get("spec") or {}
    refs = report.get("reference") or {}
    analysis = report.get("analysis") or {}
    metrics = report.get("metrics") or {}
    geometry = report.get("geometry") or []
    summary = analysis.get("summary") or {}
    stability = analysis.get("stability") or {}
    rows = analysis.get("rows") or []
    flow = analysis.get("flow") or {}

    blocks.append(("h1", "1. 模型与设计参数"))
    if spec:
        spec_rows = []
        for key in ("name", "S_ref", "AR", "taper", "sweep", "dihedral", "cant",
                    "v_tail", "Sh", "Vv", "engine", "fuselage_len"):
            if key in spec and spec[key] is not None:
                spec_rows.append([key, spec[key]])
        derived = spec.get("derived") or {}
        for key in ("span_m", "root_chord_m", "tip_chord_m", "fuselage_len_m",
                    "x_wing_m", "tail_x_m", "htail_area_m2", "vtail_area_m2"):
            if key in derived and derived[key] is not None:
                spec_rows.append([key, round(derived[key], 4)])
        blocks.append(("table", ["参数", "数值"], spec_rows))
    else:
        blocks.append(("p", "（当前会话没有 build_aircraft 的设计参数记录。）"))

    blocks.append(("h1", "2. 几何组件"))
    if geometry:
        geom_rows = []
        for item in geometry:
            params = item.get("params") or {}
            keys = [k for k in ("span_m", "area_m2", "MAC_m", "root_chord_m",
                                "tip_chord_m", "length_m", "diameter_m")
                    if k in params]
            detail = ", ".join("%s=%s" % (k, round(params[k], 4) if isinstance(params[k], float) else params[k])
                               for k in keys)
            geom_rows.append([item.get("name"), item.get("type"), detail])
            blocks.append(("table", ["名称", "类型", "关键参数"], geom_rows))
    else:
        blocks.append(("p", "（没有可用的几何组件信息。）"))

    blocks.append(("h1", "3. 参考量与计算状态"))
    ref_rows = [[k, refs.get(k)] for k in
                ("S_ref", "b_ref", "c_ref", "xcg", "ycg", "zcg", "re_ref")
                if refs.get(k) is not None]
    blocks.append(("table", ["参考量", "数值"], ref_rows))
    flow_rows = [[k, flow.get(k)] for k in ("case", "vinf", "rho", "mach", "alpha_deg", "beta_deg", "solver")
                 if flow.get(k) is not None]
    if flow_rows:
        blocks.append(("table", ["来流/求解", "数值"], flow_rows))

    blocks.append(("h1", "4. VSPAERO 极曲线"))
    if rows:
        polar_rows = []
        for row in rows[:200]:
            polar_rows.append([
                row.get("alpha_deg"), row.get("beta_deg"), row.get("mach"),
                None if row.get("CL") is None else round(row["CL"], 4),
                None if row.get("CD") is None else round(row["CD"], 5),
                None if row.get("LD") is None else round(row["LD"], 2),
                None if row.get("Cm") is None else round(row["Cm"], 4),
                None if row.get("CS") is None else round(row["CS"], 4),
            ])
        blocks.append(("table", ["Alpha(deg)", "Beta(deg)", "Mach", "CL", "CD", "L/D", "Cm", "CS"], polar_rows))
    else:
        blocks.append(("p", "（没有 VSPAERO 极曲线数据；请先运行 openvsp_run_vspaero。）"))

    blocks.append(("h1", "5. 气动指标"))
    metric_rows = []
    for key, value in summary.items():
        metric_rows.append([key, value])
    for key, value in metrics.items():
        metric_rows.append([key, value])
    if metric_rows:
        blocks.append(("table", ["指标", "数值"], metric_rows))
    else:
        blocks.append(("p", "（没有可用的气动指标。）"))

    blocks.append(("h1", "6. 稳定性导数"))
    if stability.get("available"):
        deriv_rows = [[k, v] for k, v in (stability.get("derivatives") or {}).items()
                      if v is not None]
        if deriv_rows:
            blocks.append(("table", ["导数", "数值"], deriv_rows))
        blocks.append(("p", "SM = %s, X_np = %s m" % (stability.get("SM"), stability.get("X_np_m"))))
        modes = stability.get("modes") or {}
        for name, entry in modes.items():
            if isinstance(entry, dict):
                blocks.append(("p", "模态 %s: %s" % (name, json.dumps(entry, ensure_ascii=False))))
    else:
        blocks.append(("p", stability.get("note") or "（未运行 stability 工况。）"))
        if summary.get("SM_estimated") is not None:
            blocks.append(("p", "由极曲线回归估计: SM=%s, Xnp=%s m"
                           % (summary.get("SM_estimated"), summary.get("X_np_estimated_m"))))

    blocks.append(("h1", "7. 说明与限制"))
    notes = report.get("notes") or []
    for note in notes:
        blocks.append(("p", "- %s" % note))
    if not notes:
        blocks.append(("p", "- 计算结果基于 VSPAERO VLM/尾迹迭代，适用低马赫不可压假设。"))
    return blocks


def _report_paragraphs(report: dict):
    """Flatten structured report blocks to (kind, text) for the minimal writer."""
    out = []
    for block in _report_blocks(report):
        kind = block[0]
        if kind in ("h1", "h2"):
            out.append((kind, str(block[1])))
        elif kind == "p":
            out.append(("p", str(block[1])))
        elif kind == "table":
            headers, rows = block[1], block[2]
            out.append(("p", " | ".join(str(h) for h in headers)))
            for row in rows:
                out.append(("p", " | ".join("" if v is None else str(v) for v in row)))
    return out


# ---------------------------------------------------------------------------
# Engine: all tool implementations (in-process OpenVSP API)
# ---------------------------------------------------------------------------

WORKER_OPS = ("openvsp_run_vspaero",)

_KNOWN_SPEC_KEYS = {
    "name", "S_ref", "AR", "taper", "sweep", "dihedral", "cant", "v_tail",
    "Sh", "Vv", "engine", "fuselage_len", "include_fuselage",
    "fuselage_fineness", "h_tail_AR", "v_tail_AR", "tail_taper", "tail_sweep",
    "x_wing", "tail_x",
}

_VSPAERO_OUTPUT_EXTS = (
    ".polar", ".stab", ".history", ".lod", ".vspgeom", ".vspaero", ".vkey",
    ".adb", ".flt", ".csf", ".cuts", ".slc", ".group.0", ".group.1",
    ".quad.cases", ".adb.cases", ".pstab", ".qstab", ".rstab", ".forces",
)


class Engine:
    """Executes the MCP tools.  One instance per MCP session (or worker run)."""

    TOOL_METHODS = {
        "openvsp_status": "_tool_status",
        "openvsp_new_model": "_tool_new_model",
        "openvsp_build_aircraft": "_tool_build_aircraft",
        "openvsp_get_geometry": "_tool_get_geometry",
        "openvsp_run_vspaero": "_tool_run_vspaero",
        "openvsp_export": "_tool_export",
        "openvsp_generate_report": "_tool_generate_report",
        "openvsp_list_files": "_tool_list_files",
    }

    def __init__(self, cfg: Config, state: dict = None):
        self.cfg = cfg
        self.state = dict(state or {})
        self._vsp = None
        self._vsp_error = None
        self._worker_mode = cfg.force_subprocess
        self._package_dir = None

    # ------------------------------------------------------------------
    # Routing (in-process vs worker subprocess)
    # ------------------------------------------------------------------
    def call(self, name, args):
        if name not in self.TOOL_METHODS:
            raise ToolError(ERR_INVALID_PARAMS, "未知工具: %s" % name)
        if self._worker_mode:
            return _json_safe(self._call_via_worker(name, args))
        if name in ("openvsp_status", "openvsp_list_files"):
            return _json_safe(self._call_local(name, args))
        if (name in WORKER_OPS and self.cfg.analysis_subprocess
                and self._worker_available()):
            try:
                return _json_safe(self._call_via_worker(name, args))
            except ToolError as exc:
                if exc.code in (ERR_WORKER_FAILED, ERR_WORKER_TIMEOUT):
                    raise
                raise
        try:
            return _json_safe(self._call_local(name, args))
        except ToolError as exc:
            if exc.code != ERR_VSP_UNAVAILABLE or not self._worker_available():
                raise
            _log(_WARN, "进程内 OpenVSP 不可用，改用 worker 子进程 (%s)", self.cfg.python)
            try:
                result = self._call_via_worker(name, args)
            except ToolError as worker_exc:
                raise ToolError(ERR_VSP_UNAVAILABLE,
                                "OpenVSP Python API 不可用。进程内: %s；worker: %s"
                                % (exc.message, worker_exc.message))
            self._worker_mode = True
            return _json_safe(result)

    def _worker_available(self) -> bool:
        if getattr(sys, "frozen", False):
            return False
        try:
            return Path(self.cfg.python).is_file()
        except OSError:
            return False

    def _call_local(self, name, args):
        method_name = self.TOOL_METHODS.get(name)
        if method_name is None:
            raise ToolError(ERR_INVALID_PARAMS, "未知工具: %s" % name)
        return getattr(self, method_name)(args or {})

    def _call_via_worker(self, name, args):
        if not self._worker_available():
            raise ToolError(ERR_WORKER_FAILED,
                            "OPENVSP_PYTHON 解释器不存在: %r" % self.cfg.python)
        workdir = self.cfg.ensure_workdir()
        tmp_dir = workdir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        stamp = "%d_%d" % (os.getpid(), time.time_ns())
        req_path = tmp_dir / ("req_%s.json" % stamp)
        resp_path = tmp_dir / ("resp_%s.json" % stamp)
        payload = {"op": name, "args": args or {}, "state": self.state}
        req_path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False),
                            encoding="utf-8")
        env = os.environ.copy()
        env.update({
            "OPENVSP_ROOT": self.cfg.root,
            "OPENVSP_PYTHON": self.cfg.python,
            "OPENVSP_MCP_WORKDIR": self.cfg.workdir,
            "OPENVSP_MCP_AI_DIR": self.cfg.ai_dir,
            "OPENVSP_MCP_AERO_SET": str(self.cfg.aero_set),
            "OPENVSP_MCP_FORCE_SUBPROCESS": "0",
            "OPENVSP_MCP_ANALYSIS_SUBPROCESS": "0",
            "OPENVSP_MCP_LOG_LEVEL": self.cfg.log_level,
            "PYTHONIOENCODING": "utf-8",
        })
        if self.cfg.pythonpath:
            env["OPENVSP_PYTHONPATH"] = os.pathsep.join(self.cfg.pythonpath)
        cmd = [self.cfg.python, str(Path(__file__).resolve()),
               "--worker", str(req_path), str(resp_path)]
        _log(_DEBUG, "worker -> %s", name)
        try:
            proc = subprocess.run(cmd, cwd=str(tmp_dir), env=env,
                                  capture_output=True, timeout=self.cfg.worker_timeout)
        except subprocess.TimeoutExpired:
            self._cleanup(req_path, resp_path)
            raise ToolError(ERR_WORKER_TIMEOUT,
                            "OpenVSP worker 执行超时（%.0f 秒，工具 %s）；"
                            "可增大 OPENVSP_MCP_WORKER_TIMEOUT 或减小网格/计算量。"
                            % (self.cfg.worker_timeout, name))
        except OSError as exc:
            self._cleanup(req_path, resp_path)
            raise ToolError(ERR_WORKER_FAILED, "无法启动 worker: %s" % exc)
        try:
            if not resp_path.is_file():
                tail = _tail(proc.stderr) or _tail(proc.stdout)
                raise ToolError(ERR_WORKER_FAILED,
                                "worker 未返回结果 (exit=%s)。stderr: %s"
                                % (proc.returncode, tail))
            try:
                data = json.loads(resp_path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise ToolError(ERR_WORKER_FAILED, "worker 响应解析失败: %s" % exc)
        finally:
            self._cleanup(req_path, resp_path)
        if isinstance(data.get("state"), dict):
            self.state.update(data["state"])
        if not data.get("ok"):
            error = data.get("error") or {}
            raise ToolError(error.get("code") or ERR_WORKER_FAILED,
                            error.get("message") or "worker 执行失败",
                            data=error.get("data"))
        return data.get("result")

    @staticmethod
    def _cleanup(*paths):
        for path in paths:
            try:
                Path(path).unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # OpenVSP import
    # ------------------------------------------------------------------
    def _ensure_vsp(self):
        if self._vsp is not None:
            return self._vsp
        if self._vsp_error is not None:
            raise self._vsp_error
        cfg = self.cfg
        root = Path(cfg.root)
        if not root.is_dir():
            error = ToolError(
                ERR_VSP_UNAVAILABLE,
                "OpenVSP 安装目录不存在: %s。请设置环境变量 OPENVSP_ROOT "
                "指向 OpenVSP 3.x 安装根目录。" % cfg.root)
            self._vsp_error = error
            raise error
        for path in cfg.python_api_paths():
            if path and path not in sys.path:
                sys.path.append(path)
        try:
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(cfg.root)
        except OSError as exc:
            _log(_WARN, "add_dll_directory(%s) 失败: %s", cfg.root, exc)
        try:
            import openvsp as vsp  # noqa: F401 - imported lazily
        except Exception as exc:  # noqa: BLE001
            error = ToolError(
                ERR_VSP_UNAVAILABLE,
                "无法 import openvsp Python API: %s。请确认 OPENVSP_ROOT、"
                "OPENVSP_PYTHONPATH 正确，且使用含 numpy 的 Python "
                "（可用 OPENVSP_PYTHON 指定）。" % exc)
            self._vsp_error = error
            raise error
        self._vsp = vsp
        try:
            self._package_dir = str(Path(vsp.__file__).resolve().parent)
        except Exception:  # noqa: BLE001
            self._package_dir = cfg.root
        _log(_INFO, "OpenVSP Python API 已加载: %s (%s)", vsp.GetVSPVersion(), self._package_dir)
        return vsp

    def _ensure_model(self):
        vsp = self._ensure_vsp()
        try:
            geoms = vsp.FindGeoms()
        except Exception as exc:  # noqa: BLE001
            geoms = None
            _log(_WARN, "FindGeoms 失败: %s", exc)
        if geoms:
            return vsp
        path = self.state.get("vsp3_path")
        if path and Path(path).is_file():
            try:
                vsp.ClearVSPModel()
                vsp.ReadVSPFile(str(path))
                vsp.Update()
            except Exception as exc:  # noqa: BLE001
                raise ToolError(ERR_IO, "读取模型文件失败 %s: %s" % (path, exc))
            if vsp.FindGeoms():
                return vsp
        raise ToolError(ERR_NO_MODEL,
                        "当前没有模型。请先调用 openvsp_new_model / "
                        "openvsp_build_aircraft，或向 openvsp_get_geometry 传入 .vsp3 路径。")

    # ------------------------------------------------------------------
    # Small OpenVSP helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _set_parm(vsp, geom_id, name, group, value) -> bool:
        try:
            parm_id = vsp.FindParm(geom_id, name, group)
        except Exception:  # noqa: BLE001
            return False
        if not parm_id:
            return False
        try:
            vsp.SetParmVal(parm_id, float(value))
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _parm(vsp, geom_id, name, group):
        try:
            parm_id = vsp.FindParm(geom_id, name, group)
            if parm_id:
                return float(vsp.GetParmVal(parm_id))
        except Exception:  # noqa: BLE001
            return None
        return None

    @staticmethod
    def _xsec_parm(vsp, geom_id, name):
        """Read a parameter that lives on an XSecCurve container (e.g. ThickChord)."""
        try:
            xsec_surf = vsp.GetXSecSurf(geom_id, 0)
            count = vsp.GetNumXSec(xsec_surf) if xsec_surf else 0
            if not count:
                return None
            index = 1 if count > 1 else 0
            xsec = vsp.GetXSec(xsec_surf, index)
            parm_id = vsp.GetXSecParm(xsec, name)
            if parm_id:
                return float(vsp.GetParmVal(parm_id))
        except Exception:  # noqa: BLE001
            return None
        return None

    @staticmethod
    def _bbox(vsp, geom_id):
        try:
            low = vsp.GetGeomBBoxMin(geom_id)
            high = vsp.GetGeomBBoxMax(geom_id)

            def coordinate(vec, axis):
                attr = getattr(vec, axis, None)
                if attr is None:
                    return 0.0
                return float(attr() if callable(attr) else attr)

            return {
                "min": [coordinate(low, "x"), coordinate(low, "y"), coordinate(low, "z")],
                "max": [coordinate(high, "x"), coordinate(high, "y"), coordinate(high, "z")],
            }
        except Exception:  # noqa: BLE001
            return None

    def _geom_brief(self, vsp, geom_id):
        try:
            name = vsp.GetGeomName(geom_id)
            gtype = vsp.GetGeomTypeName(geom_id)
        except Exception:  # noqa: BLE001
            name, gtype = "", ""
        return {"id": geom_id, "name": name, "type": gtype,
                "bbox": self._bbox(vsp, geom_id)}

    def _geom_details(self, vsp, geom_id, include_parms: bool = True):
        info = self._geom_brief(vsp, geom_id)
        gtype = info["type"]
        geom_name = str(info.get("name") or "")
        if geom_name in ("Wing", "HTail", "VTail", "Fuselage", "Propeller"):
            info["role"] = {"Wing": "main_wing", "HTail": "horizontal_tail",
                            "VTail": "vertical_tail", "Fuselage": "fuselage",
                            "Propeller": "propeller"}[geom_name]
        params = {}
        if gtype == "Wing":
            for key, (name, group) in {
                "span_m": ("TotalSpan", "WingGeom"),
                "projected_span_m": ("TotalProjectedSpan", "WingGeom"),
                "area_m2": ("TotalArea", "WingGeom"),
                "MAC_m": ("MAC", "WingGeom"),
                "aspect_ratio": ("TotalAR", "WingGeom"),
            }.items():
                value = self._parm(vsp, geom_id, name, group)
                if value is not None:
                    params[key] = value
            for key, (name, group) in {
                "root_chord_m": ("Root_Chord", "XSec_1"),
                "tip_chord_m": ("Tip_Chord", "XSec_1"),
                "half_span_m": ("Span", "XSec_1"),
                "sweep_deg": ("Sweep", "XSec_1"),
                "dihedral_deg": ("Dihedral", "XSec_1"),
                "twist_deg": ("Twist", "XSec_1"),
            }.items():
                value = self._parm(vsp, geom_id, name, group)
                if value is not None:
                    params[key] = value
            thickness = self._xsec_parm(vsp, geom_id, "ThickChord")
            if thickness is not None:
                params["thickness_ratio"] = thickness
        elif gtype in ("Pod", "Fuselage", "Body"):
            length = self._parm(vsp, geom_id, "Length", "Design")
            if length is not None:
                params["length_m"] = length
            fineness = self._parm(vsp, geom_id, "FineRatio", "Design")
            if fineness is not None:
                params["fineness_ratio"] = fineness
        elif gtype == "Propeller":
            diameter = self._parm(vsp, geom_id, "Diameter", "Design")
            if diameter is not None:
                params["diameter_m"] = diameter
            blades = self._parm(vsp, geom_id, "NumBlade", "Design")
            if blades is not None:
                params["num_blades"] = blades
        if include_parms and not params:
            for parm_id in vsp.GetGeomParmIDs(geom_id):
                try:
                    if vsp.GetParmGroupName(parm_id) == "Design":
                        params[vsp.GetParmName(parm_id)] = round(float(vsp.GetParmVal(parm_id)), 6)
                        if len(params) >= 10:
                            break
                except Exception:  # noqa: BLE001
                    continue
        location = {}
        for key in ("X_Rel_Location", "Y_Rel_Location", "Z_Rel_Location"):
            value = self._parm(vsp, geom_id, key, "XForm")
            if value is not None:
                location[key[0].lower()] = value
        if location:
            params["location_m"] = location
        info["params"] = params
        return info

    # ------------------------------------------------------------------
    # Tool: openvsp_status
    # ------------------------------------------------------------------
    def _tool_status(self, args):
        info = {
            "ok": True,
            "server": {
                "name": SERVER_NAME,
                "title": SERVER_TITLE,
                "version": SERVER_VERSION,
                "pid": os.getpid(),
            },
            "protocol": {
                "era": "legacy",
                "protocolVersion": PROTOCOL_VERSION,
                "supportedProtocolVersions": list(SUPPORTED_PROTOCOLS),
                "modernEraSupported": False,
                "modernProtocolVersion": MODERN_PROTOCOL,
                "transports": ["stdio"],
            },
            "python": {
                "executable": sys.executable,
                "version": platform.python_version(),
                "implementation": platform.python_implementation(),
            },
            "config": {
                "openvsp_root": self.cfg.root,
                "openvsp_python": self.cfg.python,
                "openvsp_pythonpath": self.cfg.pythonpath,
                "workspace": self.cfg.workdir,
                "ai_dir": self.cfg.ai_dir,
                "aero_set": self.cfg.aero_set,
                "analysis_subprocess": self.cfg.analysis_subprocess,
                "force_subprocess": self.cfg.force_subprocess,
            },
            "openvsp": {
                "available": False,
                "root_exists": Path(self.cfg.root).is_dir(),
                "vsp_exe": str(Path(self.cfg.root) / "vsp.exe") if Path(self.cfg.root, "vsp.exe").is_file() else None,
                "vspaero_exe": str(Path(self.cfg.root) / "vspaero.exe") if Path(self.cfg.root, "vspaero.exe").is_file() else None,
                "vspaero_available": False,
            },
            "ai_design": {"available": False, "path": self.cfg.ai_dir},
            "model": {
                "name": self.state.get("model_name"),
                "vsp3_path": self.state.get("vsp3_path"),
                "has_spec": bool(self.state.get("spec")),
                "last_analysis": None,
            },
        }
        try:
            vsp = self._ensure_vsp()
            info["openvsp"].update({
                "available": True,
                "version": vsp.GetVSPVersion(),
                "python_api_dir": self._package_dir,
            })
            try:
                vspaero_path = vsp.GetVSPAEROPath()
                info["openvsp"]["vspaero_path"] = vspaero_path
                info["openvsp"]["vspaero_available"] = bool(
                    vspaero_path and Path(vspaero_path, "vspaero.exe").is_file())
            except Exception as exc:  # noqa: BLE001
                info["openvsp"]["vspaero_error"] = str(exc)
        except ToolError as exc:
            info["openvsp"]["error"] = exc.message
            info["openvsp"]["hint"] = (
                "检查 OPENVSP_ROOT / OPENVSP_PYTHONPATH；MCP 客户端配置里也可显式设置 "
                "OPENVSP_PYTHON 指向已安装 numpy 且可 import openvsp 的 python.exe。")
        ai = _load_ai_design(self.cfg)
        if ai is not None:
            info["ai_design"]["available"] = True
            info["ai_design"]["version"] = getattr(ai, "__version__", None)
            info["ai_design"]["modules"] = _ai_design_modules()
        last = self.state.get("last_analysis")
        if isinstance(last, dict):
            info["model"]["last_analysis"] = {
                "case": last.get("case"),
                "timestamp": last.get("timestamp"),
                "summary": last.get("summary"),
                "files": last.get("files"),
            }
        return info

    # ------------------------------------------------------------------
    # Tool: openvsp_new_model
    # ------------------------------------------------------------------
    def _tool_new_model(self, args):
        name = str(args.get("name") or "untitled").strip() or "untitled"
        vsp = self._ensure_vsp()
        vsp.ClearVSPModel()
        vsp.Update()
        self.state = {
            "model_name": name,
            "vsp3_path": None,
            "spec": None,
            "refs": None,
            "aero_geoms": [],
            "thick_geoms": [],
            "last_analysis": None,
            "created_at": _now_iso(),
        }
        return {"ok": True, "model_name": name, "cleared": True, "geometries": []}

    # ------------------------------------------------------------------
    # Tool: openvsp_build_aircraft
    # ------------------------------------------------------------------
    def _normalise_spec(self, spec: dict):
        warnings = []
        if not isinstance(spec, dict):
            raise ToolError(ERR_INVALID_PARAMS, "spec 必须是对象")
        for key in spec:
            if key not in _KNOWN_SPEC_KEYS:
                warnings.append("未知 spec 字段已忽略: %s" % key)

        def number(key, default=None, lo=None, hi=None):
            return _as_float(spec.get(key), "spec.%s" % key, default=default,
                             lo=lo, hi=hi)

        name = str(spec.get("name") or "UAV").strip() or "UAV"
        s_ref = number("S_ref", 1.2, 1e-4, 1e5)
        aspect = number("AR", 8.0, 1.0, 60.0)
        taper = number("taper", 0.7, 0.05, 1.0)
        sweep = number("sweep", 2.0, -60.0, 60.0)
        dihedral = number("dihedral", 3.0, -60.0, 60.0)
        cant = number("cant", 0.0, -60.0, 60.0)
        v_tail = bool(spec.get("v_tail", True))
        sh = number("Sh", None, 0.0, 1e5)
        vv = number("Vv", 0.05, 0.0, 1.0)
        engine = str(spec.get("engine") or "none").strip().lower()
        fuselage_len = number("fuselage_len", None, 0.05, 1e4)
        include_fuselage = bool(spec.get("include_fuselage", True))
        fineness = number("fuselage_fineness", 7.0, 2.0, 40.0)
        h_tail_ar = number("h_tail_AR", 4.0, 1.0, 20.0)
        v_tail_ar = number("v_tail_AR", 2.0, 0.5, 10.0)
        tail_taper = number("tail_taper", 0.6, 0.05, 1.0)
        tail_sweep = number("tail_sweep", 5.0, -40.0, 60.0)
        x_wing = number("x_wing", None, -1e4, 1e4)
        tail_x = number("tail_x", None, -1e4, 1e4)

        span = math.sqrt(aspect * s_ref)
        if fuselage_len is None:
            fuselage_len = 0.45 * span
        if x_wing is None:
            x_wing = 0.28 * fuselage_len
        if tail_x is None:
            tail_x = 0.72 * fuselage_len
        if sh is None:
            sh = 0.18 * s_ref
        if include_fuselage and fuselage_len < 0.25 * span:
            warnings.append("fuselage_len 相对翼展偏短（<0.25*span），模型可能看起来不协调")
        if not include_fuselage and sh <= 0:
            warnings.append("未生成机身且平尾面积为 0")
        if tail_x <= x_wing:
            fixed = x_wing + 0.18 * fuselage_len
            warnings.append("tail_x 必须在机翼之后，已自动调整为 %.3f m" % fixed)
            tail_x = fixed
        if cant:
            warnings.append("cant 以整翼绕 X 轴刚性滚转近似实现（OpenVSP WING 无独立 Cant 参数）")
        return {
            "name": name,
            "S_ref": s_ref,
            "AR": aspect,
            "taper": taper,
            "sweep": sweep,
            "dihedral": dihedral,
            "cant": cant,
            "v_tail": v_tail,
            "Sh": sh,
            "Vv": vv,
            "engine": engine,
            "fuselage_len": fuselage_len,
            "include_fuselage": include_fuselage,
            "fuselage_fineness": fineness,
            "h_tail_AR": h_tail_ar,
            "v_tail_AR": v_tail_ar,
            "tail_taper": tail_taper,
            "tail_sweep": tail_sweep,
            "x_wing": x_wing,
            "tail_x": tail_x,
            "_warnings": warnings,
        }

    def _add_wing(self, vsp, name, span, area, taper, sweep=0.0, dihedral=0.0,
                  x=0.0, z=0.0, x_rotation=0.0, symmetric=True):
        geom_id = vsp.AddGeom("WING", "")
        vsp.SetGeomName(geom_id, name)
        root = 2.0 * area / (span * (1.0 + taper))
        tip = root * taper
        ok = True
        ok &= self._set_parm(vsp, geom_id, "Span", "XSec_1",
                             span / 2.0 if symmetric else span)
        ok &= self._set_parm(vsp, geom_id, "Root_Chord", "XSec_1", root)
        ok &= self._set_parm(vsp, geom_id, "Tip_Chord", "XSec_1", tip)
        if not ok:
            raise ToolError(ERR_INTERNAL, "创建机翼 %s 失败（核心尺寸参数缺失）" % name)
        self._set_parm(vsp, geom_id, "Sweep", "XSec_1", sweep)
        self._set_parm(vsp, geom_id, "Dihedral", "XSec_1", dihedral)
        self._set_parm(vsp, geom_id, "X_Rel_Location", "XForm", x)
        self._set_parm(vsp, geom_id, "Z_Rel_Location", "XForm", z)
        if x_rotation:
            self._set_parm(vsp, geom_id, "X_Rel_Rotation", "XForm", x_rotation)
        if not symmetric:
            self._set_parm(vsp, geom_id, "Sym_Planar_Flag", "Sym", 0.0)
        vsp.Update()
        return geom_id, root, tip

    def _add_fuselage(self, vsp, length, fineness, x=0.0):
        geom_id = vsp.AddGeom("POD", "")
        vsp.SetGeomName(geom_id, "Fuselage")
        if not self._set_parm(vsp, geom_id, "Length", "Design", length):
            raise ToolError(ERR_INTERNAL, "创建机身失败（POD 的 Length 参数缺失）")
        self._set_parm(vsp, geom_id, "FineRatio", "Design", fineness)
        self._set_parm(vsp, geom_id, "X_Rel_Location", "XForm", x)
        vsp.Update()
        return geom_id

    def _add_propeller(self, vsp, diameter, blades, x, z=0.0):
        geom_id = vsp.AddGeom("PROP", "")
        vsp.SetGeomName(geom_id, "Propeller")
        self._set_parm(vsp, geom_id, "Diameter", "Design", diameter)
        self._set_parm(vsp, geom_id, "NumBlade", "Design", blades)
        self._set_parm(vsp, geom_id, "X_Rel_Location", "XForm", x)
        self._set_parm(vsp, geom_id, "Z_Rel_Location", "XForm", z)
        self._set_parm(vsp, geom_id, "Sym_Planar_Flag", "Sym", 0.0)
        vsp.Update()
        return geom_id

    def _tool_build_aircraft(self, args):
        vsp = self._ensure_vsp()
        raw_spec = args.get("spec")
        if raw_spec is None:
            raw_spec = {}
        spec = self._normalise_spec(raw_spec)
        warnings = spec.pop("_warnings")

        span = math.sqrt(spec["AR"] * spec["S_ref"])
        taper = spec["taper"]
        fuselage_len = spec["fuselage_len"]
        x_wing = spec["x_wing"]
        tail_x = spec["tail_x"]
        z_wing = 0.06 * fuselage_len
        z_tail = 0.03 * fuselage_len

        vsp.ClearVSPModel()
        vsp.Update()

        wing, root_chord, tip_chord = self._add_wing(
            vsp, "Wing", span, spec["S_ref"], taper,
            sweep=spec["sweep"], dihedral=spec["dihedral"],
            x=x_wing, z=z_wing, x_rotation=spec["cant"], symmetric=True)

        htail = None
        if spec["Sh"] and spec["Sh"] > 0:
            h_span = math.sqrt(spec["h_tail_AR"] * spec["Sh"])
            htail, _, _ = self._add_wing(
                vsp, "HTail", h_span, spec["Sh"], spec["tail_taper"],
                sweep=spec["tail_sweep"], x=tail_x, z=z_tail, symmetric=True)
        else:
            warnings.append("未生成平尾（Sh=0）")

        vtail = None
        sv = 0.0
        l_tail = max(tail_x - x_wing, 0.05 * fuselage_len)
        if spec["v_tail"] and spec["Vv"] > 0:
            sv = spec["Vv"] * spec["S_ref"] * span / l_tail
            v_span = math.sqrt(spec["v_tail_AR"] * sv)
            vtail, _, _ = self._add_wing(
                vsp, "VTail", v_span, sv, spec["tail_taper"],
                sweep=spec["tail_sweep"], x=tail_x, z=0.0,
                x_rotation=90.0, symmetric=False)
        else:
            warnings.append("未生成垂尾（v_tail=false 或 Vv=0）")

        fuselage = None
        if spec["include_fuselage"]:
            fuselage = self._add_fuselage(vsp, fuselage_len,
                                          spec["fuselage_fineness"])

        propeller = None
        engine = spec["engine"]
        if engine in ("tractor", "pusher", "propeller"):
            diameter = max(0.08 * span, 0.25 * fuselage_len)
            if engine == "pusher":
                prop_x = fuselage_len + 0.03 * fuselage_len
            else:
                prop_x = -0.03 * fuselage_len
            propeller = self._add_propeller(vsp, diameter, 3, prop_x,
                                            z=z_wing if engine == "tractor" else 0.0)
        elif engine not in ("none", "", "electric", "piston", "turboprop", "jet", "turbofan"):
            warnings.append("未识别的 engine=%r，仅作为元数据记录（未生成发动机几何）" % engine)

        vsp.Update()

        # Tag geometry sets: aero surfaces in the thin set, bodies in thick set.
        aero_ids = [geom for geom in (wing, htail, vtail) if geom]
        thick_ids = [geom for geom in (fuselage, propeller) if geom]
        for geom_id in aero_ids:
            vsp.SetSetFlag(geom_id, self.cfg.aero_set, True)
        thick_set = self.cfg.aero_set + 1
        for geom_id in thick_ids:
            vsp.SetSetFlag(geom_id, thick_set, True)
        vsp.Update()

        # Reference quantities from the built wing.
        s_ref = self._parm(vsp, wing, "TotalArea", "WingGeom") or spec["S_ref"]
        b_ref = self._parm(vsp, wing, "TotalSpan", "WingGeom") or span
        c_ref = self._parm(vsp, wing, "MAC", "WingGeom") or root_chord * 0.9
        sweep_rad = math.radians(spec["sweep"])
        mac_span_frac = (1.0 / 6.0) * (1.0 + 2.0 * taper) / (1.0 + taper)
        x_mac_quarter = x_wing + (span * 0.5) * mac_span_frac * math.tan(sweep_rad) \
            + 0.25 * c_ref
        refs = {
            "S_ref": round(s_ref, 6),
            "b_ref": round(b_ref, 6),
            "c_ref": round(c_ref, 6),
            "xcg": round(x_mac_quarter, 6),
            "ycg": 0.0,
            "zcg": 0.0,
            "re_ref": 1.0e7,
        }

        try:
            vsp.SetVSPAERORefWingID(wing)
        except Exception as exc:  # noqa: BLE001
            warnings.append("SetVSPAERORefWingID 失败: %s" % exc)

        out_path = Path(self.cfg.workdir) / "models" / (
            _safe_name(spec["name"], "uav") + ".vsp3")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            vsp.SetVSP3FileName(str(out_path))
            vsp.WriteVSPFile(str(out_path), vsp.SET_ALL)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(ERR_EXPORT_FAILED, "写入 .vsp3 失败: %s" % exc)

        derived = {
            "span_m": round(span, 6),
            "root_chord_m": round(root_chord, 6),
            "tip_chord_m": round(tip_chord, 6),
            "mac_m": round(c_ref, 6),
            "fuselage_len_m": round(fuselage_len, 6),
            "fuselage_diameter_m": round(fuselage_len / spec["fuselage_fineness"], 6),
            "x_wing_m": round(x_wing, 6),
            "tail_x_m": round(tail_x, 6),
            "tail_arm_m": round(l_tail, 6),
            "htail_area_m2": round(spec["Sh"], 6),
            "vtail_area_m2": round(sv, 6),
        }
        spec["derived"] = derived
        self.state.update({
            "model_name": spec["name"],
            "vsp3_path": str(out_path),
            "spec": spec,
            "refs": refs,
            "aero_geoms": [vsp.GetGeomName(geom) for geom in aero_ids],
            "thick_geoms": [vsp.GetGeomName(geom) for geom in thick_ids],
            "last_analysis": None,
            "created_at": _now_iso(),
        })
        geometries = [self._geom_brief(vsp, geom) for geom in vsp.FindGeoms()]
        return {
            "ok": True,
            "model_name": spec["name"],
            "vsp3_path": str(out_path),
            "spec": spec,
            "derived": derived,
            "reference": refs,
            "geometries": geometries,
            "geometry_count": len(geometries),
            "aero_geometries": self.state["aero_geoms"],
            "warnings": warnings,
            "files": {"vsp3": str(out_path),
                      "vsp3_size_bytes": out_path.stat().st_size if out_path.is_file() else 0},
        }

    # ------------------------------------------------------------------
    # Tool: openvsp_get_geometry
    # ------------------------------------------------------------------
    def _tool_get_geometry(self, args):
        vsp = self._ensure_vsp()
        path = args.get("path")
        include_parms = bool(args.get("include_parms", True))
        if path:
            model_path = Path(str(path)).expanduser()
            if not model_path.is_file():
                raise ToolError(ERR_IO, "模型文件不存在: %s" % model_path)
            try:
                vsp.ClearVSPModel()
                vsp.ReadVSPFile(str(model_path))
                vsp.Update()
            except Exception as exc:  # noqa: BLE001
                raise ToolError(ERR_IO, "读取 .vsp3 失败: %s" % exc)
            if not vsp.FindGeoms():
                raise ToolError(ERR_IO, "文件中没有可读取的几何: %s" % model_path)
            self.state.update({
                "model_name": model_path.stem,
                "vsp3_path": str(model_path.resolve()),
                "spec": None,
                "refs": None,
                "aero_geoms": [],
                "thick_geoms": [],
                "last_analysis": None,
            })
        else:
            vsp = self._ensure_model()

        geom_ids = vsp.FindGeoms()
        geometries = [self._geom_details(vsp, geom_id, include_parms)
                      for geom_id in geom_ids]
        mins = [1e30, 1e30, 1e30]
        maxs = [-1e30, -1e30, -1e30]
        for item in geometries:
            bbox = item.get("bbox")
            if not bbox:
                continue
            for axis in range(3):
                mins[axis] = min(mins[axis], bbox["min"][axis])
                maxs[axis] = max(maxs[axis], bbox["max"][axis])
        overall = None
        if mins[0] < 1e29:
            overall = {"min": [round(v, 6) for v in mins],
                       "max": [round(v, 6) for v in maxs],
                       "size": [round(maxs[i] - mins[i], 6) for i in range(3)]}
        type_counts = {}
        for item in geometries:
            type_counts[item["type"]] = type_counts.get(item["type"], 0) + 1
        wings = [item for item in geometries if item["type"] == "Wing"]
        summary = {
            "geometry_count": len(geometries),
            "type_counts": type_counts,
            "bounding_box": overall,
            "wing_count": len(wings),
        }
        if self.state.get("refs"):
            summary["reference"] = self.state["refs"]
        return {
            "ok": True,
            "model_name": self.state.get("model_name"),
            "vsp3_path": self.state.get("vsp3_path"),
            "geometries": geometries,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # Tool: openvsp_run_vspaero
    # ------------------------------------------------------------------
    def _derive_refs(self, vsp):
        wings = []
        try:
            geom_ids = vsp.FindGeoms()
        except Exception:  # noqa: BLE001
            geom_ids = []
        for geom_id in geom_ids:
            try:
                if vsp.GetGeomTypeName(geom_id) != "Wing":
                    continue
            except Exception:  # noqa: BLE001
                continue
            area = self._parm(vsp, geom_id, "TotalArea", "WingGeom") or 0.0
            wings.append((area, geom_id))
        if not wings:
            return None
        wings.sort(reverse=True)
        geom_id = wings[0][1]
        s_ref = self._parm(vsp, geom_id, "TotalArea", "WingGeom") or 1.0
        b_ref = self._parm(vsp, geom_id, "TotalSpan", "WingGeom") or 1.0
        c_ref = self._parm(vsp, geom_id, "MAC", "WingGeom") or 1.0
        x_wing = self._parm(vsp, geom_id, "X_Rel_Location", "XForm") or 0.0
        return {
            "S_ref": round(s_ref, 6),
            "b_ref": round(b_ref, 6),
            "c_ref": round(c_ref, 6),
            "xcg": round(x_wing + 0.25 * c_ref, 6),
            "ycg": 0.0,
            "zcg": 0.0,
            "re_ref": 1.0e7,
        }

    def _resolve_refs(self, vsp, override, warnings):
        refs = dict(self.state.get("refs") or {})
        if not refs:
            derived = self._derive_refs(vsp)
            if derived:
                refs = derived
                if not self.state.get("refs"):
                    self.state["refs"] = derived
            else:
                warnings.append("未找到机翼，参考量使用默认值 S_ref=b_ref=c_ref=1")
                refs = {"S_ref": 1.0, "b_ref": 1.0, "c_ref": 1.0,
                        "xcg": 0.0, "ycg": 0.0, "zcg": 0.0, "re_ref": 1.0e7}
        if override is not None:
            if not isinstance(override, dict):
                raise ToolError(ERR_INVALID_PARAMS, "refs 必须是对象")
            for key, value in override.items():
                if key not in ("S_ref", "b_ref", "c_ref", "xcg", "ycg", "zcg", "re_ref"):
                    raise ToolError(ERR_INVALID_PARAMS,
                                    "refs 不支持字段 %r（可用: S_ref/b_ref/c_ref/xcg/ycg/zcg/re_ref）" % key)
                refs[key] = _as_float(value, "refs.%s" % key)
        for key in ("S_ref", "b_ref", "c_ref"):
            if not refs.get(key) or refs[key] <= 0:
                raise ToolError(ERR_INVALID_PARAMS, "参考量 %s 必须为正数" % key)
        refs.setdefault("xcg", 0.0)
        refs.setdefault("ycg", 0.0)
        refs.setdefault("zcg", 0.0)
        refs.setdefault("re_ref", 1.0e7)
        return {key: (round(float(value), 6) if isinstance(value, (int, float)) else value)
                for key, value in refs.items()}

    def _aero_sets(self, vsp, solver, warnings):
        thin_set = self.cfg.aero_set
        thick_set = self.cfg.aero_set + 1
        has_tags = False
        try:
            for geom_id in vsp.FindGeoms():
                if vsp.GetSetFlag(geom_id, thin_set) or vsp.GetSetFlag(geom_id, thick_set):
                    has_tags = True
                    break
        except Exception:  # noqa: BLE001
            has_tags = False
        if has_tags:
            if solver == "panel":
                return thin_set, thick_set, warnings
            return thin_set, vsp.SET_NONE, warnings
        warnings.append("模型没有 geometry-set 标记，按 OpenVSP 显示状态选择薄/厚面；"
                        "solver=vlm 时仅计算薄面（机翼/尾翼）")
        if solver == "panel":
            return vsp.SET_SHOWN, vsp.SET_SHOWN, warnings
        return vsp.SET_SHOWN, vsp.SET_NONE, warnings

    def _configure_vspaero(self, vsp, thin_set, thick_set, refs, mach, vinf,
                           rho, alpha, beta, ncpu, wake_iters, warnings):
        def set_i(analysis, name, value):
            try:
                vsp.SetIntAnalysisInput(analysis, name, [int(value)])
                return True
            except Exception as exc:  # noqa: BLE001
                warnings.append("VSPAERO 输入 %s 不支持: %s" % (name, exc))
                return False

        def set_d(analysis, name, value):
            try:
                vsp.SetDoubleAnalysisInput(analysis, name, [float(value)])
                return True
            except Exception as exc:  # noqa: BLE001
                warnings.append("VSPAERO 输入 %s 不支持: %s" % (name, exc))
                return False

        vsp.SetAnalysisInputDefaults("VSPAEROComputeGeometry")
        set_i("VSPAEROComputeGeometry", "GeomSet", thick_set)
        set_i("VSPAEROComputeGeometry", "ThinGeomSet", thin_set)
        set_i("VSPAEROComputeGeometry", "Symmetry", 0)
        set_i("VSPAEROComputeGeometry", "CullFracFlag", 1)
        set_d("VSPAEROComputeGeometry", "CullFrac", 0.03)

        vsp.SetAnalysisInputDefaults("VSPAEROSweep")
        set_i("VSPAEROSweep", "GeomSet", thick_set)
        set_i("VSPAEROSweep", "ThinGeomSet", thin_set)
        set_i("VSPAEROSweep", "Symmetry", 0)
        set_i("VSPAEROSweep", "RefFlag", 1)
        set_d("VSPAEROSweep", "Sref", refs["S_ref"])
        set_d("VSPAEROSweep", "bref", refs["b_ref"])
        set_d("VSPAEROSweep", "cref", refs["c_ref"])
        set_i("VSPAEROSweep", "UseCGModeFlag", 0)
        set_d("VSPAEROSweep", "Xcg", refs["xcg"])
        set_d("VSPAEROSweep", "Ycg", refs["ycg"])
        set_d("VSPAEROSweep", "Zcg", refs["zcg"])
        set_d("VSPAEROSweep", "AlphaStart", alpha[0])
        set_d("VSPAEROSweep", "AlphaEnd", alpha[1])
        set_i("VSPAEROSweep", "AlphaNpts", alpha[2])
        set_d("VSPAEROSweep", "BetaStart", beta[0])
        set_d("VSPAEROSweep", "BetaEnd", beta[1])
        set_i("VSPAEROSweep", "BetaNpts", beta[2])
        set_d("VSPAEROSweep", "MachStart", mach)
        set_d("VSPAEROSweep", "MachEnd", mach)
        set_i("VSPAEROSweep", "MachNpts", 1)
        set_d("VSPAEROSweep", "Vinf", vinf)
        set_d("VSPAEROSweep", "Rho", rho)
        set_d("VSPAEROSweep", "ReCref", refs.get("re_ref") or 1.0e7)
        set_i("VSPAEROSweep", "NCPU", ncpu)
        set_i("VSPAEROSweep", "WakeNumIter", wake_iters)
        set_i("VSPAEROSweep", "FixedWakeFlag", 0)
        set_i("VSPAEROSweep", "NumWakeNodes", 8)
        set_d("VSPAEROSweep", "WakeRelax", 1.0)
        set_i("VSPAEROSweep", "FreezeWakeAtIteration", 10000)
        vsp.Update()

    def _run_vspaero_stab(self, out_dir: Path, stem: str, ncpu: int):
        exe = None
        try:
            vsp = self._ensure_vsp()
            package = vsp.GetVSPAEROPath()
            candidate = Path(package) / "vspaero.exe" if package else None
            if candidate and candidate.is_file():
                exe = candidate
        except Exception:  # noqa: BLE001
            exe = None
        if exe is None:
            candidate = Path(self.cfg.root) / "vspaero.exe"
            if candidate.is_file():
                exe = candidate
        if exe is None:
            _log(_WARN, "找不到 vspaero.exe，跳过 -stab 稳定导数计算")
            return False
        env = os.environ.copy()
        env["PATH"] = str(Path(self.cfg.root)) + os.pathsep + env.get("PATH", "")
        try:
            proc = subprocess.run(
                [str(exe), "-omp", str(ncpu), "-stab", stem],
                cwd=str(out_dir), env=env, capture_output=True,
                timeout=self.cfg.analysis_timeout)
        except subprocess.TimeoutExpired:
            _log(_WARN, "vspaero -stab 超时（%.0fs）", self.cfg.analysis_timeout)
            return False
        except OSError as exc:
            _log(_WARN, "vspaero -stab 启动失败: %s", exc)
            return False
        if proc.returncode != 0:
            _log(_WARN, "vspaero -stab 退出码 %s: %s", proc.returncode,
                 _tail(proc.stderr, 800))
            return False
        return (out_dir / (stem + ".stab")).is_file()

    def _clean_vspaero_outputs(self, out_dir: Path, stem: str):
        if not out_dir.is_dir():
            return
        for path in out_dir.iterdir():
            if not path.is_file():
                continue
            name = path.name
            if not name.startswith(stem + "."):
                continue
            suffix = name[len(stem):]
            if suffix.startswith(".case."):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if any(suffix == ext or suffix.startswith(ext + ".") or suffix.endswith(ext)
                   for ext in _VSPAERO_OUTPUT_EXTS):
                try:
                    path.unlink()
                except OSError:
                    pass

    def _tool_run_vspaero(self, args):
        case = str(args.get("case") or "polar").strip().lower()
        if case not in ("polar", "stability", "beta"):
            raise ToolError(ERR_INVALID_PARAMS,
                            "case 必须是 polar / stability / beta，收到 %r" % case)
        solver = str(args.get("solver") or "vlm").strip().lower()
        if solver not in ("vlm", "panel"):
            raise ToolError(ERR_INVALID_PARAMS, "solver 必须是 vlm 或 panel")
        started = time.time()
        vsp = self._ensure_model()
        warnings = []

        if case == "polar":
            alpha = _parse_triple(args.get("alpha"), (0.0, 8.0, 5), "alpha", -30.0, 30.0)
            beta = (0.0, 0.0, 1)
        elif case == "stability":
            alpha = _parse_triple(args.get("alpha"), (2.0, 2.0, 1), "alpha", -30.0, 30.0)
            beta = (0.0, 0.0, 1)
        else:
            alpha = _parse_triple(args.get("alpha"), (2.0, 2.0, 1), "alpha", -30.0, 30.0)
            beta = _parse_triple(args.get("beta"), (-4.0, 4.0, 5), "beta", -20.0, 20.0)

        mach = _as_float(args.get("mach"), "mach", default=0.05, lo=0.0, hi=0.9)
        vinf = _as_float(args.get("vinf"), "vinf", default=20.0, lo=0.1, hi=300.0)
        rho = _as_float(args.get("rho"), "rho", default=1.225, lo=0.001, hi=2.0)
        ncpu = int(_as_float(args.get("ncpu"), "ncpu",
                             default=min(4, os.cpu_count() or 4), lo=1, hi=32))
        wake_iters = int(_as_float(args.get("wake_iters"), "wake_iters",
                                   default=3, lo=0, hi=20))
        refs = self._resolve_refs(vsp, args.get("refs"), warnings)
        thin_set, thick_set, warnings = self._aero_sets(vsp, solver, warnings)

        stem = _safe_name(self.state.get("model_name") or "model")
        output_dir = args.get("output_dir")
        if output_dir:
            out_dir = Path(str(output_dir)).expanduser().resolve()
        else:
            out_dir = (Path(self.cfg.workdir) / "vspaero" / ("%s_%s" % (stem, case))).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        self._clean_vspaero_outputs(out_dir, stem)
        model_path = out_dir / (stem + ".vsp3")
        vsp.SetVSP3FileName(str(model_path))
        vsp.Update()
        try:
            vsp.WriteVSPFile(str(model_path), vsp.SET_ALL)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(ERR_ANALYSIS_FAILED, "写出分析用 .vsp3 失败: %s" % exc)

        self._configure_vspaero(vsp, thin_set, thick_set, refs, mach, vinf, rho,
                                alpha, beta, ncpu, wake_iters, warnings)
        _log(_INFO, "VSPAERO case=%s model=%s solver=%s sets=(thin=%s thick=%s) "
                    "alpha=%s beta=%s mach=%s ncpu=%s",
             case, stem, solver, thin_set, thick_set, alpha, beta, mach, ncpu)
        try:
            vsp.ExecAnalysis("VSPAEROComputeGeometry")
        except Exception as exc:  # noqa: BLE001
            raise ToolError(ERR_ANALYSIS_FAILED,
                            "VSPAEROComputeGeometry 失败: %s（请检查几何是否有效）" % exc)
        try:
            vsp.ExecAnalysis("VSPAEROSweep")
        except Exception as exc:  # noqa: BLE001
            raise ToolError(ERR_ANALYSIS_FAILED, "VSPAEROSweep 失败: %s" % exc)

        polar_path = out_dir / (stem + ".polar")
        stab_path = out_dir / (stem + ".stab")
        if case == "stability":
            if not self._run_vspaero_stab(out_dir, stem, ncpu):
                warnings.append("VSPAERO -stab 直接运行失败或超时；"
                                "稳定导数改用极曲线回归估计（SM/Xnp）。")
        if not polar_path.is_file():
            raise ToolError(ERR_ANALYSIS_FAILED,
                            "VSPAERO 未生成 .polar 文件（%s）。可能是几何/参考量/求解设置问题。"
                            % polar_path)
        try:
            header, raw_rows = parse_polar(polar_path)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolError(ERR_ANALYSIS_FAILED, "解析 .polar 失败: %s" % exc)
        if not raw_rows:
            raise ToolError(ERR_ANALYSIS_FAILED, ".polar 中没有有效的工况数据")
        rows = _normalise_polar_rows(raw_rows)
        summary = _polar_summary(rows, refs)
        if case == "beta":
            summary["beta_sweep"] = _beta_summary(rows)

        stability = None
        stab_blocks = []
        if case == "stability":
            if stab_path.is_file():
                try:
                    stab_blocks = parse_stab(stab_path)
                except Exception as exc:  # noqa: BLE001
                    warnings.append("解析 .stab 失败: %s" % exc)
                    stab_blocks = []
            flow_for_modes = {"vinf": vinf, "rho": rho, "mach": mach,
                              "alpha_deg": list(alpha), "beta_deg": list(beta),
                              "case": case, "solver": solver}
            inertia = args.get("inertia")
            if inertia is not None and not isinstance(inertia, dict):
                raise ToolError(ERR_INVALID_PARAMS, "inertia 必须是对象，例如 {\"Iyy\": 1.2}")
            mass_kg = args.get("mass_kg")
            mass_kg = _as_float(mass_kg, "mass_kg", default=None, lo=1e-3) if mass_kg is not None else None
            if mass_kg is None and isinstance(self.state.get("spec"), dict):
                mass_kg = _as_float(self.state["spec"].get("mass_kg"), "mass_kg",
                                    default=None, lo=1e-3) if self.state["spec"].get("mass_kg") else None
            stability = _stability_payload(stab_blocks, rows, refs,
                                           flow_for_modes, mass_kg, inertia)
            if stab_blocks and stability.get("SM") is None:
                stability["SM"] = summary.get("SM_estimated")
                stability["X_np_m"] = summary.get("X_np_estimated_m")

        flow = {
            "case": case,
            "solver": solver,
            "mach": mach,
            "vinf": vinf,
            "rho": rho,
            "alpha_deg": list(alpha),
            "beta_deg": list(beta),
            "sets": {"thin_set": thin_set, "thick_set": thick_set,
                     "symmetry": 0},
        }
        files = {"dir": str(out_dir), "model": str(model_path)}
        for key, suffix in (("polar", ".polar"), ("stab", ".stab"),
                            ("history", ".history"), ("lod", ".lod"),
                            ("vspgeom", ".vspgeom"), ("vspaero_input", ".vspaero"),
                            ("solution_db", ".adb"), ("forces", ".flt")):
            candidate = out_dir / (stem + suffix)
            files[key] = str(candidate) if candidate.is_file() else None
        try:
            files["all"] = sorted(path.name for path in out_dir.iterdir()
                                  if path.is_file())[:100]
        except OSError:
            files["all"] = []
        duration = round(time.time() - started, 2)
        result = {
            "ok": True,
            "case": case,
            "solver": {"name": "VSPAERO", "method": "VLM" if solver == "vlm" else "Panel/VLM",
                       "version": "7.2.2"},
            "flow": flow,
            "reference": refs,
            "rows": rows[:400],
            "row_count": len(rows),
            "summary": summary,
            "stability": stability,
            "files": files,
            "warnings": warnings,
            "duration_s": duration,
        }
        self.state["last_analysis"] = {
            "case": case,
            "timestamp": _now_iso(),
            "reference": refs,
            "flow": flow,
            "summary": summary,
            "stability": stability,
            "files": files,
            "rows": rows[:400],
            "warnings": warnings,
            "duration_s": duration,
        }
        return result

    # ------------------------------------------------------------------
    # Tool: openvsp_export
    # ------------------------------------------------------------------
    def _tool_export(self, args):
        fmt = str(args.get("format") or "vsp3").strip().lower().lstrip(".")
        if fmt not in ("vsp3", "stl", "csv"):
            raise ToolError(ERR_INVALID_PARAMS, "format 必须是 vsp3 / stl / csv")
        vsp = self._ensure_model()
        stem = _safe_name(self.state.get("model_name") or "model")
        output_path = args.get("output_path")
        if output_path:
            out_path = Path(str(output_path)).expanduser()
            if not out_path.is_absolute():
                out_path = Path(self.cfg.workdir) / out_path
        else:
            out_path = Path(self.cfg.workdir) / "exports" / (stem + "." + fmt)
        out_path = out_path.resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        expected_suffix = {"vsp3": ".vsp3", "stl": ".stl", "csv": ".csv"}[fmt]
        if out_path.suffix.lower() != expected_suffix:
            out_path = out_path.with_suffix(expected_suffix)
        try:
            if fmt == "vsp3":
                vsp.SetVSP3FileName(str(out_path))
                vsp.Update()
                vsp.WriteVSPFile(str(out_path), vsp.SET_ALL)
            elif fmt == "stl":
                vsp.ExportFile(str(out_path), vsp.SET_ALL, vsp.EXPORT_STL)
            else:
                try:
                    vsp.SetComputationFileName(vsp.DEGEN_GEOM_CSV_TYPE, str(out_path))
                except Exception:  # noqa: BLE001
                    pass
                vsp.ComputeDegenGeom(vsp.SET_ALL, vsp.DEGEN_GEOM_CSV_TYPE)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(ERR_EXPORT_FAILED, "导出 %s 失败: %s" % (fmt, exc))
        if not out_path.is_file():
            raise ToolError(ERR_EXPORT_FAILED,
                            "导出命令已执行但未找到输出文件: %s" % out_path)
        return {
            "ok": True,
            "format": fmt,
            "output_path": str(out_path),
            "size_bytes": out_path.stat().st_size,
            "note": ("DegenGeom CSV（面板/分段几何数据）" if fmt == "csv" else None),
        }

    # ------------------------------------------------------------------
    # Tool: openvsp_generate_report
    # ------------------------------------------------------------------
    def _report_data(self, args):
        vsp = None
        geometry = []
        try:
            vsp = self._ensure_model()
        except ToolError:
            vsp = None
        if vsp is not None:
            try:
                geometry = [self._geom_details(vsp, geom_id, include_parms=True)
                            for geom_id in vsp.FindGeoms()]
            except Exception:  # noqa: BLE001
                geometry = []
        spec = self.state.get("spec")
        refs = self.state.get("refs") or {}
        analysis = args.get("result")
        if not isinstance(analysis, dict):
            analysis = self.state.get("last_analysis")
        metrics = args.get("metrics") if isinstance(args.get("metrics"), dict) else {}
        notes = []
        if not analysis:
            notes.append("本次报告未包含 VSPAERO 结果；请先运行 openvsp_run_vspaero，"
                         "或在调用参数 result 中传入计算结果。")
        notes.append("报告由 OpenVSP MCP 服务器自动生成；VLM 结果不含粘性/压缩性修正。")
        return {
            "generated_at": _now_iso(),
            "spec": spec,
            "geometry": geometry,
            "reference": refs,
            "analysis": analysis,
            "metrics": metrics,
            "notes": notes,
        }

    def _try_ai_report(self, ai, report_data, out_path):
        report_module = _import_ai_report_module(ai)
        candidates = []
        for module in (report_module, ai):
            if module is None:
                continue
            for func_name in ("generate_report", "build_report", "create_report",
                              "write_report", "generate"):
                func = getattr(module, func_name, None)
                if callable(func):
                    candidates.append(func)
        if not candidates:
            return None
        analysis = report_data.get("analysis")
        ai_analysis = _adapt_analysis_for_ai(analysis)
        payload = {
            "result": ai_analysis,
            "analysis": ai_analysis,
            "metrics": report_data.get("metrics"),
            "spec": report_data.get("spec"),
            "geometry": report_data.get("geometry"),
            "reference": report_data.get("reference"),
            "report_path": str(out_path),
            "output_path": str(out_path),
            "output_dir": str(out_path.parent),
            "title": "OpenVSP 气动分析报告",
            "data": report_data,
        }
        for func in candidates:
            try:
                returned = _call_flexible(func, payload)
            except Exception as exc:  # noqa: BLE001
                _log(_DEBUG, "ai_design 报告接口调用失败: %s", exc)
                continue
            if isinstance(returned, (str, os.PathLike)):
                candidate = Path(returned)
                if candidate.is_file():
                    return str(candidate)
                if not candidate.is_absolute():
                    candidate = out_path.parent / candidate
                if candidate.is_file():
                    return str(candidate)
            if out_path.is_file():
                return str(out_path)
        return None

    def _tool_generate_report(self, args):
        report_path = args.get("report_path")
        if report_path:
            out_path = Path(str(report_path)).expanduser()
            if not out_path.is_absolute():
                out_path = Path(self.cfg.workdir) / out_path
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            stem = _safe_name(self.state.get("model_name") or "openvsp")
            out_path = Path(self.cfg.workdir) / "reports" / (
                "%s_aero_report_%s.docx" % (stem, stamp))
        out_path = out_path.resolve()
        if out_path.suffix.lower() != ".docx":
            out_path = out_path.with_suffix(".docx")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        title = str(args.get("title") or "OpenVSP 气动设计分析报告")
        report_data = self._report_data(args)

        engine_used = None
        ai = _load_ai_design(self.cfg)
        if ai is not None:
            ai_path = self._try_ai_report(ai, report_data, out_path)
            if ai_path:
                engine_used = "ai_design"
                out_path = Path(ai_path)
        if engine_used is None:
            try:
                engine_used = _write_report_docx(out_path, report_data, title)
            except Exception as exc:  # noqa: BLE001
                raise ToolError(ERR_REPORT_FAILED, "生成 Word 报告失败: %s" % exc)
        if not out_path.is_file():
            raise ToolError(ERR_REPORT_FAILED,
                            "报告生成后未找到文件: %s" % out_path)
        sections = []
        if report_data.get("spec"):
            sections.append("设计参数")
        if report_data.get("geometry"):
            sections.append("几何组件")
        if report_data.get("analysis"):
            sections.append("VSPAERO 结果")
        if (report_data.get("analysis") or {}).get("stability"):
            sections.append("稳定性导数")
        return {
            "ok": True,
            "report_path": str(out_path),
            "engine": engine_used,
            "size_bytes": out_path.stat().st_size,
            "sections": sections,
            "ai_design_available": ai is not None,
            "note": None if engine_used == "ai_design"
                    else "ai_design.report 不可用，已使用本地报告生成器",
        }

    # ------------------------------------------------------------------
    # Tool: openvsp_list_files
    # ------------------------------------------------------------------
    def _tool_list_files(self, args):
        raw_dir = args.get("dir")
        base = Path(str(raw_dir)).expanduser() if raw_dir else Path(self.cfg.workdir)
        if not base.is_absolute():
            base = Path(self.cfg.workdir) / base
        base = base.resolve()
        if not base.is_dir():
            raise ToolError(ERR_IO, "目录不存在: %s" % base)
        recursive = bool(args.get("recursive", False))
        pattern = str(args.get("pattern") or "*")
        limit = int(_as_float(args.get("limit"), "limit", default=200, lo=1, hi=2000))
        entries = []
        paths = base.rglob(pattern) if recursive else base.glob(pattern)
        for path in sorted(paths, key=lambda p: str(p).lower()):
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append({
                "name": path.name,
                "path": str(path),
                "relative_path": str(path.relative_to(base)),
                "is_dir": path.is_dir(),
                "size_bytes": 0 if path.is_dir() else stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).astimezone()
                            .isoformat(timespec="seconds"),
            })
            if len(entries) >= limit:
                break
        return {
            "ok": True,
            "dir": str(base),
            "pattern": pattern,
            "recursive": recursive,
            "entry_count": len(entries),
            "truncated": len(entries) >= limit,
            "entries": entries,
        }


# ---------------------------------------------------------------------------
# MCP tool catalogue (JSON Schema)
# ---------------------------------------------------------------------------

_SPEC_SCHEMA_PROPERTIES = {
    "name": {"type": "string", "description": "模型名称（用于 .vsp3 文件名），默认 UAV"},
    "S_ref": {"type": "number", "description": "机翼参考面积 S_ref，m²，默认 1.2"},
    "AR": {"type": "number", "description": "机翼展弦比 AR，默认 8（翼展 b = sqrt(AR*S_ref)）"},
    "taper": {"type": "number", "description": "机翼梢根比 0<taper<=1，默认 0.7"},
    "sweep": {"type": "number", "description": "机翼前缘后掠角，deg，默认 2"},
    "dihedral": {"type": "number", "description": "机翼上反角，deg，默认 3"},
    "cant": {"type": "number", "description": "机翼 cant（整翼绕 X 轴滚转），deg，默认 0"},
    "v_tail": {"type": "boolean", "description": "是否生成垂尾，默认 true"},
    "Sh": {"type": "number", "description": "平尾面积，m²，默认 0.18*S_ref"},
    "Vv": {"type": "number", "description": "垂尾容积系数 Vv，默认 0.05（Sv=Vv*S_ref*b/l_t）"},
    "engine": {"type": "string", "description": "动力类型：none/electric/piston/turboprop/jet/turbofan 仅记录；tractor/pusher/propeller 额外生成螺旋桨几何（不参与 VLM）"},
    "fuselage_len": {"type": "number", "description": "机身长度，m，默认 0.45*翼展"},
    "fuselage_fineness": {"type": "number", "description": "机身长细比，默认 7"},
    "include_fuselage": {"type": "boolean", "description": "是否生成机身（POD 体），默认 true"},
    "h_tail_AR": {"type": "number", "description": "平尾展弦比，默认 4"},
    "v_tail_AR": {"type": "number", "description": "垂尾展弦比，默认 2"},
    "tail_taper": {"type": "number", "description": "尾翼梢根比，默认 0.6"},
    "tail_sweep": {"type": "number", "description": "尾翼后掠角，deg，默认 5"},
    "x_wing": {"type": "number", "description": "机翼前缘 X 位置，m，默认 0.28*机身长"},
    "tail_x": {"type": "number", "description": "尾翼前缘 X 位置，m，默认 0.72*机身长"},
}

_TOOLS = [
    {
        "name": "openvsp_status",
        "title": "OpenVSP 环境状态",
        "description": "检查 OpenVSP 版本、安装目录、OpenVSP Python API / VSPAERO 可用性、"
                       "当前模型状态、工作目录与协议版本。建议首先调用。",
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "openvsp_new_model",
        "title": "新建空模型",
        "description": "清空当前 OpenVSP 模型并开始一个新模型（name 用于 .vsp3 文件名）。",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "模型名称"}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "openvsp_build_aircraft",
        "title": "生成飞机几何",
        "description": "按参数 spec 生成常规布局小型飞机：机翼（梯形+后掠+上反+可选中/垂尾）"
                       "+ 可选机身（POD）+ 可选螺旋桨。默认全薄面 VLM 配置，返回组件、参考量"
                       "（S_ref/b_ref/c_ref/xcg）、派生尺寸与 .vsp3 路径。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "object",
                    "description": "飞机参数对象；全部字段有默认值，可只传部分字段",
                    "properties": _SPEC_SCHEMA_PROPERTIES,
                    "additionalProperties": True,
                },
            },
            "required": ["spec"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "openvsp_get_geometry",
        "title": "读取几何信息",
        "description": "返回当前模型的几何组件列表（名称/类型/包围盒/关键参数：机翼展长、面积、"
                       "MAC、根梢弦长、后掠/上反、机身长度、桨径等）。可传 path 直接读取 .vsp3。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "可选：.vsp3 文件路径（读取后成为当前模型）"},
                "include_parms": {"type": "boolean", "description": "是否返回详细参数，默认 true"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "openvsp_run_vspaero",
        "title": "运行 VSPAERO 气动分析",
        "description": "运行 VSPAERO。case=polar 生成 CL/CD/L-D 极曲线；case=stability 额外计算"
                       "稳定性导数与 SM/Xnp（-stab）；case=beta 做侧滑扫描。返回解析后的行数据、"
                       "指标摘要、稳定性导数、模态估计与输出文件路径。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "case": {"type": "string", "enum": ["polar", "stability", "beta"],
                         "description": "分析类型，默认 polar"},
                "alpha": {"type": ["number", "array"], "items": {"type": "number"},
                          "description": "迎角范围：数字（单点）或 [start,end] 或 [start,end,npts]；"
                                         "默认 polar=[0,8,5]、stability=[2,2,1]、beta=[2,2,1]"},
                "beta": {"type": ["number", "array"], "items": {"type": "number"},
                         "description": "侧滑角范围（仅 case=beta），默认 [-4,4,5]"},
                "mach": {"type": "number", "description": "马赫数，默认 0.05"},
                "vinf": {"type": "number", "description": "来流速度 Vinf，m/s，默认 20"},
                "rho": {"type": "number", "description": "空气密度 rho，kg/m³，默认 1.225"},
                "refs": {
                    "type": "object",
                    "description": "参考量覆盖：S_ref(m²)/b_ref(m)/c_ref(m)/xcg/ycg/zcg/re_ref",
                    "properties": {
                        "S_ref": {"type": "number"},
                        "b_ref": {"type": "number"},
                        "c_ref": {"type": "number"},
                        "xcg": {"type": "number"},
                        "ycg": {"type": "number"},
                        "zcg": {"type": "number"},
                        "re_ref": {"type": "number"},
                    },
                    "additionalProperties": False,
                },
                "solver": {"type": "string", "enum": ["vlm", "panel"],
                           "description": "vlm=全薄面 VLM（默认，稳定）；panel=薄面+厚体混合"},
                "ncpu": {"type": "integer", "description": "并行 CPU 数，默认 4"},
                "wake_iters": {"type": "integer", "description": "尾迹迭代次数，默认 3"},
                "output_dir": {"type": "string", "description": "输出目录（默认 workspace/vspaero/<model>_<case>）"},
                "mass_kg": {"type": "number", "description": "可选：质量，用于短周期模态估计"},
                "inertia": {"type": "object", "description": "可选：惯量 {Iyy: ...} 用于模态估计"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "openvsp_export",
        "title": "导出模型/网格",
        "description": "导出当前模型：format=vsp3（OpenVSP 工程）、stl（三角网格）、"
                       "csv（DegenGeom 面板/分段几何数据）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["vsp3", "stl", "csv"]},
                "output_path": {"type": "string", "description": "输出路径（默认 workspace/exports/）"},
            },
            "required": ["format"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "openvsp_generate_report",
        "title": "生成分析报告",
        "description": "生成 Word 报告（.docx）。优先调用 ai_design.report（若 ../ai 可用），"
                       "否则用本地生成器；可传入 result（VSPAERO 结果）/metrics 补充内容。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "result": {"type": "object", "description": "可选：openvsp_run_vspaero 的返回结果"},
                "metrics": {"type": "object", "description": "可选：额外指标键值"},
                "report_path": {"type": "string", "description": "输出 .docx 路径"},
                "title": {"type": "string", "description": "报告标题"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "openvsp_list_files",
        "title": "列出文件",
        "description": "列出目录文件（默认 workspace），返回名称/大小/修改时间，便于查看结果文件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dir": {"type": "string", "description": "目录路径，默认工作目录"},
                "recursive": {"type": "boolean", "description": "是否递归，默认 false"},
                "pattern": {"type": "string", "description": "glob 模式，默认 *"},
                "limit": {"type": "integer", "description": "最多返回条数，默认 200"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
]

_TOOL_BY_NAME = {tool["name"]: tool for tool in _TOOLS}


# ---------------------------------------------------------------------------
# stdio framing + protocol server
# ---------------------------------------------------------------------------


class FrameIO:
    """Newline-delimited JSON-RPC framing (with tolerant LSP-style fallback)."""

    def __init__(self, raw_in, raw_out):
        self.raw_in = raw_in
        self.raw_out = raw_out
        self.buffer = b""

    def _fill(self) -> bool:
        try:
            chunk = self.raw_in.read(65536)
        except (OSError, ValueError):
            return False
        if not chunk:
            return False
        self.buffer += chunk
        return True

    def read_payload(self):
        while True:
            index = self.buffer.find(b"\n")
            if index < 0:
                if not self._fill():
                    remaining = self.buffer.strip()
                    self.buffer = b""
                    return remaining or None
                continue
            line = self.buffer[:index].strip()
            self.buffer = self.buffer[index + 1:]
            if not line:
                continue
            if line.lower().startswith(b"content-length:"):
                try:
                    length = int(line.split(b":", 1)[1].strip())
                except (ValueError, IndexError):
                    return line
                while True:  # consume remaining headers up to the blank line
                    head_end = self.buffer.find(b"\n")
                    if head_end < 0:
                        if not self._fill():
                            return None
                        continue
                    header_line = self.buffer[:head_end].strip()
                    self.buffer = self.buffer[head_end + 1:]
                    if not header_line:
                        break
                while len(self.buffer) < length:
                    if not self._fill():
                        return None
                payload = self.buffer[:length]
                self.buffer = self.buffer[length:]
                return payload.strip() or None
            return line

    def send(self, obj) -> None:
        data = json.dumps(obj, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
        self.raw_out.write(data + b"\n")
        self.raw_out.flush()


class McpServer:
    def __init__(self, engine: Engine, cfg: Config):
        self.engine = engine
        self.cfg = cfg
        self.initialized = False
        self.initializing = False
        self.client_version = None
        self.client_info = None
        self.running = True

    # -- loop -----------------------------------------------------------
    def serve(self, io: FrameIO) -> None:
        while self.running:
            payload = io.read_payload()
            if payload is None:
                _log(_INFO, "stdin EOF，服务器退出")
                break
            try:
                message = json.loads(payload.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                io.send({"jsonrpc": "2.0", "id": None,
                         "error": {"code": PARSE_ERROR,
                                   "message": "Parse error: %s" % exc}})
                continue
            try:
                responses = self.handle(message)
            except JsonRpcError as exc:
                responses = [{"jsonrpc": "2.0",
                              "id": message.get("id") if isinstance(message, dict) else None,
                              "error": exc.to_dict()}]
            except Exception as exc:  # noqa: BLE001 - never crash the loop
                _log(_ERROR, "处理消息异常: %s\n%s", exc, traceback.format_exc())
                responses = [{"jsonrpc": "2.0",
                              "id": message.get("id") if isinstance(message, dict) else None,
                              "error": {"code": INTERNAL_ERROR,
                                        "message": "Internal error: %s" % exc}}]
            if isinstance(message, list):
                if responses:
                    io.send(responses)
            else:
                for response in responses:
                    io.send(response)
            if not self.running:
                break

    # -- dispatch -------------------------------------------------------
    def handle(self, message):
        if isinstance(message, list):
            if not message:
                return [{"jsonrpc": "2.0", "id": None,
                         "error": {"code": INVALID_REQUEST,
                                   "message": "Invalid Request: empty batch"}}]
            responses = []
            for item in message:
                response = self.handle_one(item)
                if response is not None:
                    responses.append(response)
            return responses
        response = self.handle_one(message)
        return [] if response is None else [response]

    def handle_one(self, message):
        if not isinstance(message, dict):
            return self._error_response(None, INVALID_REQUEST,
                                        "Invalid Request: message must be an object")
        if message.get("jsonrpc") != "2.0":
            return self._error_response(message.get("id"), INVALID_REQUEST,
                                        "Invalid Request: jsonrpc must be \"2.0\"")
        method = message.get("method")
        if not isinstance(method, str) or not method:
            return self._error_response(message.get("id"), INVALID_REQUEST,
                                        "Invalid Request: missing method")
        has_id = "id" in message
        message_id = message.get("id")
        if message_id is not None and isinstance(message_id, (dict, list)):
            return self._error_response(None, INVALID_REQUEST,
                                        "Invalid Request: id must be a string, number or null")
        params = message.get("params")
        if params is not None and not isinstance(params, (dict, list)):
            if not has_id:
                return None
            return self._error_response(message_id, INVALID_PARAMS,
                                        "Invalid params: params must be an object")
        if not has_id:
            self._handle_notification(method, params)
            return None
        started = time.time()
        try:
            result = self._dispatch(method, params if isinstance(params, dict) else {},
                                    message_id)
        except JsonRpcError as exc:
            response = self._error_response(message_id, exc.code, exc.message, exc.data)
            _log(_WARN, "%s (id=%r) -> error %s: %s", method, message_id,
                 exc.code, exc.message)
            return response
        except ToolError as exc:
            response = self._error_response(message_id, INTERNAL_ERROR,
                                            "Internal error: %s" % exc.message)
            _log(_ERROR, "%s (id=%r) 未处理 ToolError: %s", method, message_id, exc.message)
            return response
        finally:
            duration_ms = (time.time() - started) * 1000.0
            _log(_DEBUG, "%s (id=%r) %.1f ms", method, message_id, duration_ms)
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    @staticmethod
    def _error_response(message_id, code, message, data=None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = _json_safe(data)
        return {"jsonrpc": "2.0", "id": message_id, "error": error}

    def _handle_notification(self, method, params) -> None:
        if method in ("notifications/initialized", "initialized"):
            self.initialized = True
            _log(_INFO, "客户端握手完成 (notifications/initialized)")
        elif method == "notifications/cancelled":
            _log(_INFO, "收到取消通知: %s", params)
        elif method == "exit":
            _log(_INFO, "收到 exit 通知")
            self.running = False
        elif method.startswith("notifications/"):
            _log(_DEBUG, "忽略通知: %s", method)
        else:
            _log(_DEBUG, "忽略未知通知/消息: %s", method)

    def _dispatch(self, method, params, message_id):
        if method == "initialize":
            return self._on_initialize(params)
        # Modern-era detection: a client that skips the legacy handshake and
        # sends ``_meta`` protocolVersion (2026-07-28+) gets a clear hint.
        if not self.initialized:
            meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None
            if meta:
                version = meta.get("io.modelcontextprotocol/protocolVersion")
                if isinstance(version, str) and version.startswith("2026"):
                    raise JsonRpcError(
                        INVALID_REQUEST,
                        "Modern MCP protocol %s 不受支持：本服务器仅实现 legacy 握手 "
                        "(protocolVersion=%s)。请让客户端使用 legacy initialize 流程。"
                        % (version, PROTOCOL_VERSION),
                        data={"protocolEra": "legacy",
                              "protocolVersion": PROTOCOL_VERSION,
                              "supportedProtocolVersions": list(SUPPORTED_PROTOCOLS)})
        if method in ("notifications/initialized", "initialized"):
            self.initialized = True
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": _TOOLS}
        if method == "tools/call":
            return self._on_tools_call(params)
        if method == "shutdown":
            self.running = False
            _log(_INFO, "收到 shutdown，服务器即将退出")
            return None
        if method == "server/discover":
            raise JsonRpcError(
                METHOD_NOT_FOUND,
                "Method not found: server/discover。本服务器实现 legacy MCP "
                "(initialize + notifications/initialized, protocolVersion=%s)，"
                "未实现 modern MCP %s 的 server/discover/_meta 流程。"
                % (PROTOCOL_VERSION, MODERN_PROTOCOL),
                data={"protocolEra": "legacy",
                      "protocolVersion": PROTOCOL_VERSION,
                      "supportedProtocolVersions": list(SUPPORTED_PROTOCOLS),
                      "modernProtocolVersion": MODERN_PROTOCOL,
                      "hint": "请使用 legacy 客户端或按 initialize 握手接入。"})
        if method.startswith("notifications/"):
            return None
        raise JsonRpcError(METHOD_NOT_FOUND, "Method not found: %s" % method)

    def _on_initialize(self, params):
        requested = params.get("protocolVersion")
        self.initialized = True
        self.client_version = requested if isinstance(requested, str) else None
        self.client_info = params.get("clientInfo")
        negotiated = PROTOCOL_VERSION
        if isinstance(requested, str):
            version = requested.strip()
            if version in SUPPORTED_PROTOCOLS:
                negotiated = version
        result = {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": SERVER_NAME,
                "title": SERVER_TITLE,
                "version": SERVER_VERSION,
            },
            "instructions": INSTRUCTIONS,
        }
        if isinstance(requested, str) and requested not in SUPPORTED_PROTOCOLS:
            result["_meta"] = {
                "openvsp-mcp/requestedProtocolVersion": requested,
                "openvsp-mcp/negotiatedProtocolVersion": negotiated,
                "openvsp-mcp/note": (
                    "本服务器仅支持 legacy MCP 握手（2025-11-25 / 2025-06-18 / "
                    "2024-11-05）。modern MCP 2026-07-28 的 server/discover 流程未实现；"
                    "若客户端强制 modern，将无法建立会话。"),
            }
            _log(_WARN, "客户端请求协议 %s，已降级协商到 %s", requested, negotiated)
        _log(_INFO, "initialize: client=%s version=%s -> %s",
             self.client_info.get("name") if isinstance(self.client_info, dict) else None,
             requested, negotiated)
        return result

    def _on_tools_call(self, params):
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise JsonRpcError(INVALID_PARAMS,
                               "Invalid params: tools/call 需要字符串参数 name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise JsonRpcError(INVALID_PARAMS,
                               "Invalid params: arguments 必须是对象")
        if name not in _TOOL_BY_NAME:
            raise JsonRpcError(INVALID_PARAMS,
                               "Invalid params: 未知工具 %r" % name,
                               data={"available_tools": list(_TOOL_BY_NAME)})
        started = time.time()
        try:
            result = self.engine.call(name, arguments)
            engine_used = "worker" if self.engine._worker_mode else "in-process"
            payload = {"ok": True, "tool": name, "engine": engine_used}
            for key, value in (result or {}).items():
                if key == "ok":
                    continue
                payload[key] = value
            text = json.dumps(_json_safe(payload), ensure_ascii=False, indent=2)
            _log(_INFO, "tools/call %s ok (%.1fs)", name, time.time() - started)
            return {"content": [{"type": "text", "text": _truncate_text(text)}],
                    "isError": False}
        except ToolError as exc:
            _log(_WARN, "tools/call %s 失败 [%s]: %s", name, exc.code, exc.message)
            text = json.dumps({"ok": False, "tool": name,
                               "error": exc.to_dict()},
                              ensure_ascii=False, indent=2)
            return {"content": [{"type": "text", "text": _truncate_text(text)}],
                    "isError": True}
        except Exception as exc:  # noqa: BLE001
            _log(_ERROR, "tools/call %s 内部异常: %s\n%s", name, exc,
                 traceback.format_exc())
            text = json.dumps({"ok": False, "tool": name,
                               "error": {"code": ERR_INTERNAL,
                                         "message": "%s: %s" % (type(exc).__name__, exc)}},
                              ensure_ascii=False, indent=2)
            return {"content": [{"type": "text", "text": _truncate_text(text)}],
                    "isError": True}


def _truncate_text(text: str, limit: int = 200000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (结果已截断，原始内容过大)"


# ---------------------------------------------------------------------------
# stdio hardening
# ---------------------------------------------------------------------------


def harden_stdio():
    """Protect the JSON-RPC channel from C++ prints and child stdin reads.

    Returns ``(raw_in, raw_out)`` duplicated file objects for protocol I/O.
    fd 1 is redirected to stderr (OpenVSP C++ prints go to stderr) and fd 0 to
    /dev/null (child processes cannot steal protocol lines).
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    raw_out = os.fdopen(os.dup(1), "wb", buffering=0)
    raw_in = os.fdopen(os.dup(0), "rb", buffering=0)
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        try:
            os.dup2(2, 1)
        finally:
            os.dup2(devnull, 0)
            os.close(devnull)
    except OSError as exc:
        _log(_WARN, "stdio 重定向部分失败: %s", exc)
    try:
        sys.stdout = sys.stderr
    except Exception:  # noqa: BLE001
        pass
    return raw_in, raw_out


# ---------------------------------------------------------------------------
# Worker entry point
# ---------------------------------------------------------------------------


def worker_main(request_path: Path, response_path: Path) -> int:
    harden_stdio()
    engine = None
    state = {}
    try:
        cfg = resolve_config()
        cfg.force_subprocess = False
        cfg.analysis_subprocess = False
        _set_log_level(cfg.log_level)
        request = json.loads(Path(request_path).read_text(encoding="utf-8"))
        state = request.get("state") or {}
        op = request.get("op")
        args = request.get("args") or {}
        engine = Engine(cfg, state)
        result = engine._call_local(op, args)
        response = {"ok": True, "result": _json_safe(result),
                    "state": _json_safe(engine.state)}
    except ToolError as exc:
        response = {"ok": False, "error": exc.to_dict(),
                    "state": _json_safe(engine.state if engine else state)}
    except Exception as exc:  # noqa: BLE001
        _log(_ERROR, "worker 异常: %s\n%s", exc, traceback.format_exc())
        response = {"ok": False,
                    "error": {"code": ERR_INTERNAL,
                              "message": "%s: %s" % (type(exc).__name__, exc),
                              "data": {"traceback": traceback.format_exc()[-4000:]}},
                    "state": _json_safe(engine.state if engine else state)}
    try:
        Path(response_path).write_text(
            json.dumps(_json_safe(response), ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        _log(_ERROR, "worker 写入响应失败: %s", exc)
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def _print_status() -> int:
    cfg = resolve_config()
    _set_log_level("ERROR")
    engine = Engine(cfg)
    status = _json_safe(engine.call("openvsp_status", {}))
    sys.stdout.write(json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    return 0 if status.get("openvsp", {}).get("available") else 1


def _self_test(cfg: Config) -> int:
    """Minimal in-process smoke test used for quick diagnostics."""
    _set_log_level(cfg.log_level)
    engine = Engine(cfg)
    tools = ["openvsp_status"] + [name for name in Engine.TOOL_METHODS
                                  if name != "openvsp_status"]
    status = engine.call("openvsp_status", {})
    print("status: openvsp.available=%s version=%s vspaero=%s"
          % (status.get("openvsp", {}).get("available"),
             status.get("openvsp", {}).get("version"),
             status.get("openvsp", {}).get("vspaero_available")))
    if not status.get("openvsp", {}).get("available"):
        return 1
    result = engine.call("openvsp_build_aircraft", {"spec": {
        "name": "selftest_uav", "S_ref": 1.2, "AR": 8.0}})
    print("build: %s geometries=%s vsp3=%s"
          % (result.get("ok"), result.get("geometry_count"),
             result.get("vsp3_path")))
    geom = engine.call("openvsp_get_geometry", {})
    print("geometry: %s components=%s"
          % (geom.get("ok"), geom.get("summary", {}).get("geometry_count")))
    print("tools: %s" % ", ".join(tools))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="openvsp-mcp",
        description="OpenVSP R2 MCP server (stdio, legacy MCP 2025-11-25)")
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    parser.add_argument("--status", action="store_true",
                        help="打印 JSON 环境状态（openvsp_status 内容）")
    parser.add_argument("--self-test", action="store_true",
                        help="运行最小自检（status + build_aircraft + geometry）")
    parser.add_argument("--worker", nargs=2, metavar=("REQUEST", "RESPONSE"),
                        help="内部使用：worker 子进程模式")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.version:
        sys.stdout.write("%s %s (protocol %s)\n"
                         % (SERVER_NAME, SERVER_VERSION, PROTOCOL_VERSION))
        return 0
    if args.worker:
        return worker_main(Path(args.worker[0]), Path(args.worker[1]))

    cfg = resolve_config()
    _set_log_level(cfg.log_level)
    if args.status:
        return _print_status()
    if args.self_test:
        return _self_test(cfg)

    raw_in, raw_out = harden_stdio()
    frame_io = FrameIO(raw_in, raw_out)
    engine = Engine(cfg)
    server = McpServer(engine, cfg)
    _log(_INFO, "%s %s 启动 (legacy MCP %s, pid=%s, root=%s, workdir=%s)",
         SERVER_NAME, SERVER_VERSION, PROTOCOL_VERSION, os.getpid(),
         cfg.root, cfg.workdir)
    try:
        server.serve(frame_io)
    except KeyboardInterrupt:
        _log(_INFO, "收到 Ctrl+C，退出")
    except Exception as exc:  # noqa: BLE001
        _log(_ERROR, "服务器异常退出: %s\n%s", exc, traceback.format_exc())
        return 1
    finally:
        _log(_INFO, "服务器已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
