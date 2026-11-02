"""独立子进程生成 VSPAERO 退化几何（.vspgeom / .vkey / .csf）。

为什么需要独立进程：实测 OpenVSP 3.51.3 Python API 在同一进程内"建模 → 保存 →
ClearVSPModel → ReadVSPFile → ExecAnalysis(VSPAEROComputeGeometry)"时，几何分析
可能静默失败（不生成 .vspgeom）；而"新进程读取 .vsp3 后运行分析"稳定成功。
因此把退化几何生成放到独立子进程，既规避该状态问题，也隔离 OpenVSP 原生崩溃。

用法（由 VSPBridge.prepare_vspaero_geometry 调用）::

    python _degen_worker.py <model.vsp3> [openvsp_root]

成功时 stdout 最后一行输出 ``DEGEN_RESULT {json}``。
"""

from __future__ import annotations

import json
import os
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: _degen_worker.py <model.vsp3> [openvsp_root]")
        return 2
    vsp3 = os.path.abspath(argv[1])
    root = argv[2] if len(argv) > 2 and argv[2] else None
    if not os.path.isfile(vsp3):
        print(f"DEGEN_ERROR 文件不存在: {vsp3}")
        return 2

    # 让子进程能 import ai_design 包
    package_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if package_parent not in sys.path:
        sys.path.insert(0, package_parent)

    try:
        from ai_design.vsp_bridge import ensure_openvsp

        vsp = ensure_openvsp(root)
        vsp.ClearVSPModel()
        vsp.ReadVSPFile(vsp3)
        vsp.Update()
        vsp.SetAnalysisInputDefaults("VSPAEROComputeGeometry")
        thin_set_index = None
        try:
            thin_set_index = int(vsp.GetSetIndex("VSPAERO_THIN"))
        except Exception:
            thin_set_index = -1
        if thin_set_index and thin_set_index > 0:
            # 仅机翼/尾翼薄面参与 VLM；机身厚体不进入升力面网格
            vsp.SetIntAnalysisInput("VSPAEROComputeGeometry", "ThinGeomSet",
                                    [int(thin_set_index)])
            vsp.SetIntAnalysisInput("VSPAEROComputeGeometry", "GeomSet", [-1])
        result_id = vsp.ExecAnalysis("VSPAEROComputeGeometry")
        vsp.Update()
        stem = os.path.splitext(os.path.basename(vsp3))[0]
        folder = os.path.dirname(vsp3)
        payload = {
            "ok": True,
            "result_id": str(result_id),
            "thin_set_index": thin_set_index if (thin_set_index or 0) > 0 else None,
            "vspgeom": os.path.join(folder, stem + ".vspgeom"),
            "vkey": os.path.join(folder, stem + ".vkey"),
            "csf": os.path.join(folder, stem + ".csf"),
            "exists": {
                "vspgeom": os.path.isfile(os.path.join(folder, stem + ".vspgeom")),
                "vkey": os.path.isfile(os.path.join(folder, stem + ".vkey")),
                "csf": os.path.isfile(os.path.join(folder, stem + ".csf")),
            },
        }
        # 可能输出到当前目录（不同版本行为差异），做一次兜底查找
        if not payload["exists"]["vspgeom"]:
            local = os.path.join(os.getcwd(), stem + ".vspgeom")
            if os.path.isfile(local):
                for ext in (".vspgeom", ".vkey", ".csf"):
                    source = os.path.join(os.getcwd(), stem + ext)
                    if os.path.isfile(source):
                        try:
                            os.replace(source, os.path.join(folder, stem + ext))
                        except OSError:
                            pass
                payload["exists"]["vspgeom"] = os.path.isfile(
                    os.path.join(folder, stem + ".vspgeom"))
        print("DEGEN_RESULT " + json.dumps(payload, ensure_ascii=True))
        return 0 if payload["exists"]["vspgeom"] else 1
    except Exception as exc:  # pragma: no cover - 子进程兜底
        print(f"DEGEN_ERROR {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
