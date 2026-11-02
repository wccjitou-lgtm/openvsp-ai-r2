"""OpenVSP Python API 桥接层。

职责
----
- 惰性导入 OpenVSP（仅在真正需要建模/求解时才加载 DLL 与 Python 包）
- 以 SI 单位（m / m² / m/s / kg/m³）为唯一对外接口，内部换算为 OpenVSP
  使用的英尺（OpenVSP 3.x API 的模型长度单位为 ft）
- 提供几何构建原语：机翼、机身、V 尾 / 常规尾、操纵面
- 参数设置、保存 .vsp3、导出 STL / DegenGeom CSV
- 运行 VSPAERO（subprocess，稳定获取 .polar/.stab），解析结果

已知 OpenVSP 3.51.3 API 事实（实测）
-----------------------------------
- ``vsp.GetParmVal/SetParmVal`` 数值单位为 ft（模型英寸制默认内部单位）。
- ``vsp.GetGeomBBoxMin/Max`` 不含对称镜像副本，不能用于统计全机尺寸。
- VSPAERO 求解器输入文件（``*.vspaero``）需使用 ft/slug 单位体系。
- ``vspaero.exe <base> -omp N -stab`` 会额外输出 ``<base>.stab``，其中包含
  每个迎角算例的稳定性导数、``SM`` 与 ``X_np``（均以 Cref 归一）。
- 捕获 vspaero 控制台输出时必须指定 ``errors="replace"``，否则 Windows 中文
  代码页下 subprocess 读线程会抛 UnicodeDecodeError。
"""

from __future__ import annotations

import glob
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# 单位换算常量
# --------------------------------------------------------------------------- #
M_TO_FT = 3.280839895013123
FT_TO_M = 1.0 / M_TO_FT
M2_TO_FT2 = M_TO_FT ** 2
KG_TO_SLUG = 1.0 / 14.593902937206364
KGPM3_TO_SLUGPFT3 = KG_TO_SLUG / (M_TO_FT ** 3)  # ≈ 0.00194032
MS_TO_FPS = M_TO_FT
ISA_T0 = 288.15
ISA_LAPSE = 0.0065
ISA_P0 = 101325.0
ISA_RHO0 = 1.225
ISA_R = 287.05287
ISA_GAMMA = 1.4
GRAVITY = 9.80665


class VSPError(RuntimeError):
    """OpenVSP / VSPAERO 相关错误。"""


def standard_atmosphere(altitude_m: float) -> dict[str, float]:
    """ISA 对流层标准大气（0~11 km）。"""
    h = max(-500.0, min(float(altitude_m), 11000.0))
    temperature = ISA_T0 - ISA_LAPSE * h
    pressure = ISA_P0 * (temperature / ISA_T0) ** (GRAVITY / (ISA_R * ISA_LAPSE))
    density = pressure / (ISA_R * temperature)
    sound_speed = math.sqrt(ISA_GAMMA * ISA_R * temperature)
    return {
        "temperature_k": temperature,
        "pressure_pa": pressure,
        "density_kgm3": density,
        "sound_speed_ms": sound_speed,
    }


# --------------------------------------------------------------------------- #
# OpenVSP 定位与惰性导入
# --------------------------------------------------------------------------- #
_OPENVSP_MODULE: Any = None
_OPENVSP_ROOT: str | None = None


def _candidate_openvsp_roots() -> list[str]:
    candidates: list[str] = []
    for env_name in ("OPENVSP_ROOT", "VSP_ROOT", "OPENVSP_HOME"):
        value = os.environ.get(env_name)
        if value:
            candidates.append(value)
    candidates.extend([
        r"C:\OpenVSP-3.51.3-win64",
        r"D:\OpenVSP-3.51.3-win64",
    ])
    for pattern in (r"C:\OpenVSP-*", r"D:\OpenVSP-*",
                    r"C:\Program Files\OpenVSP*"):
        candidates.extend(sorted(glob.glob(pattern), reverse=True))
    seen: list[str] = []
    for item in candidates:
        if item and item not in seen:
            seen.append(item)
    return seen


def find_openvsp_root(root_hint: str | None = None) -> str | None:
    """定位 OpenVSP 安装目录（包含 vspaero.exe 的目录）。"""
    candidates: list[str] = []
    if root_hint:
        candidates.append(root_hint)
    candidates.extend(_candidate_openvsp_roots())
    for candidate in candidates:
        folder = Path(candidate)
        if folder.is_dir() and (folder / "vspaero.exe").is_file():
            return str(folder.resolve())
    return None


def ensure_openvsp(root_hint: str | None = None) -> Any:
    """惰性导入 ``openvsp`` 模块（含 DLL 目录与 Python 搜索路径）。"""
    global _OPENVSP_MODULE, _OPENVSP_ROOT
    if _OPENVSP_MODULE is not None:
        return _OPENVSP_MODULE
    root = find_openvsp_root(root_hint)
    if root is None:
        raise VSPError(
            "未找到 OpenVSP 安装目录（需要包含 vspaero.exe）。"
            "请设置环境变量 OPENVSP_ROOT 或在配置 vsp.openvsp_root 指定。")
    _OPENVSP_ROOT = root
    py_root = Path(root) / "python"
    for path in (py_root / "openvsp", py_root / "openvsp" / "openvsp",
                 py_root / "degen_geom", py_root / "utilities"):
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))
    try:
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(root)
    except OSError:  # pragma: no cover
        pass
    os.environ["PATH"] = root + os.pathsep + os.environ.get("PATH", "")
    try:
        import openvsp  # type: ignore
    except Exception as exc:  # pragma: no cover - 环境问题
        raise VSPError(f"导入 openvsp 失败（root={root}）: {exc}") from exc
    try:
        version = openvsp.GetVSPVersion()
    except Exception:  # pragma: no cover
        version = "unknown"
    _OPENVSP_MODULE = openvsp
    _OPENVSP_ROOT = root
    return openvsp


def openvsp_version() -> str:
    try:
        module = ensure_openvsp()
        return str(module.GetVSPVersion())
    except Exception:
        return "unavailable"


# --------------------------------------------------------------------------- #
# 几何组件元数据
# --------------------------------------------------------------------------- #
@dataclass
class Component:
    """一个几何部件的元数据（SI 单位，供报告与工具查询）。"""

    role: str                    # fuselage / wing / htail / vtail / vtail_pair / canard
    name: str
    geom_id: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    control_surfaces: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "name": self.name,
            "geom_id": self.geom_id,
            "params": dict(self.params),
            "control_surfaces": list(self.control_surfaces),
        }


class VSPBridge:
    """OpenVSP 建模 / VSPAERO 求解桥接器。

    典型用法::

        bridge = VSPBridge(logger=logger)
        bridge.build_geometry(geometry_spec)   # SI dict，见 designer
        bridge.save_vsp3(model_path)
        bridge.prepare_vspaero_geometry(model_path)
        result = bridge.run_vspaero(model_path, out_dir, ref=..., flow=..., ...)
        polar = bridge.parse_polar(result["polar_path"])
    """

    def __init__(self, openvsp_root: str | None = None,
                 vspaero_exe: str | None = None,
                 logger: Any = None,
                 timeout_s: float = 300.0):
        self.openvsp_root = openvsp_root
        self.vspaero_exe = vspaero_exe
        self.logger = logger
        self.timeout_s = float(timeout_s)
        self.components: list[Component] = []
        self.airfoil_cache_dir = Path(tempfile.gettempdir()) / "openvsp_ai_airfoils"
        self.vsp3_path: str | None = None
        self._vsp: Any = None

    # ------------------------------------------------------------------ 基础
    @property
    def vsp(self) -> Any:
        if self._vsp is None:
            self._vsp = ensure_openvsp(self.openvsp_root)
        return self._vsp

    @property
    def root(self) -> str:
        root = self.openvsp_root or find_openvsp_root()
        if not root:
            root = os.environ.get("OPENVSP_ROOT", "")
        if not root:
            raise VSPError("未找到 OpenVSP 安装目录")
        return root

    @property
    def vspaero_path(self) -> str:
        if self.vspaero_exe and Path(self.vspaero_exe).is_file():
            return str(Path(self.vspaero_exe))
        candidate = Path(self.root) / "vspaero.exe"
        if not candidate.is_file():
            raise VSPError(f"未找到 vspaero.exe: {candidate}")
        return str(candidate)

    def _log(self, level: str, message: object) -> None:
        if self.logger is not None:
            getattr(self.logger, level, self.logger.info)(message)

    def version(self) -> str:
        try:
            return str(self.vsp.GetVSPVersion())
        except Exception:
            return "unknown"

    # ------------------------------------------------------------------ 内部
    def _sp(self, geom_id: str, name: str, group: str, value: float,
            required: bool = False) -> str | None:
        """设置几何参数（安全包装，自动换算 XForm 相对参数名）。"""
        vsp = self.vsp
        parm_name = name
        if group == "XForm" and name in ("X_Location", "Y_Location", "Z_Location"):
            parm_name = name.replace("_Location", "_Rel_Location")
        elif group == "XForm" and name in ("X_Rotation", "Y_Rotation", "Z_Rotation"):
            parm_name = name.replace("_Rotation", "_Rel_Rotation")
        parm_id = vsp.GetParm(geom_id, parm_name, group)
        if not parm_id:
            parm_id = vsp.GetParm(geom_id, name, group)
        if not parm_id:
            if required:
                raise VSPError(f"找不到参数 {group}/{name}（geom={geom_id}）")
            return None
        vsp.SetParmVal(parm_id, float(value))
        return parm_id

    def _xsec_parm(self, xsec_id: Any, name: str, value: float,
                   required: bool = False) -> bool:
        vsp = self.vsp
        parm_id = vsp.GetXSecParm(xsec_id, name)
        if not parm_id:
            if required:
                raise VSPError(f"找不到翼型截面参数 {name}")
            return False
        vsp.SetParmVal(parm_id, float(value))
        return True

    def _set_airfoil(self, geom_id: str, xsec_index: int, airfoil: str) -> None:
        """设置翼型：优先使用本地 Profili 库坐标（FileAirfoil），否则 NACA 参数化。"""
        vsp = self.vsp
        xss = vsp.GetXSecSurf(geom_id, 0)
        name = str(airfoil or "NACA2412").strip()
        try:
            from .airfoils import find_airfoil, write_selig_dat
            item = find_airfoil(name)
            compact = name.upper().replace(" ", "").replace("-", "")
            param_naca = compact.startswith("NACA") and compact[4:].isdigit()
            if item is not None and not param_naca:
                dat_path = write_selig_dat(item["name"], self.airfoil_cache_dir)
                vsp.ChangeXSecShape(xss, xsec_index, vsp.XS_FILE_AIRFOIL)
                xsec = vsp.GetXSec(xss, xsec_index)
                vsp.ReadFileAirfoil(xsec, dat_path)
                self._log("info", f"翼型 {item['name']} 已从本地库导入 OpenVSP: {dat_path}")
                return
        except Exception as exc:
            self._log("warn", f"导入本地翼型 {name!r} 失败，回退 NACA 参数化: {exc}")
        try:
            vsp.ChangeXSecShape(xss, xsec_index, vsp.XS_FOUR_SERIES)
        except Exception:
            return
        xsec = vsp.GetXSec(xss, xsec_index)
        text = str(airfoil or "NACA2412").upper().replace("NACA", "").strip()
        camber, camber_loc, thickness = 0.02, 0.40, 0.12
        digits = "".join(ch for ch in text if ch.isdigit())
        try:
            if len(digits) >= 4:
                thickness = int(digits[-2:]) / 100.0
                camber = int(digits[0]) / 100.0
                camber_loc = int(digits[1]) / 10.0 if int(digits[1]) > 0 else 0.4
            if len(digits) == 5:
                thickness = int(digits[-2:]) / 100.0
                camber, camber_loc = 0.02, 0.30
        except (ValueError, IndexError):
            pass
        camber = min(max(camber, 0.0), 0.09)
        thickness = min(max(thickness, 0.04), 0.30)
        self._xsec_parm(xsec, "Camber", camber)
        self._xsec_parm(xsec, "CamberLoc", camber_loc)
        self._xsec_parm(xsec, "ThickChord", thickness)

    def _begin_model(self) -> None:
        vsp = self.vsp
        vsp.ClearVSPModel()
        vsp.VSPCheckSetup()
        self.components = []

    def _register(self, component: Component) -> Component:
        self.components.append(component)
        return component

    def _find_component(self, name_or_role: str) -> Component:
        query = (name_or_role or "").strip()
        for component in self.components:
            if component.name == query or component.role == query:
                return component
        lowered = query.lower()
        for component in self.components:
            if component.name.lower() == lowered or component.role.lower() == lowered:
                return component
        raise VSPError(f"未找到几何部件: {name_or_role!r}；现有: "
                       f"{[c.name for c in self.components]}")

    # ------------------------------------------------------------------ 原语
    def add_fuselage(self, name: str = "Fuselage", length_m: float = 2.4,
                     max_diameter_m: float = 0.24, x_m: float = 0.0,
                     z_m: float = 0.0, stations: list[tuple[float, float, float, float]] | None = None,
                     width_ratio: float = 1.0, section: str = "ellipse",
                     section_exponent: float = 2.0) -> Component:
        """构建机身：沿 X 轴、若干椭圆截面放样。"""
        vsp = self.vsp
        if self._vsp is None:
            raise VSPError("请先调用 ensure_openvsp()")
        fid = vsp.AddGeom("FUSELAGE", "")
        vsp.SetGeomName(fid, name)
        length_ft = float(length_m) * M_TO_FT
        diameter_ft = float(max_diameter_m) * M_TO_FT
        self._sp(fid, "Length", "Design", length_ft)
        self._sp(fid, "X_Location", "XForm", float(x_m) * M_TO_FT)
        self._sp(fid, "Y_Location", "XForm", 0.0)
        self._sp(fid, "Z_Location", "XForm", float(z_m) * M_TO_FT)

        # 首/尾截面保留极小非零椭圆，避免退化面导致 OpenVSP 三角化报错
        stations = stations or [
            (0.00, 0.04, 0.03, 0.01),
            (0.10, 0.65, 0.62, 0.01),
            (0.33, 1.00, 0.92, 0.00),
            (0.72, 0.72, 0.66, -0.01),
            (1.00, 0.16, 0.12, 0.00),
        ]
        xss = vsp.GetXSecSurf(fid, 0)
        count = vsp.GetNumXSec(xss)
        # 补足截面数量（默认机身截面较少）
        guard = 0
        while count < len(stations) and guard < 20:
            try:
                # 在倒数第二个截面之前插入；OpenVSP 对“末尾索引”插入是 no-op。
                vsp.InsertXSec(fid, max(1, count - 2), vsp.XS_ELLIPSE)
            except Exception as exc:
                self._log("warn", f"补充机身截面失败: {exc}")
                break
            vsp.Update()
            count = vsp.GetNumXSec(vsp.GetXSecSurf(fid, 0))
            guard += 1
        xss = vsp.GetXSecSurf(fid, 0)
        count = vsp.GetNumXSec(xss)
        used = stations[:count]
        for index, station in enumerate(used):
            x_percent, width_factor, height_factor = station[0], station[1], station[2]
            z_percent = station[3] if len(station) > 3 else 0.0
            try:
                if section == "superellipse":
                    vsp.ChangeXSecShape(xss, index, vsp.XS_SUPER_ELLIPSE)
                else:
                    vsp.ChangeXSecShape(xss, index, vsp.XS_ELLIPSE)
            except Exception:
                pass
            xsec = vsp.GetXSec(xss, index)
            self._xsec_parm(xsec, "XLocPercent", float(x_percent))
            self._xsec_parm(xsec, "ZLocPercent", float(z_percent))
            if section == "superellipse":
                self._xsec_parm(xsec, "Super_Width", diameter_ft * float(width_factor)
                                * float(width_ratio))
                self._xsec_parm(xsec, "Super_Height", diameter_ft * float(height_factor))
                self._xsec_parm(xsec, "Super_M", float(section_exponent))
                self._xsec_parm(xsec, "Super_N", float(section_exponent))
                self._xsec_parm(xsec, "Super_M_bot", float(section_exponent))
                self._xsec_parm(xsec, "Super_N_bot", float(section_exponent))
                self._xsec_parm(xsec, "Super_MaxWidthLoc", 0.0)
            else:
                self._xsec_parm(xsec, "Ellipse_Width", diameter_ft * float(width_factor)
                                * float(width_ratio))
                self._xsec_parm(xsec, "Ellipse_Height", diameter_ft * float(height_factor))
        # 首页/尾页统一圆形端盖（OpenVSP EndCap 组）。
        for cap_name in ("CapUMinOption", "CapUMaxOption"):
            try:
                cap_parm = vsp.GetParm(fid, cap_name, "EndCap")
                if cap_parm:
                    vsp.SetParmVal(cap_parm, float(vsp.ROUND_END_CAP))
            except Exception:
                pass
        try:
            cap_tess = vsp.GetParm(fid, "CapUMinTess", "EndCap")
            if cap_tess:
                vsp.SetParmVal(cap_tess, 7.0)
        except Exception:
            pass
        vsp.Update()
        params = {
            "length_m": float(length_m),
            "max_diameter_m": float(max_diameter_m),
            "x_le_m": float(x_m),
            "z_m": float(z_m),
            "stations": [[round(float(v), 4) for v in s] for s in used],
            "section": section,
            "section_exponent": float(section_exponent),
            "end_caps": "round",
        }
        component = Component("fuselage", name, fid, params)
        self._register(component)
        self._log("info", f"机身 {name}: 长 {length_m:.2f} m，最大直径 "
                           f"{max_diameter_m:.3f} m")
        return component

    def add_wing(self, name: str, span_m: float, root_chord_m: float,
                 tip_chord_m: float, sweep_deg: float = 0.0,
                 dihedral_deg: float = 0.0, twist_deg: float = 0.0,
                 x_le_m: float = 0.0, y_m: float = 0.0, z_m: float = 0.0,
                 airfoil: str = "NACA2412", airfoil_tip: str | None = None,
                 symmetry: bool = True, role: str = "wing",
                 sweep_location: float = 0.25, root_y_m: float = 0.0,
                 full_span_m: float | None = None,
                 full_area_m2: float | None = None,
                 full_root_chord_m: float | None = None) -> Component:
        """构建梯形机翼/尾面。

        ``span_m`` 在 ``symmetry=True`` 时表示全展长，否则表示单侧展长；
        ``x_le_m`` 为翼根前缘 X 坐标（机头为 0，向后为正）。
        """
        vsp = self.vsp
        wid = vsp.AddGeom("WING", "")
        vsp.SetGeomName(wid, name)
        self._sp(wid, "Sym_Planar_Flag", "Sym", 2 if symmetry else 0)
        root_y = max(float(root_y_m or 0.0), 0.0)
        self._sp(wid, "X_Location", "XForm", float(x_le_m) * M_TO_FT)
        self._sp(wid, "Y_Location", "XForm", (float(y_m) + root_y) * M_TO_FT)
        self._sp(wid, "Z_Location", "XForm", float(z_m) * M_TO_FT)

        semi_span_m = ((float(span_m) / 2.0 - root_y) if symmetry
                       else float(span_m))
        semi_span_m = max(semi_span_m, 0.01)
        semi_span_ft = semi_span_m * M_TO_FT
        xss = vsp.GetXSecSurf(wid, 0)
        xsec = vsp.GetXSec(xss, 1)
        self._xsec_parm(xsec, "Span", semi_span_ft, required=True)
        self._xsec_parm(xsec, "Root_Chord", float(root_chord_m) * M_TO_FT, required=True)
        self._xsec_parm(xsec, "Tip_Chord", float(tip_chord_m) * M_TO_FT, required=True)
        self._xsec_parm(xsec, "Sweep", float(sweep_deg))
        self._xsec_parm(xsec, "Sweep_Location", float(sweep_location))
        self._xsec_parm(xsec, "Dihedral", float(dihedral_deg))
        self._xsec_parm(xsec, "Twist", float(twist_deg))
        self._set_airfoil(wid, 0, airfoil)
        self._set_airfoil(wid, 1, airfoil_tip or airfoil)
        vsp.Update()

        exposed_span = 2.0 * semi_span_m if symmetry else float(span_m)
        total_span = (float(full_span_m) if full_span_m is not None
                      else (float(span_m) if symmetry else 2 * float(span_m)))
        taper = float(tip_chord_m) / float(root_chord_m) if root_chord_m else 0.0
        area = 0.5 * (float(root_chord_m) + float(tip_chord_m)) * exposed_span
        full_area = (float(full_area_m2) if full_area_m2 is not None
                     else 0.5 * (float(full_root_chord_m or root_chord_m) +
                                 float(tip_chord_m)) * total_span)
        mac = (2.0 / 3.0) * float(root_chord_m) * (
            (1 + taper + taper ** 2) / (1 + taper)) if (1 + taper) else root_chord_m
        params = {
            "span_m": total_span,
            "exposed_span_m": exposed_span,
            "root_y_m": root_y,
            "root_chord_m": float(root_chord_m),
            "full_root_chord_m": float(full_root_chord_m or root_chord_m),
            "tip_chord_m": float(tip_chord_m),
            "area_m2": area,
            "full_area_m2": full_area,
            "mac_m": mac,
            "taper_ratio": taper,
            "sweep_deg": float(sweep_deg),
            "dihedral_deg": float(dihedral_deg),
            "twist_deg": float(twist_deg),
            "x_le_m": float(x_le_m),
            "z_m": float(z_m),
            "airfoil": airfoil,
            "symmetry": bool(symmetry),
            "aspect_ratio": (total_span ** 2 / area) if area else 0.0,
        }
        component = Component(role, name, wid, params)
        self._register(component)
        self._log("info", f"{role} {name}: 展长 {total_span:.2f} m，面积 {area:.3f} m²，"
                           f"AR {params['aspect_ratio']:.1f}，后掠 {sweep_deg:.1f}°")
        return component

    def add_vtail_pair(self, name: str = "VTail", panel_span_m: float = 0.9,
                       root_chord_m: float = 0.5, tip_chord_m: float = 0.25,
                       dihedral_deg: float = 40.0, sweep_deg: float = 20.0,
                       x_le_m: float = 2.0, z_m: float = 0.1,
                       airfoil: str = "NACA0012") -> Component:
        """构建 V 形尾翼（左右各一块面板，绕 X 轴倾斜 dihedral_deg）。"""
        vsp = self.vsp
        vid = vsp.AddGeom("WING", "")
        vsp.SetGeomName(vid, name)
        self._sp(vid, "Sym_Planar_Flag", "Sym", 2)
        self._sp(vid, "X_Location", "XForm", float(x_le_m) * M_TO_FT)
        self._sp(vid, "Y_Location", "XForm", 0.0)
        self._sp(vid, "Z_Location", "XForm", float(z_m) * M_TO_FT)
        # 翼面基准在 XY 平面，绕 X 轴旋转 (90-? ) 得到与水平面成 dihedral 的面板
        self._sp(vid, "X_Rotation", "XForm", float(dihedral_deg))
        xss = vsp.GetXSecSurf(vid, 0)
        xsec = vsp.GetXSec(xss, 1)
        self._xsec_parm(xsec, "Span", float(panel_span_m) * M_TO_FT, required=True)
        self._xsec_parm(xsec, "Root_Chord", float(root_chord_m) * M_TO_FT, required=True)
        self._xsec_parm(xsec, "Tip_Chord", float(tip_chord_m) * M_TO_FT, required=True)
        self._xsec_parm(xsec, "Sweep", float(sweep_deg))
        self._xsec_parm(xsec, "Sweep_Location", 0.25)
        self._xsec_parm(xsec, "Dihedral", 0.0)
        self._xsec_parm(xsec, "Twist", 0.0)
        self._set_airfoil(vid, 0, airfoil)
        self._set_airfoil(vid, 1, airfoil)
        vsp.Update()

        taper = float(tip_chord_m) / float(root_chord_m) if root_chord_m else 0.0
        panel_area = 0.5 * (float(root_chord_m) + float(tip_chord_m)) * float(panel_span_m)
        projected_h = 2.0 * panel_area * math.cos(math.radians(dihedral_deg))
        projected_v = 2.0 * panel_area * math.sin(math.radians(dihedral_deg))
        params = {
            "panel_span_m": float(panel_span_m),
            "root_chord_m": float(root_chord_m),
            "tip_chord_m": float(tip_chord_m),
            "panel_area_m2": panel_area,
            "total_area_m2": 2.0 * panel_area,
            "projected_horizontal_m2": projected_h,
            "projected_vertical_m2": projected_v,
            "taper_ratio": taper,
            "dihedral_deg": float(dihedral_deg),
            "sweep_deg": float(sweep_deg),
            "x_le_m": float(x_le_m),
            "z_m": float(z_m),
            "airfoil": airfoil,
            "symmetry": True,
        }
        component = Component("vtail_pair", name, vid, params)
        self._register(component)
        self._log("info", f"V 尾 {name}: 单侧展长 {panel_span_m:.2f} m，"
                           f"上反角 {dihedral_deg:.0f}°，总浸湿面积 "
                           f"{2 * panel_area:.3f} m²")
        return component

    def add_conventional_tail(self, htail_span_m: float, htail_area_m2: float,
                              vtail_span_m: float, vtail_area_m2: float,
                              x_htail_le_m: float, x_vtail_le_m: float,
                              z_htail_m: float = 0.0, z_vtail_m: float = 0.0,
                              htail_taper: float = 0.45, vtail_taper: float = 0.40,
                              htail_sweep_deg: float = 12.0,
                              vtail_sweep_deg: float = 30.0,
                              htail_airfoil: str = "NACA0012",
                              vtail_airfoil: str = "NACA0012",
                              htail_name: str = "HTail",
                              vtail_name: str = "VTail") -> tuple[Component, Component]:
        """构建常规尾翼（水平尾翼 + 垂直尾翼）。"""
        h_root = 2.0 * htail_area_m2 / (htail_span_m * (1 + htail_taper))
        h_tip = h_root * htail_taper
        htail = self.add_wing(
            name=htail_name, span_m=htail_span_m, root_chord_m=h_root,
            tip_chord_m=h_tip, sweep_deg=htail_sweep_deg, dihedral_deg=0.0,
            twist_deg=0.0, x_le_m=x_htail_le_m, z_m=z_htail_m,
            airfoil=htail_airfoil, symmetry=True, role="htail")

        v_root = 2.0 * vtail_area_m2 / (vtail_span_m * (1 + vtail_taper))
        v_tip = v_root * vtail_taper
        vtail = self._add_vertical_fin(
            name=vtail_name, span_m=vtail_span_m, root_chord_m=v_root,
            tip_chord_m=v_tip, sweep_deg=vtail_sweep_deg,
            x_le_m=x_vtail_le_m, z_m=z_vtail_m, airfoil=vtail_airfoil)
        return htail, vtail

    def _add_vertical_fin(self, name: str, span_m: float, root_chord_m: float,
                          tip_chord_m: float, sweep_deg: float = 30.0,
                          x_le_m: float = 0.0, z_m: float = 0.0,
                          airfoil: str = "NACA0012") -> Component:
        vsp = self.vsp
        vid = vsp.AddGeom("WING", "")
        vsp.SetGeomName(vid, name)
        xss = vsp.GetXSecSurf(vid, 0)
        xsec = vsp.GetXSec(xss, 1)
        self._xsec_parm(xsec, "Span", float(span_m) * M_TO_FT, required=True)
        self._xsec_parm(xsec, "Root_Chord", float(root_chord_m) * M_TO_FT, required=True)
        self._xsec_parm(xsec, "Tip_Chord", float(tip_chord_m) * M_TO_FT, required=True)
        self._xsec_parm(xsec, "Sweep", float(sweep_deg))
        self._xsec_parm(xsec, "Sweep_Location", 0.25)
        self._xsec_parm(xsec, "Dihedral", 0.0)
        self._xsec_parm(xsec, "Twist", 0.0)
        self._set_airfoil(vid, 0, airfoil)
        self._set_airfoil(vid, 1, airfoil)
        vsp.Update()
        self._sp(vid, "Sym_Planar_Flag", "Sym", 0)
        self._sp(vid, "X_Location", "XForm", float(x_le_m) * M_TO_FT)
        self._sp(vid, "Y_Location", "XForm", 0.0)
        self._sp(vid, "Z_Location", "XForm", float(z_m) * M_TO_FT)
        self._sp(vid, "X_Rotation", "XForm", 90.0)
        vsp.Update()
        taper = float(tip_chord_m) / float(root_chord_m) if root_chord_m else 0.0
        area = 0.5 * (float(root_chord_m) + float(tip_chord_m)) * float(span_m)
        params = {
            "span_m": float(span_m),
            "root_chord_m": float(root_chord_m),
            "tip_chord_m": float(tip_chord_m),
            "area_m2": area,
            "taper_ratio": taper,
            "sweep_deg": float(sweep_deg),
            "x_le_m": float(x_le_m),
            "z_m": float(z_m),
            "airfoil": airfoil,
            "symmetry": False,
        }
        component = Component("vtail", name, vid, params)
        self._register(component)
        self._log("info", f"垂尾 {name}: 展长 {span_m:.2f} m，面积 {area:.3f} m²")
        return component

    def add_control_surface(self, component_name: str, name: str,
                            u_start: float = 0.0, u_end: float = 1.0,
                            eta_start: float = 0.0, eta_end: float = 1.0,
                            chord_fraction: float = 0.25,
                            deflection_deg: float = 0.0) -> dict[str, Any]:
        """在机翼类部件后缘添加操纵面（副翼 / 升降舵 / 方向舵 / ruddervator）。

        ``u_start/u_end`` 为展向位置（0=翼根，1=翼尖）；``eta_start/eta_end``
        为弦向位置；``chord_fraction`` 为操纵面弦长占当地弦长比例。
        """
        vsp = self.vsp
        component = self._find_component(component_name)
        ss_id = vsp.AddSubSurf(component.geom_id, vsp.SS_CONTROL)
        vsp.Update()
        vsp.SetSubSurfName(ss_id, name) if hasattr(vsp, "SetSubSurfName") else None
        settings = {
            "UStart": float(u_start),
            "UEnd": float(u_end),
            "EtaStart": float(eta_start),
            "EtaEnd": float(eta_end),
            "Length_C_Start": float(chord_fraction),
            "Length_C_End": float(chord_fraction),
            "Abs_Rel_Flag": 1.0,
            "SE_Const_Flag": 1.0,
            "Surf_Type": 2.0,
            "StartAngle": float(deflection_deg),
            "EndAngle": float(deflection_deg),
            "SameAngleFlag": 1.0,
        }
        applied: dict[str, float] = {}
        for parm_name, value in settings.items():
            parm_id = vsp.GetParm(ss_id, parm_name, "SS_Control")
            if parm_id:
                vsp.SetParmVal(parm_id, float(value))
                applied[parm_name] = float(value)
        vsp.Update()
        info = {
            "name": name,
            "subsurface_id": ss_id,
            "u_start": float(u_start),
            "u_end": float(u_end),
            "eta_start": float(eta_start),
            "eta_end": float(eta_end),
            "chord_fraction": float(chord_fraction),
            "deflection_deg": float(deflection_deg),
            "params_applied": sorted(applied),
        }
        component.control_surfaces.append(info)
        self._log("info", f"操纵面 {name}: 挂在 {component.name}，"
                           f"展向 {u_start:.2f}~{u_end:.2f}，弦长比 {chord_fraction:.2f}")
        return info

    def auto_group_control_surfaces(self) -> bool:
        """把操纵面自动加入 VSPAERO 控制面组（用于控制导数计算）。"""
        vsp = self.vsp
        try:
            vsp.AutoGroupVSPAEROControlSurfaces()
            vsp.Update()
            return True
        except Exception as exc:  # pragma: no cover - API 差异
            self._log("warn", f"自动分组 VSPAERO 操纵面失败: {exc}")
            return False

    # ------------------------------------------------------------------ 查询
    def set_parameter(self, component_name: str, parm_name: str, value: float,
                      group: str | None = None) -> dict[str, Any]:
        """按参数名修改已建几何参数（例如 Span / Root_Chord / X_Rel_Location）。"""
        vsp = self.vsp
        component = self._find_component(component_name)
        geom_id = component.geom_id
        groups = [group] if group else ["XSec_1", "Design", "XForm", "Sym"]
        # 先尝试机翼截面参数
        for grp in groups:
            parm_id = vsp.GetParm(geom_id, parm_name, grp)
            if parm_id:
                vsp.SetParmVal(parm_id, float(value))
                vsp.Update()
                component.params[parm_name] = float(value)
                return {"ok": True, "component": component.name, "group": grp,
                        "parameter": parm_name, "value": float(value)}
        # 再尝试截面容器（xsec 参数）
        xss = vsp.GetXSecSurf(geom_id, 0)
        try:
            count = vsp.GetNumXSec(xss)
        except Exception:
            count = 0
        for index in range(count):
            xsec = vsp.GetXSec(xss, index)
            parm_id = vsp.GetXSecParm(xsec, parm_name)
            if parm_id:
                vsp.SetParmVal(parm_id, float(value))
                vsp.Update()
                component.params[parm_name] = float(value)
                return {"ok": True, "component": component.name, "group": f"XSec_{index}",
                        "parameter": parm_name, "value": float(value)}
        raise VSPError(f"部件 {component.name} 上找不到参数 {parm_name}")

    def get_geometry(self) -> dict[str, Any]:
        """返回当前模型的几何摘要（供工具 / 报告 / 调试使用）。"""
        components: list[dict[str, Any]] = []
        if self._vsp is not None:
            try:
                for component in self.components:
                    info = component.to_dict()
                    try:
                        bbox_min = self.vsp.GetGeomBBoxMin(component.geom_id)
                        bbox_max = self.vsp.GetGeomBBoxMax(component.geom_id)
                        info["bbox_m"] = {
                            "min": [round(v * FT_TO_M, 4) for v in
                                    (bbox_min.x(), bbox_min.y(), bbox_min.z())],
                            "max": [round(v * FT_TO_M, 4) for v in
                                    (bbox_max.x(), bbox_max.y(), bbox_max.z())],
                        }
                    except Exception:
                        info["bbox_m"] = None
                    components.append(info)
            except Exception:
                components = [c.to_dict() for c in self.components]
        totals = self._aggregate_geometry()
        return {"components": components, "totals": totals,
                "vsp3_path": self.vsp3_path, "openvsp_version": self.version()}

    def _aggregate_geometry(self) -> dict[str, Any]:
        """汇总全机几何参数（SI）。"""
        totals: dict[str, Any] = {"component_count": len(self.components),
                                  "span_m": 0.0, "wing_area_m2": 0.0,
                                  "tail_area_m2": 0.0}
        for component in self.components:
            params = component.params
            if component.role == "wing":
                totals["span_m"] = max(totals["span_m"], float(params.get("span_m", 0)))
                totals["wing_area_m2"] = float(params.get("area_m2", 0.0))
                totals["wing_mac_m"] = float(params.get("mac_m", 0.0))
                totals["aspect_ratio"] = float(params.get("aspect_ratio", 0.0))
                totals["sweep_deg"] = float(params.get("sweep_deg", 0.0))
            elif component.role in ("htail", "vtail"):
                totals["tail_area_m2"] += float(params.get("area_m2", 0.0))
            elif component.role == "vtail_pair":
                totals["tail_area_m2"] += float(params.get("total_area_m2", 0.0))
            elif component.role == "fuselage":
                totals["fuselage_length_m"] = float(params.get("length_m", 0.0))
                totals["fuselage_max_diameter_m"] = float(
                    params.get("max_diameter_m", 0.0))
        return {key: (round(value, 4) if isinstance(value, float) else value)
                for key, value in totals.items()}

    # ------------------------------------------------------------------ 读取已有模型
    def read_model_parameters(self, vsp3_path: str | Path) -> dict[str, Any]:
        """读取已有 ``.vsp3`` 的关键几何参数（SI），供 GA ``seed_model`` 作为基线参考。

        永不抛异常；返回 ``{ok, path, wing, fuselage, wings, error}``。
        """
        vsp = self.vsp
        path = Path(str(vsp3_path))
        result: dict[str, Any] = {
            "ok": False, "path": str(path), "error": None,
            "wing": None, "fuselage": {}, "wings": [],
        }
        if not path.is_file():
            result["error"] = f"seed 模型不存在: {path}"
            return result
        try:
            vsp.ClearVSPModel()
            vsp.ReadVSPFile(str(path))
            vsp.Update()
            geoms = list(vsp.FindGeoms())
            result["component_count"] = len(geoms)
            wings: list[dict[str, Any]] = []
            for gid in geoms:
                try:
                    geom_type = str(vsp.GetGeomTypeName(gid)).upper()
                except Exception:
                    continue
                if "WING" not in geom_type:
                    continue
                try:
                    name = vsp.GetGeomName(gid)
                    sym_flag = 0
                    sym_parm = vsp.GetParm(gid, "Sym_Planar_Flag", "Sym")
                    if sym_parm:
                        sym_flag = int(vsp.GetParmVal(sym_parm))
                    xss = vsp.GetXSecSurf(gid, 0)
                    count = vsp.GetNumXSec(xss)

                    def _xparm(index: int, parm_name: str,
                               scale: float = 1.0) -> float | None:
                        try:
                            xsec = vsp.GetXSec(xss, index)
                            parm_id = vsp.GetXSecParm(xsec, parm_name)
                            if not parm_id:
                                return None
                            return float(vsp.GetParmVal(parm_id)) * scale
                        except Exception:
                            return None

                    semi_span_ft = _xparm(min(1, count - 1), "Span")
                    root_ft = _xparm(min(1, count - 1), "Root_Chord")
                    tip_ft = _xparm(min(1, count - 1), "Tip_Chord")
                    sweep = _xparm(min(1, count - 1), "Sweep")
                    dihedral = _xparm(min(1, count - 1), "Dihedral")
                    if not (semi_span_ft and root_ft and tip_ft):
                        continue
                    symmetric = sym_flag == 2
                    span_m = (semi_span_ft * (2.0 if symmetric else 1.0)) * FT_TO_M
                    root_m = root_ft * FT_TO_M
                    tip_m = tip_ft * FT_TO_M
                    area_m2 = 0.5 * (root_m + tip_m) * span_m
                    x_le_m = z_m = None
                    for parm_name, key in (("X_Rel_Location", "x_le_m"),
                                           ("Z_Rel_Location", "z_m")):
                        try:
                            loc_parm = vsp.GetParm(gid, parm_name, "XForm")
                            if loc_parm:
                                value = float(vsp.GetParmVal(loc_parm)) * FT_TO_M
                                if key == "x_le_m":
                                    x_le_m = value
                                else:
                                    z_m = value
                        except Exception:
                            pass
                    wings.append({
                        "name": name,
                        "span_m": round(span_m, 4),
                        "root_chord_m": round(root_m, 4),
                        "tip_chord_m": round(tip_m, 4),
                        "area_m2": round(area_m2, 4),
                        "aspect_ratio": round(span_m ** 2 / area_m2, 3) if area_m2 else None,
                        "taper_ratio": round(tip_m / root_m, 4) if root_m else None,
                        "sweep_deg": round(float(sweep), 3) if sweep is not None else None,
                        "dihedral_deg": (round(float(dihedral), 3)
                                         if dihedral is not None else None),
                        "x_le_m": round(x_le_m, 4) if x_le_m is not None else None,
                        "z_m": round(z_m, 4) if z_m is not None else None,
                        "symmetric": symmetric,
                    })
                except Exception:
                    continue
            result["wings"] = wings
            if wings:
                main = max(wings, key=lambda item: item.get("area_m2") or 0.0)
                result["wing"] = main
                result["ok"] = True
            for gid in geoms:
                try:
                    if "FUSELAGE" not in str(vsp.GetGeomTypeName(gid)).upper():
                        continue
                    length_m = None
                    length_parm = vsp.GetParm(gid, "Length", "Design")
                    if length_parm:
                        length_m = float(vsp.GetParmVal(length_parm)) * FT_TO_M
                    max_diameter_m = 0.0
                    xss = vsp.GetXSecSurf(gid, 0)
                    for index in range(vsp.GetNumXSec(xss)):
                        xsec = vsp.GetXSec(xss, index)
                        for parm_name in ("Ellipse_Height", "Ellipse_Width"):
                            parm_id = vsp.GetXSecParm(xsec, parm_name)
                            if parm_id:
                                max_diameter_m = max(
                                    max_diameter_m,
                                    float(vsp.GetParmVal(parm_id)) * FT_TO_M)
                    result["fuselage"] = {
                        "length_m": round(length_m, 4) if length_m else None,
                        "max_diameter_m": round(max_diameter_m, 4) if max_diameter_m else None,
                    }
                    break
                except Exception:
                    continue
        except Exception as exc:  # pragma: no cover - 兼容性兜底
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                vsp.ClearVSPModel()
            except Exception:
                pass
        return result

    # ------------------------------------------------------------------ 保存 / 导出
    def configure_vspaero_thin_set(self, set_name: str = "VSPAERO_THIN") -> int | None:
        """创建 VSPAERO 薄面几何集（仅机翼/尾翼，机身不参与 VLM）。

        实测 OpenVSP 3.51.3 中 VSPAEROComputeGeometry 默认 ``ThinGeomSet=1``
        （"Shown" 集，包含机身），把机身当作薄面参与 VLM 会导致解发散
        （CL 出现数十量级、CD 为负）。把 ``ThinGeomSet`` 指向仅含翼面的
        几何集后结果恢复物理合理量级。

        返回集合索引（失败返回 None）。
        """
        vsp = self.vsp
        try:
            try:
                index = int(vsp.GetSetIndex(set_name))
            except Exception:
                index = -1
            if not index or index <= 0:
                index = 4  # 默认模型中 1=Shown, 2=Not_Shown, 3=Set_0, 4=Set_1
                vsp.SetSetName(index, set_name)
            for component in self.components:
                is_thin = component.role in ("wing", "canard", "tandem_wing", "htail", "vtail", "vtail_pair")
                try:
                    vsp.SetSetFlag(component.geom_id, index, bool(is_thin))
                except Exception:
                    pass
            vsp.Update()
            self._log("info", f"VSPAERO 薄面集 {set_name}（#{index}）: 仅机翼/尾翼参与 VLM，"
                               f"机身按厚体处理（不进入升力面网格）")
            return index
        except Exception as exc:  # pragma: no cover - 兼容性兜底
            self._log("warn", f"配置 VSPAERO 薄面集失败（将由求解器默认处理）: {exc}")
            return None

    def save_vsp3(self, path: str | Path) -> str:
        vsp = self.vsp
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        vsp.Update()
        vsp.WriteVSPFile(str(target), vsp.SET_ALL)
        self.vsp3_path = str(target)
        self._log("info", f"已保存 VSP 模型: {target}")
        return str(target)

    def export(self, path: str | Path, fmt: str = "stl",
               component_name: str | None = None) -> str:
        """导出 STL / DegenGeom CSV / 其他 OpenVSP 支持格式。"""
        vsp = self.vsp
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fmt = fmt.lower().strip().lstrip(".")
        if fmt in ("stl", "stlb", "obj", "step", "stp", "iges", "igs", "csv"):
            if component_name:
                component = self._find_component(component_name)
                geom_set = component.geom_id
            else:
                geom_set = vsp.SET_ALL
            if fmt == "csv":
                self.compute_degen_geometry(target)
                return str(target)
            export_type = {
                "stl": getattr(vsp, "EXPORT_STL", None),
                "stlb": getattr(vsp, "EXPORT_STLB", None),
                "obj": getattr(vsp, "EXPORT_OBJ", None),
                "step": getattr(vsp, "EXPORT_STEP", None),
                "stp": getattr(vsp, "EXPORT_STEP", None),
                "iges": getattr(vsp, "EXPORT_IGES", None),
                "igs": getattr(vsp, "EXPORT_IGES", None),
            }.get(fmt)
            if export_type is None:
                raise VSPError(f"不支持的导出格式: {fmt}")
            if fmt in ("stl", "stlb", "obj"):
                # 部分版本没有 STEP/IGES 导出，失败时降级
                try:
                    vsp.ExportFile(str(target), geom_set, export_type)
                    return str(target)
                except Exception:
                    pass
            vsp.ExportFile(str(target), geom_set, export_type)
            return str(target)
        raise VSPError(f"不支持的导出格式: {fmt}")

    def compute_degen_geometry(self, target_csv: str | Path | None = None) -> dict[str, Any]:
        """调用 CompGeom/DegenGeom，导出退化几何 CSV。"""
        vsp = self.vsp
        types = int(getattr(vsp, "DEGEN_GEOM_CSV_TYPE", 1))
        try:
            vsp.ComputeDegenGeom(int(vsp.SET_ALL), types)
        except Exception as exc:
            raise VSPError(f"计算退化几何失败: {exc}") from exc
        files = []
        for pattern in ("*_DegenGeom.csv", "*DegenGeom*.csv", "*.csv"):
            found = glob.glob(pattern)
            if found:
                files.extend(found)
        result = {"ok": True, "csv_candidates": sorted(set(files))}
        if target_csv is not None and files:
            target = Path(target_csv)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sorted(set(files))[-1], target)
            result["csv_path"] = str(target)
        return result

    # ------------------------------------------------------------------ VSPAERO
    def _generate_degen_subprocess(self, vsp3_path: str) -> dict[str, Any] | None:
        """独立子进程生成 VSPAERO 退化几何（推荐路径，规避 OpenVSP 进程内状态问题）。"""
        worker = Path(__file__).resolve().parent / "_degen_worker.py"
        if not worker.is_file():
            return None
        command = [sys.executable, str(worker), str(vsp3_path)]
        root = self.openvsp_root or find_openvsp_root()
        if root:
            command.append(root)
        try:
            completed = subprocess.run(
                command, cwd=str(Path(vsp3_path).parent), capture_output=True,
                encoding="utf-8", errors="replace", timeout=max(self.timeout_s, 120.0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._log("warn", f"退化几何子进程失败: {exc}")
            return None
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        payload: dict[str, Any] | None = None
        for line in stdout.splitlines():
            if line.startswith("DEGEN_RESULT "):
                try:
                    payload = json.loads(line[len("DEGEN_RESULT "):])
                except json.JSONDecodeError:
                    payload = None
        if payload is None or not payload.get("exists", {}).get("vspgeom"):
            tail = (stdout + "\n" + stderr).strip()[-2000:]
            self._log("warn", f"退化几何子进程未生成 vspgeom（returncode="
                               f"{completed.returncode}）: {tail[-800:]}")
            return None
        self._log("debug", f"退化几何生成成功（子进程）: "
                            f"{Path(payload['vspgeom']).name}")
        return payload

    def _generate_degen_inprocess(self, vsp3_path: str) -> dict[str, Any]:
        """进程内生成退化几何（子进程不可用时的兜底）。"""
        vsp = self.vsp
        vsp.ClearVSPModel()
        vsp.ReadVSPFile(str(vsp3_path))
        vsp.Update()
        vsp.SetAnalysisInputDefaults("VSPAEROComputeGeometry")
        try:
            thin_set_index = int(vsp.GetSetIndex("VSPAERO_THIN"))
            if thin_set_index > 0:
                vsp.SetIntAnalysisInput("VSPAEROComputeGeometry", "ThinGeomSet",
                                        [int(thin_set_index)])
                vsp.SetIntAnalysisInput("VSPAEROComputeGeometry", "GeomSet", [-1])
        except Exception:
            pass
        vsp.ExecAnalysis("VSPAEROComputeGeometry")
        vsp.Update()
        stem = Path(vsp3_path).stem
        folder = Path(vsp3_path).parent
        return {
            "vspgeom_path": str(folder / f"{stem}.vspgeom"),
            "vkey_path": str(folder / f"{stem}.vkey"),
            "csf_path": str(folder / f"{stem}.csf"),
            "folder": str(folder),
            "stem": stem,
        }

    def prepare_vspaero_geometry(self, vsp3_path: str | Path,
                                 prefer_subprocess: bool = True) -> dict[str, Any]:
        """读取 .vsp3 并生成 VSPAERO 所需的 ``.vspgeom`` / ``.vkey`` / ``.csf``。

        优先用独立子进程生成（实测更稳定），失败时退回同进程 API 调用。
        若模型带有 ``VSPAERO_THIN`` 几何集，则仅机翼/尾翼作为薄面参与 VLM。
        """
        vsp3_path = str(vsp3_path)
        if not Path(vsp3_path).is_file():
            raise VSPError(f"VSP3 文件不存在: {vsp3_path}")
        stem = Path(vsp3_path).stem
        folder = Path(vsp3_path).parent
        result = {
            "vspgeom_path": str(folder / f"{stem}.vspgeom"),
            "vkey_path": str(folder / f"{stem}.vkey"),
            "csf_path": str(folder / f"{stem}.csf"),
            "folder": str(folder),
            "stem": stem,
            "source": "unknown",
            "thin_set_index": None,
        }
        if prefer_subprocess:
            payload = self._generate_degen_subprocess(vsp3_path)
            if payload:
                result["source"] = "subprocess"
                result["thin_set_index"] = payload.get("thin_set_index")
                for key, value in payload.get("exists", {}).items():
                    if not value:
                        self._log("warn", f"退化几何缺少 .{key} 文件")
                self.vsp3_path = vsp3_path
                return result
            self._log("warn", "退化几何子进程不可用，改用进程内 OpenVSP API")
        fallback = self._generate_degen_inprocess(vsp3_path)
        result.update(fallback)
        result["source"] = "inprocess"
        self.vsp3_path = vsp3_path
        if not Path(result["vspgeom_path"]).is_file():
            raise VSPError(
                f"VSPAERO 几何文件生成失败: {result['vspgeom_path']}"
                "（OpenVSP 未输出退化几何，请检查 .vsp3 是否有效）")
        return result

    def _vspaero_config(self, ref: dict[str, Any], flow: dict[str, Any],
                        aoa_list: list[float], betas: list[float],
                        wake_iters: int = 3) -> str:
        """生成 VSPAERO 输入文件文本（ft / slug 单位制）。

        注意：VSPAERO 支持 ``AoA`` 与 ``Beta`` 均为逗号分隔列表，会生成
        迎角 × 侧滑角网格算例（实测 3.51.3 可用）。
        """
        sref_ft2 = float(ref["sref_m2"]) * M2_TO_FT2
        cref_ft = float(ref["cref_m"]) * M_TO_FT
        bref_ft = float(ref["bref_m"]) * M_TO_FT
        xcg_ft = float(ref.get("xcg_m", 0.0)) * M_TO_FT
        ycg_ft = float(ref.get("ycg_m", 0.0)) * M_TO_FT
        zcg_ft = float(ref.get("zcg_m", 0.0)) * M_TO_FT
        vinf_fps = float(flow["velocity_ms"]) * MS_TO_FPS
        rho = float(flow["density_kgm3"]) * KGPM3_TO_SLUGPFT3
        mach = float(flow.get("mach") or 0.0)
        if mach <= 0:
            mach = float(flow["velocity_ms"]) / max(
                float(flow.get("sound_speed_ms", 340.0)), 1e-6)
        reynolds = flow.get("reynolds")
        if reynolds is None:
            mu = 1.7894e-5
            reynolds = (float(flow["density_kgm3"]) * float(flow["velocity_ms"])
                        * float(ref["cref_m"]) / mu)
        alphas = ",".join(f"{float(a):.4f}" for a in aoa_list)
        beta_text = ",".join(f"{float(b):.4f}" for b in betas)
        lines = [
            f"Sref = {sref_ft2:.8f}",
            f"Cref = {cref_ft:.8f}",
            f"Bref = {bref_ft:.8f}",
            f"X_cg = {xcg_ft:.8f}",
            f"Y_cg = {ycg_ft:.8f}",
            f"Z_cg = {zcg_ft:.8f}",
            f"Mach = {mach:.6f}",
            f"AoA = {alphas}",
            f"Beta = {beta_text}",
            f"Vinf = {vinf_fps:.5f}",
            f"Rho = {rho:.10f}",
            f"ReCref = {float(reynolds):.6e}",
            f"WakeIters = {int(wake_iters)}",
            "VSP_StabilityType = 0",
            "",
        ]
        return "\n".join(lines)

    def _copy_vspaero_inputs(self, prep: dict[str, Any], base: str) -> None:
        """把 .vspgeom/.vkey/.csf 复制到指定基名（供独立求解轮次使用）。"""
        for ext in (".vspgeom", ".vkey", ".csf"):
            source = Path(prep["vspgeom_path"]).with_suffix(ext)
            target = Path(base + ext)
            if not source.is_file():
                continue
            if source.resolve() == target.resolve():
                continue  # 同一路径（扫掠轮基名与模型同名）无需复制
            try:
                shutil.copy2(source, target)
            except OSError as exc:
                self._log("warn", f"复制 VSPAERO 输入 {source.name} 失败: {exc}")

    def _run_solver(self, base: str, flags: list[str], out: Path,
                    n_cpu: int = 4) -> dict[str, Any]:
        command = [self.vspaero_path, "-omp", str(max(1, int(n_cpu)))] + list(flags) + [base]
        started = time.time()
        try:
            completed = subprocess.run(
                command, cwd=str(out), capture_output=True, encoding="utf-8",
                errors="replace", timeout=self.timeout_s,
            )
            elapsed = time.time() - started
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            elapsed = time.time() - started
            raise VSPError(
                f"VSPAERO 求解超时（>{self.timeout_s:.0f}s，base={base}）") from exc
        except OSError as exc:
            raise VSPError(f"无法运行 vspaero.exe: {exc}") from exc
        self._log("debug", f"vspaero[{Path(base).name}] {elapsed:.1f}s "
                            f"returncode={returncode}")
        return {
            "command": command,
            "returncode": returncode,
            "elapsed_s": round(elapsed, 2),
            "stdout_tail": stdout[-4000:],
            "stderr_tail": stderr[-2000:],
        }

    def run_vspaero(self, vsp3_path: str | Path, out_dir: str | Path,
                    ref: dict[str, Any], flow: dict[str, Any],
                    aoa_list: list[float] | None = None,
                    betas: list[float] | None = None,
                    stability: bool = True,
                    stability_alpha_deg: float | None = None,
                    wake_iters: int = 3, n_cpu: int = 4) -> dict[str, Any]:
        """运行 VSPAERO：一次扫掠求解（polar，支持迎角×侧滑角网格）+ 一次稳定性求解。

        实测 OpenVSP 3.51.3 的 ``vspaero.exe -stab`` 只写出 ``.stab``（以及
        ``.flt``），不写 ``.polar``；因此 polar 与 stability 分两轮执行，稳定性
        轮只取一个迎角算例，成本最低。

        Parameters
        ----------
        ref : {"sref_m2","cref_m","bref_m","xcg_m", ...} 参考量（SI）
        flow : {"velocity_ms","altitude_m","density_kgm3","mach"?,"sound_speed_ms"?}
        aoa_list : 迎角列表（deg）
        betas : 侧滑角列表（deg）
        stability : 是否运行稳定性导数 / 静稳定裕度
        stability_alpha_deg : 稳定性线性化迎角，默认取扫掠范围内靠中间的值

        Returns
        -------
        dict: ok / polar / stab / runs / paths
        """
        vsp3_path = str(Path(vsp3_path))
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = Path(vsp3_path).stem
        aoa_list = [float(a) for a in (aoa_list or [-4, -2, 0, 2, 4, 6, 8, 10])]
        betas = [float(b) for b in (betas or [0.0])]
        if not Path(vsp3_path).is_file():
            raise VSPError(f"VSP3 文件不存在: {vsp3_path}")

        prep = self.prepare_vspaero_geometry(vsp3_path)
        runs: list[dict[str, Any]] = []

        # ---- 第 1 轮：气动扫掠（polar） ---------------------------------
        sweep_base = str(out / stem)
        self._copy_vspaero_inputs(prep, sweep_base)
        Path(sweep_base + ".vspaero").write_text(
            self._vspaero_config(ref, flow, aoa_list, betas, wake_iters=wake_iters),
            encoding="utf-8")
        sweep_run = self._run_solver(sweep_base, [], out, n_cpu=n_cpu)
        sweep_run["base"] = sweep_base
        sweep_run["kind"] = "sweep"
        polar_path = sweep_base + ".polar"
        sweep_run["polar_path"] = str(polar_path) if Path(polar_path).is_file() else None
        runs.append(sweep_run)
        if sweep_run["returncode"] != 0 or not Path(polar_path).is_file():
            tail = (sweep_run["stdout_tail"] or "")[-3000:]
            raise VSPError(
                f"VSPAERO 扫掠求解失败（returncode={sweep_run['returncode']}）。"
                f"输出尾部:\n{tail}")
        polar = self.parse_polar(polar_path)
        if not polar["rows"]:
            raise VSPError(f"VSPAERO polar 文件无有效数据: {polar_path}")

        # ---- 第 2 轮：稳定性导数（.stab） -------------------------------
        stab_result: dict[str, Any] | None = None
        stab_path: str | None = None
        if stability:
            if stability_alpha_deg is None:
                stability_alpha_deg = float(
                    aoa_list[len(aoa_list) // 2] if aoa_list else 2.0)
            low, high = min(aoa_list), max(aoa_list)
            stability_alpha_deg = min(max(float(stability_alpha_deg), low), high)
            stab_base = str(out / f"{stem}_stability")
            self._copy_vspaero_inputs(prep, stab_base)
            Path(stab_base + ".vspaero").write_text(
                self._vspaero_config(ref, flow, [stability_alpha_deg], [0.0],
                                     wake_iters=wake_iters),
                encoding="utf-8")
            stab_run = self._run_solver(stab_base, ["-stab"], out, n_cpu=n_cpu)
            stab_run["base"] = stab_base
            stab_run["kind"] = "stability"
            runs.append(stab_run)
            candidate = stab_base + ".stab"
            if Path(candidate).is_file():
                stab_path = str(candidate)
                try:
                    stab_result = self.parse_stab(candidate)
                except Exception as exc:  # pragma: no cover - 解析容错
                    self._log("warn", f"解析 .stab 失败: {exc}")
            else:
                self._log("warn", "稳定性求解未生成 .stab 文件，静稳定裕度将用 "
                                   "polar 斜率法降级估计")
        else:
            self._log("warn", "未启用稳定性分析（-stab），静稳定裕度将用 polar 斜率法估计")

        result: dict[str, Any] = {
            "ok": True,
            "stem": stem,
            "runs": runs,
            "total_elapsed_s": round(sum(float(run["elapsed_s"]) for run in runs), 2),
            "polar_path": str(polar_path),
            "stab_path": stab_path,
            "polar": polar,
            "stab": stab_result,
            "vspgeom_path": prep["vspgeom_path"],
            "csf_path": prep["csf_path"],
        }
        return result

    @staticmethod
    def shift_moments(polar: dict[str, Any], xcg_old_m: float, xcg_new_m: float,
                      cref_m: float) -> dict[str, Any]:
        """把 polar 力矩系数从旧重心参考点平移到新重心参考点。

        ``CMy_new = CMy_old + (xcg_new - xcg_old)/Cref * CL``
        （等价于静稳定裕度变化 ``SM_new = SM_old - Δxcg/Cref``）。
        """
        delta = (float(xcg_new_m) - float(xcg_old_m)) / float(cref_m)
        rows: list[dict[str, Any]] = []
        for row in polar.get("rows", []):
            new_row = dict(row)
            lift = row.get("CLtot", row.get("CL"))
            if lift is not None and not math.isnan(lift):
                for key in ("CMy", "CMytot", "CMm"):
                    if key in new_row and new_row[key] is not None and not math.isnan(new_row[key]):
                        new_row[key] = new_row[key] + delta * lift
            rows.append(new_row)
        result = dict(polar)
        result["rows"] = rows
        result["xcg_shift_m"] = round(float(xcg_new_m) - float(xcg_old_m), 5)
        return result

    # ------------------------------------------------------------------ 解析
    @staticmethod
    def parse_polar(path: str | Path) -> dict[str, Any]:
        """解析 VSPAERO ``.polar`` 文件。

        返回 ``{"columns": [...], "rows": [{col: float}, ...]}``。
        """
        path = Path(path)
        if not path.is_file():
            raise VSPError(f"polar 文件不存在: {path}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        header: list[str] | None = None
        data_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            tokens = stripped.split()
            if header is None:
                if "Beta" in tokens and "Mach" in tokens and "AoA" in tokens:
                    header = tokens
                continue
            if tokens and _is_float(tokens[0]) and len(tokens) >= 10:
                data_lines.append(stripped)
        if header is None:
            raise VSPError(f"无法解析 polar 表头: {path}")
        # 表头可能包含重复列（Surface Integration / Wake Induced 两段），
        # 取数据行列数对应的第一段列名。
        first_row = data_lines[0].split() if data_lines else []
        ncol = len(first_row) or len(header)
        if len(header) > ncol:
            header = header[:ncol]
        columns: list[str] = []
        for name in header:
            clean = name.replace("/", "_").replace("-", "_")
            columns.append(clean)
        rows: list[dict[str, float]] = []
        for line in data_lines:
            values = line.split()
            if len(values) < len(columns):
                continue
            row: dict[str, float] = {}
            for name, value in zip(columns, values):
                row[name] = _to_float(value)
            rows.append(row)
        return {"columns": columns, "rows": rows, "path": str(path), "count": len(rows)}

    @staticmethod
    def parse_stab(path: str | Path) -> dict[str, Any]:
        """解析 VSPAERO ``.stab`` 文件（稳定性导数 / SM / Xnp / 操纵导数）。"""
        path = Path(path)
        if not path.is_file():
            raise VSPError(f"stab 文件不存在: {path}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        blocks: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        in_perturbations = False
        coef_names = {"CFx", "CFy", "CFz", "CMx", "CMy", "CMz",
                      "CL", "CD", "CS", "CMl", "CMm", "CMn"}
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("AoA_"):
                tokens = stripped.split()
                if len(tokens) >= 2 and _is_float(tokens[1]):
                    current = {"aoa_deg": _to_float(tokens[1]), "base": {},
                               "perturbations": {}, "derivatives": {},
                               "sm": None, "x_np": None}
            if stripped.startswith("Base_Aero"):
                if current is None:
                    current = {"aoa_deg": None, "base": {}, "perturbations": {},
                               "derivatives": {}, "sm": None, "x_np": None}
                tokens = stripped.split()
                current["base"] = _stab_row(tokens)
                blocks.append(current)
                in_perturbations = True
                continue
            if stripped.startswith("#"):
                in_perturbations = False
                continue
            tokens = stripped.split()
            if in_perturbations and len(tokens) >= 16:
                name = tokens[0]
                current["perturbations"][name] = _stab_row(tokens)
                continue
            if tokens and tokens[0] in coef_names and len(tokens) >= 9 and current:
                values = [_to_float(v) for v in tokens[1:9]]
                current["derivatives"][tokens[0]] = dict(zip(
                    ("total", "alpha", "beta", "p", "q", "r", "mach", "u"), values))
                continue
            if stripped.startswith("SM"):
                parts = stripped.split()
                for index, token in enumerate(parts[1:]):
                    if _is_float(token):
                        current_sm = _to_float(token)
                        if current is not None:
                            current["sm"] = current_sm
                        break
                continue
            if stripped.startswith("X_np"):
                parts = stripped.split()
                for token in parts[1:]:
                    if _is_float(token):
                        if current is not None:
                            current["x_np"] = _to_float(token)
                        break
                continue
        if not blocks:
            raise VSPError(f".stab 文件中未找到稳定性和操纵导数: {path}")
        ref: dict[str, float] = {}
        for line in lines[:60]:
            stripped = line.strip()
            for key in ("Sref_", "Cref_", "Bref_", "Xcg_", "Mach_", "Vinf_"):
                if stripped.startswith(key):
                    tokens = stripped.split()
                    if len(tokens) >= 2 and _is_float(tokens[1]):
                        ref[key.rstrip("_")] = _to_float(tokens[1])
        last = blocks[-1]
        return {
            "path": str(path),
            "reference": ref,
            "cases": blocks,
            "sm": last.get("sm"),
            "x_np": last.get("x_np"),
            "aoa_deg": last.get("aoa_deg"),
            "derivatives": last.get("derivatives", {}),
            "case_count": len(blocks),
        }

    # ------------------------------------------------------------------ 便捷
    def build_geometry(self, geometry: dict[str, Any]) -> dict[str, Any]:
        """按 designer 生成的几何字典构建整机模型。

        geometry 结构::

            {
              "configuration": "vtail" | "conventional" | "flying_wing",
              "fuselage": {"name","length_m","max_diameter_m","z_m"},
              "wing": {"name","span_m","root_chord_m","tip_chord_m","sweep_deg",
                        "dihedral_deg","twist_deg","x_le_m","z_m","airfoil"},
              "vtail": {"panel_span_m","root_chord_m","tip_chord_m","dihedral_deg",
                         "sweep_deg","x_le_m","z_m","airfoil"},
              "htail"/"vtail_conventional": {...},
              "control_surfaces": [{"component","name","u_start",...}]
            }
        """
        self._begin_model()
        fuselage = geometry.get("fuselage")
        if fuselage:
            self.add_fuselage(
                name=str(fuselage.get("name", "Fuselage")),
                length_m=float(fuselage["length_m"]),
                max_diameter_m=float(fuselage["max_diameter_m"]),
                x_m=float(fuselage.get("x_m", 0.0)),
                z_m=float(fuselage.get("z_m", 0.0)),
                width_ratio=float(fuselage.get("width_ratio", 1.0)),
                section=str(fuselage.get("section", "ellipse")),
                section_exponent=float(fuselage.get("section_exponent", 2.0)),
                stations=fuselage.get("stations"),
            )
        wing = geometry.get("wing")
        if wing:
            self.add_wing(
                name=str(wing.get("name", "MainWing")),
                span_m=float(wing["span_m"]),
                root_chord_m=float(wing.get("exposed_root_chord_m",
                                            wing["root_chord_m"])),
                tip_chord_m=float(wing["tip_chord_m"]),
                sweep_deg=float(wing.get("sweep_deg", 0.0)),
                dihedral_deg=float(wing.get("dihedral_deg", 2.0)),
                twist_deg=float(wing.get("twist_deg", 0.0)),
                x_le_m=float(wing.get("exposed_x_le_m", wing.get("x_le_m", 0.0))),
                z_m=float(wing.get("exposed_z_m", wing.get("z_m", 0.0))),
                airfoil=str(wing.get("airfoil", "NACA2412")),
                symmetry=bool(wing.get("symmetry", True)),
                role="wing",
                root_y_m=float(wing.get("root_y_m", 0.0)),
                full_span_m=float(wing.get("span_m", 0.0)),
                full_area_m2=wing.get("full_area_m2"),
                full_root_chord_m=wing.get("full_root_chord_m"),
            )
        canard = geometry.get("canard")
        if canard:
            self.add_wing(
                name=str(canard.get("name", "Canard")),
                span_m=float(canard["span_m"]),
                root_chord_m=float(canard.get("exposed_root_chord_m",
                                             canard["root_chord_m"])),
                tip_chord_m=float(canard["tip_chord_m"]),
                sweep_deg=float(canard.get("sweep_deg", 8.0)),
                dihedral_deg=float(canard.get("dihedral_deg", 0.0)),
                twist_deg=float(canard.get("twist_deg", 0.0)),
                x_le_m=float(canard.get("exposed_x_le_m", canard.get("x_le_m", 0.0))),
                z_m=float(canard.get("exposed_z_m", canard.get("z_m", 0.0))),
                airfoil=str(canard.get("airfoil", "NACA0012")),
                symmetry=True,
                role="canard",
                root_y_m=float(canard.get("root_y_m", 0.0)),
                full_span_m=float(canard.get("span_m", 0.0)),
                full_area_m2=canard.get("full_area_m2"),
                full_root_chord_m=canard.get("full_root_chord_m"),
            )
        tandem = geometry.get("tandem_wing")
        if tandem:
            self.add_wing(
                name=str(tandem.get("name", "TandemWing")),
                span_m=float(tandem["span_m"]),
                root_chord_m=float(tandem.get("exposed_root_chord_m",
                                             tandem["root_chord_m"])),
                tip_chord_m=float(tandem["tip_chord_m"]),
                sweep_deg=float(tandem.get("sweep_deg", 4.0)),
                dihedral_deg=float(tandem.get("dihedral_deg", 2.0)),
                twist_deg=float(tandem.get("twist_deg", 0.0)),
                x_le_m=float(tandem.get("exposed_x_le_m", tandem.get("x_le_m", 0.0))),
                z_m=float(tandem.get("exposed_z_m", tandem.get("z_m", 0.0))),
                airfoil=str(tandem.get("airfoil", "NACA0012")),
                symmetry=True,
                role="tandem_wing",
                root_y_m=float(tandem.get("root_y_m", 0.0)),
                full_span_m=float(tandem.get("span_m", 0.0)),
                full_area_m2=tandem.get("full_area_m2"),
                full_root_chord_m=tandem.get("full_root_chord_m"),
            )
        vtail = geometry.get("vtail")
        if vtail:
            self.add_vtail_pair(
                name=str(vtail.get("name", "VTail")),
                panel_span_m=float(vtail["panel_span_m"]),
                root_chord_m=float(vtail["root_chord_m"]),
                tip_chord_m=float(vtail["tip_chord_m"]),
                dihedral_deg=float(vtail.get("dihedral_deg", 40.0)),
                sweep_deg=float(vtail.get("sweep_deg", 20.0)),
                x_le_m=float(vtail.get("x_le_m", 0.0)),
                z_m=float(vtail.get("z_m", 0.0)),
                airfoil=str(vtail.get("airfoil", "NACA0012")),
            )
        htail = geometry.get("htail")
        vtail_conv = geometry.get("vtail_conventional")
        if htail:
            self.add_wing(
                name=str(htail.get("name", "HTail")),
                span_m=float(htail["span_m"]),
                root_chord_m=float(htail.get("exposed_root_chord_m",
                                             htail["root_chord_m"])),
                tip_chord_m=float(htail["tip_chord_m"]),
                sweep_deg=float(htail.get("sweep_deg", 12.0)),
                dihedral_deg=0.0,
                twist_deg=0.0,
                x_le_m=float(htail.get("exposed_x_le_m", htail.get("x_le_m", 0.0))),
                z_m=float(htail.get("exposed_z_m", htail.get("z_m", 0.0))),
                airfoil=str(htail.get("airfoil", "NACA0012")),
                symmetry=True,
                role="htail",
                root_y_m=float(htail.get("root_y_m", 0.0)),
                full_span_m=float(htail.get("span_m", 0.0)),
                full_area_m2=htail.get("full_area_m2"),
                full_root_chord_m=htail.get("full_root_chord_m"),
            )
        if vtail_conv:
            self._add_vertical_fin(
                name=str(vtail_conv.get("name", "VTail")),
                span_m=float(vtail_conv["span_m"]),
                root_chord_m=float(vtail_conv["root_chord_m"]),
                tip_chord_m=float(vtail_conv["tip_chord_m"]),
                sweep_deg=float(vtail_conv.get("sweep_deg", 30.0)),
                x_le_m=float(vtail_conv.get("x_le_m", 0.0)),
                z_m=float(vtail_conv.get("z_m", 0.0)),
                airfoil=str(vtail_conv.get("airfoil", "NACA0012")),
            )
        for item in geometry.get("control_surfaces", []) or []:
            try:
                self.add_control_surface(
                    component_name=str(item["component"]),
                    name=str(item.get("name", "Control")),
                    u_start=float(item.get("u_start", 0.0)),
                    u_end=float(item.get("u_end", 1.0)),
                    eta_start=float(item.get("eta_start", 0.0)),
                    eta_end=float(item.get("eta_end", 1.0)),
                    chord_fraction=float(item.get("chord_fraction", 0.25)),
                )
            except Exception as exc:
                self._log("warn", f"添加操纵面失败（忽略）: {exc}")
        if any(c.control_surfaces for c in self.components):
            self.auto_group_control_surfaces()
        self.vsp.Update()
        thin_set_index = self.configure_vspaero_thin_set()
        geometry_summary = self.get_geometry()
        geometry_summary["vspaero_thin_set_index"] = thin_set_index
        geometry_summary["vspaero_thin_set_note"] = (
            "VLM 仅计算机翼/尾翼薄面；机身作为厚体不参与升力面计算"
            if thin_set_index else "未配置薄面集，按求解器默认处理")
        return geometry_summary


def summarize_case_parameters(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """把 ``read_model_parameters`` 结果压缩为 result.json 用的案例参数摘要。"""
    if not isinstance(params, dict):
        return None
    wing = params.get("wing") if isinstance(params.get("wing"), dict) else {}
    fuselage = (params.get("fuselage")
                if isinstance(params.get("fuselage"), dict) else {})
    wings = params.get("wings") if isinstance(params.get("wings"), list) else []
    main_area = float(wing.get("area_m2") or 0.0)
    tail_areas: list[dict[str, Any]] = []
    for item in wings:
        if not isinstance(item, dict):
            continue
        area = float(item.get("area_m2") or 0.0)
        if main_area and abs(area - main_area) <= 1e-9:
            continue  # 跳过主翼本身
        tail_areas.append({
            "name": item.get("name"),
            "area_m2": item.get("area_m2"),
            "span_m": item.get("span_m"),
            "symmetric": item.get("symmetric"),
        })
    path = str(params.get("path") or "")
    return {
        "ok": bool(params.get("ok")),
        "file": Path(path).name if path else None,
        "path": path or None,
        "component_count": params.get("component_count"),
        "wing": {key: wing.get(key) for key in
                 ("span_m", "area_m2", "aspect_ratio", "taper_ratio",
                  "sweep_deg", "dihedral_deg", "x_le_m", "root_chord_m")
                 if wing.get(key) is not None},
        "fuselage": {key: fuselage.get(key) for key in
                     ("length_m", "max_diameter_m")
                     if fuselage.get(key) is not None},
        "tail_areas": tail_areas,
        "tail_area_ratio": (round(sum(float(item.get("area_m2") or 0.0)
                                      for item in tail_areas) / main_area, 4)
                            if main_area else None),
        "error": params.get("error"),
    }


# --------------------------------------------------------------------------- #
# 模块级辅助
# --------------------------------------------------------------------------- #
def _is_float(text: str) -> bool:
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def _to_float(text: str) -> float:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return float("nan")
    if math.isnan(value) or math.isinf(value):
        return float("nan")
    return value


def _stab_row(tokens: list[str]) -> dict[str, Any]:
    """解析 .stab 一行：case 名 / delta / units / 13 个系数。"""
    names = ("CFx", "CFy", "CFz", "CMx", "CMy", "CMz",
             "CL", "CD", "CS", "CMl", "CMm", "CMn")
    row: dict[str, Any] = {"name": tokens[0]}
    if len(tokens) >= 2:
        row["delta"] = _to_float(tokens[1].replace("+", ""))
    if len(tokens) >= 3:
        row["units"] = tokens[2]
    values = [_to_float(token) for token in tokens[3:16]]
    row["values"] = dict(zip(names, values))
    return row


def derive_stability_derivatives(stab: dict[str, Any]) -> dict[str, Any]:
    """从 .stab 结果中提取常用静稳定导数（1/rad 或 1/deg）。"""
    derivatives = stab.get("derivatives") or {}
    cl = derivatives.get("CL") or {}
    cm = derivatives.get("CMy") or derivatives.get("CMm") or {}
    cl_moment = derivatives.get("CMl") or {}
    cn_moment = derivatives.get("CMn") or {}
    result = {
        "cl_alpha_per_rad": cl.get("alpha"),
        "cm_alpha_per_rad": cm.get("alpha"),
        "cl_beta_per_rad": cl_moment.get("beta"),
        "cy_beta_per_rad": (derivatives.get("CFy") or {}).get("beta"),
        "cl_q": cl.get("q"),
        "cm_q": cm.get("q"),
        "cl_p": cl_moment.get("p"),
        "cn_beta_per_rad": cn_moment.get("beta"),
        "cn_r": cn_moment.get("r"),
    }
    if result["cl_alpha_per_rad"] not in (None, 0) and result["cm_alpha_per_rad"] is not None:
        result["sm_from_derivatives"] = -result["cm_alpha_per_rad"] / result["cl_alpha_per_rad"]
    return result


def summarize_polar(polar: dict[str, Any]) -> dict[str, Any]:
    """从 polar 中提取关键气动指标（CLmax、L/Dmax、CD0、零升迎角等）。"""
    rows = [row for row in polar.get("rows", []) if row.get("AoA") is not None]
    if not rows:
        return {"ok": False, "error": "polar 无有效数据"}
    valid = [row for row in rows if not math.isnan(row.get("CLtot", row.get("CL", float("nan"))))]
    if not valid:
        return {"ok": False, "error": "polar 升力系数无效"}

    def cl(row: dict[str, Any]) -> float:
        return _to_float(row.get("CLtot", row.get("CL", float("nan"))))

    def cd(row: dict[str, Any]) -> float:
        return _to_float(row.get("CDtot", row.get("CD", float("nan"))))

    def cm(row: dict[str, Any]) -> float:
        for name in ("CMytot", "CMy", "CMm"):
            value = row.get(name)
            if value is not None and not math.isnan(value):
                return value
        return float("nan")

    cd_valid = [row for row in valid if not math.isnan(cd(row))]
    ld_valid = [row for row in valid if not math.isnan(cd(row)) and abs(cd(row)) > 1e-9]
    cl_max_row = max(valid, key=cl)
    ld_max_row = max(ld_valid, key=lambda r: cl(r) / cd(r)) if ld_valid else None
    cd_min_row = min(cd_valid, key=cd) if cd_valid else None
    # 升力线斜率（对迎角线性回归）
    slope = None
    alpha0 = None
    points = [(math.radians(_to_float(row["AoA"])), cl(row)) for row in valid]
    if len(points) >= 2:
        n = len(points)
        sum_x = sum(p[0] for p in points)
        sum_y = sum(p[1] for p in points)
        sum_xx = sum(p[0] ** 2 for p in points)
        sum_xy = sum(p[0] * p[1] for p in points)
        denominator = n * sum_xx - sum_x ** 2
        if abs(denominator) > 1e-12:
            slope = (n * sum_xy - sum_x * sum_y) / denominator
            intercept = (sum_y - slope * sum_x) / n
            if abs(slope) > 1e-9:
                alpha0 = -intercept / slope
    # 静稳定裕度（polar 斜率法，作为 .stab 缺失时的降级方案）
    sm_polar = None
    cm_points = [(cl(row), cm(row)) for row in valid if not math.isnan(cm(row))]
    if len(cm_points) >= 2:
        n = len(cm_points)
        sum_x = sum(p[0] for p in cm_points)
        sum_y = sum(p[1] for p in cm_points)
        sum_xx = sum(p[0] ** 2 for p in cm_points)
        sum_xy = sum(p[0] * p[1] for p in cm_points)
        denominator = n * sum_xx - sum_x ** 2
        if abs(denominator) > 1e-12:
            d_cm_d_cl = (n * sum_xy - sum_x * sum_y) / denominator
            sm_polar = -d_cm_d_cl
    result = {
        "ok": True,
        "count": len(valid),
        "cl_max": cl(cl_max_row),
        "alpha_at_cl_max_deg": _to_float(cl_max_row.get("AoA")),
        "cl_at_zero_deg": None,
        "cd_min": cd(cd_min_row) if cd_min_row else None,
        "alpha_at_cd_min_deg": _to_float(cd_min_row.get("AoA")) if cd_min_row else None,
        "ld_max": (cl(ld_max_row) / cd(ld_max_row)) if ld_max_row else None,
        "alpha_at_ld_max_deg": _to_float(ld_max_row.get("AoA")) if ld_max_row else None,
        "cl_alpha_per_rad": slope,
        "cl_alpha_per_deg": (slope * math.pi / 180.0) if slope is not None else None,
        "alpha_zero_lift_deg": (math.degrees(alpha0) if alpha0 is not None else None),
        "sm_polar": sm_polar,
    }
    for row in valid:
        if abs(_to_float(row.get("AoA")) - 0.0) < 1e-6:
            result["cl_at_zero_deg"] = cl(row)
            break
    if cd_min_row is not None and result["cl_at_zero_deg"] is not None:
        result["cd0_estimate"] = result["cd_min"]
    else:
        result["cd0_estimate"] = result["cd_min"]
    return result
