# -*- coding: utf-8 -*-
"""Advisory aerodynamic formulas and design rules.

These are not hard constraints.  The LLM/technical requirements are the design
authority; this registry supplies transparent estimates and trade-study inputs.
"""
from __future__ import annotations

from typing import Any

CATEGORY_LABELS = {
    "mission": "任务与总体参数",
    "wing": "机翼与增升",
    "drag": "阻力与推进",
    "tail": "尾翼与配平",
    "stability": "稳定性与飞行品质",
    "performance": "性能",
    "mass": "质量、惯量与重心",
    "aeroelastic": "气动弹性与边界",
    "uncertainty": "适用域与不确定性",
}

RULES: list[dict[str, Any]] = [
    # ---------------- mission / sizing ----------------
    {
        "id": "R-WL-01", "category": "mission", "level": "P0", "status": "implemented",
        "name": "失速速度与翼载",
        "formula": "W/S = 0.5 * rho * V_stall^2 * CL_max；V_stall = sqrt(2(W/S)/(rho CL_max))",
        "inputs": ["W", "rho", "CL_max", "V_stall"], "outputs": ["W/S"],
        "note": "只作为初始面积估算；最终 CL_max 必须以极曲线/风洞/试飞数据校核。",
        "source": "Raymer, Aircraft Design: A Conceptual Approach; Roskam, Airplane Design I",
    },
    {
        "id": "R-WL-02", "category": "mission", "level": "P0", "status": "implemented",
        "name": "巡航升力系数",
        "formula": "CL_cruise = W / (q S), q = 0.5 rho V^2",
        "inputs": ["W", "rho", "V", "S"], "outputs": ["CL_cruise"],
        "note": "检查巡航是否落在低阻区；不是硬性限制。",
        "source": "Raymer; Roskam",
    },
    {
        "id": "R-WL-03", "category": "mission", "level": "P1", "status": "partial",
        "name": "需用功率与可用功率",
        "formula": "P_req = D V / eta_p；P_avail = 电池功率/发动机功率",
        "inputs": ["D", "V", "eta_p", "P_avail"], "outputs": ["P_req", "P_margin"],
        "note": "需要真实螺旋桨/电机效率曲线。", "source": "Torenbeek; Roskam",
    },
    {
        "id": "R-RANGE-01", "category": "mission", "level": "P1", "status": "needed",
        "name": "电动/螺旋桨航时与航程",
        "formula": "E = eta_total * E_battery / P_req；R = V E；螺旋桨 Breguet 形式另算",
        "inputs": ["E_battery", "eta_total", "P_req", "V"], "outputs": ["E", "R"],
        "note": "必须扣除电池可用深度、附件功率和爬升/备份能量。",
        "source": "Raymer; Torenbeek",
    },
    # ---------------- wing ----------------
    {
        "id": "R-LIFT-01", "category": "wing", "level": "P0", "status": "partial",
        "name": "有限翼升力线斜率",
        "formula": "CL_alpha = 2*pi*AR / (2 + sqrt(AR^2 beta^2/eta^2 + 4))（DATCOM 形式）",
        "inputs": ["AR", "beta=sqrt(1-M^2)", "eta"], "outputs": ["CL_alpha"],
        "note": "低速小后掠可用简化式；大后掠/低展弦比需面元或风洞。", "source": "USAF DATCOM; Raymer",
    },
    {
        "id": "R-LIFT-02", "category": "wing", "level": "P0", "status": "partial",
        "name": "CLmax 的平面形状修正",
        "formula": "CL_max_wing ≈ CL_max_airfoil * k_planform * k_Re * k_M * k_flap",
        "inputs": ["CL_max_airfoil", "AR", "taper", "sweep", "Re", "M", "flap"], "outputs": ["CL_max_wing"],
        "note": "不可把二维翼型 CLmax 直接当成全机 CLmax。", "source": "Raymer; Abbott & von Doenhoff",
    },
    {
        "id": "R-LIFT-03", "category": "wing", "level": "P2", "status": "needed",
        "name": "后掠/上反/扭转对升力与俯仰的修正",
        "formula": "dCL/dalpha、dCm/dalpha、CL_beta 由几何/面元数据回归；避免用手算常数代替",
        "inputs": ["sweep", "dihedral", "twist", "AR"], "outputs": ["derivatives"],
        "note": "需要 VSPAERO 或 DATCOM 数据支持。", "source": "DATCOM; Roskam",
    },
    {
        "id": "R-DRAG-01", "category": "drag", "level": "P0", "status": "implemented",
        "name": "诱导阻力与 Oswald 效率",
        "formula": "CD_i = CL^2/(pi AR e)；CD = CD0 + CD_i",
        "inputs": ["CL", "AR", "e", "CD0"], "outputs": ["CD_i", "L/D"],
        "note": "e 需从极曲线拟合；低速大 AR 可用 0.7~0.9 作初值。", "source": "Raymer; Hoerner",
    },
    {
        "id": "R-DRAG-02", "category": "drag", "level": "P1", "status": "needed",
        "name": "部件阻力积累（CD0 buildup）",
        "formula": "CD0 = Σ(Cf_i * S_wet_i * FF_i * IF_i) / S_ref + CD_misc",
        "inputs": ["部件湿面积", "Cf", "形状因子 FF", "干扰因子 IF", "S_ref"], "outputs": ["CD0"],
        "note": "至少覆盖机身、机翼、尾翼、起落架、附件；需要湿面积/干扰因子数据。", "source": "Hoerner, Fluid-Dynamic Drag; Raymer",
    },
    {
        "id": "R-DRAG-03", "category": "drag", "level": "P1", "status": "needed",
        "name": "摩擦系数与雷诺数",
        "formula": "Cf = 0.455/(log10(Re))^2.58 - 1700/Re（Prandtl-Schlichting，湍流）",
        "inputs": ["Re", "粗糙度"], "outputs": ["Cf"],
        "note": "层流/转捩位置、表面粗糙度需说明；不能默认全湍流或全层流。", "source": "Hoerner; Schlichting",
    },
    {
        "id": "R-DRAG-04", "category": "drag", "level": "P1", "status": "needed",
        "name": "配平阻力与爬升阻力",
        "formula": "CD_trim = CD0 + CD_i + CD_control + CD_misc；爬升需加 2D/(rho V^2 S)",
        "inputs": ["CD", "CL_trim", "control_deflection"], "outputs": ["CD_trim"],
        "note": "没有真实配平就不能把极曲线阻力当作任务阻力。", "source": "Roskam; Torenbeek",
    },
    {
        "id": "R-DRAG-05", "category": "drag", "level": "P1", "status": "needed",
        "name": "压缩性阻力上升（低速机可略）",
        "formula": "M_crit、CD_wave 近似；或 Prandtl-Glauert/Korn 修正",
        "inputs": ["M", "M_crit", "sweep", "airfoil"], "outputs": ["CD_compress"],
        "note": "仅在设计 Mach > 0.3 时启用；低速无人机可标注不适用。", "source": "DATCOM; Raymer",
    },
    # ---------------- tail ----------------
    {
        "id": "R-TAIL-01", "category": "tail", "level": "P0", "status": "implemented",
        "name": "尾容量系数 Vh / Vv",
        "formula": "Vh = S_h l_h /(S c)；Vv = S_v l_v /(S b)",
        "inputs": ["S_h", "S_v", "l_h", "l_v", "S", "c", "b"], "outputs": ["Vh", "Vv"],
        "note": "初值参考 0.4~0.8 / 0.03~0.06；只是起点，不是合格判据。", "source": "Raymer; Roskam; NASA NTRS 19920012297",
    },
    {
        "id": "R-TAIL-02", "category": "tail", "level": "P0", "status": "implemented",
        "name": "静稳定裕度与中性点",
        "formula": "SM = (x_NP - x_CG)/c；x_NP 由 Cm_alpha 拟合或面元/DATCOM 给出",
        "inputs": ["x_NP", "x_CG", "c"], "outputs": ["SM"],
        "note": "SM 目标由用户/任务决定；不得用固定 10% 作为唯一硬规则。", "source": "Roskam; DATCOM",
    },
    {
        "id": "R-TAIL-03", "category": "tail", "level": "P1", "status": "partial",
        "name": "平尾/垂尾面积初估",
        "formula": "S_h = Vh S c / l_h；S_v = Vv S b / l_v",
        "inputs": ["Vh", "Vv", "S", "c", "b", "l_h", "l_v"], "outputs": ["S_h", "S_v"],
        "note": "双垂尾时每片面积按总目标分配；需校核尾翼失速和平尾下洗。", "source": "Raymer; Roskam",
    },
    {
        "id": "R-TAIL-04", "category": "tail", "level": "P1", "status": "needed",
        "name": "尾翼失速与平尾下洗/动压损失",
        "formula": "CL_h = CL_h_alpha(alpha + i_h - epsilon - alpha_0)；q_h/q = eta_h",
        "inputs": ["downwash epsilon", "eta_h", "tail incidence"], "outputs": ["CL_h_available"],
        "note": "尾翼失速、T 尾深失速、双垂尾遮蔽需要专门规则/风洞数据。", "source": "DATCOM; Roskam",
    },
    {
        "id": "R-TAIL-05", "category": "tail", "level": "P1", "status": "needed",
        "name": "双垂尾/多垂尾有效面积与遮蔽",
        "formula": "S_v_eff = k_shadow * Σ S_v_i；k_shadow 由间距、平尾位置、侧滑角决定",
        "inputs": ["间距", "面积", "平尾/机身遮蔽", "beta"], "outputs": ["k_shadow"],
        "note": "不能把两片面积简单相加为无干扰有效面积。", "source": "DATCOM; Hoerner",
    },
    # ---------------- stability / flying qualities ----------------
    {
        "id": "R-STAB-01", "category": "stability", "level": "P0", "status": "partial",
        "name": "俯仰静稳定 CM_alpha / Cn_beta / Cy_beta",
        "formula": "由 VSPAERO 稳定性导数或 DATCOM 给出；按符号和量级校核",
        "inputs": ["VSPAERO .stab", "geometry", "CG"], "outputs": ["derivatives"],
        "note": "导数必须来自同一坐标/参考点；不能从不同工具混拼。", "source": "DATCOM; Roskam",
    },
    {
        "id": "R-STAB-02", "category": "stability", "level": "P0", "status": "partial",
        "name": "纵向短周期/长周期",
        "formula": "由 Cm_q、Cm_alpha、Cz_alpha、惯性矩构成 2x2 状态矩阵",
        "inputs": ["longitudinal derivatives", "Iyy", "mass", "flight condition"], "outputs": ["wn_sp", "zeta_sp", "period_phugoid"],
        "note": "当前为二自由度估算；需说明不含弹性/非线性。", "source": "Roskam; MIL-F-8785C",
    },
    {
        "id": "R-STAB-03", "category": "stability", "level": "P0", "status": "partial",
        "name": "横向 Dutch roll / 滚转 / 螺旋",
        "formula": "由 Cy_beta、Cn_beta、Cl_beta、Cl_p、Cn_r 等构成 4 自由度系统",
        "inputs": ["lateral derivatives", "Ixx/Izz", "mass"], "outputs": ["wn_dr", "zeta_dr", "roll_tau", "spiral"],
        "note": "简化模式判据只能作筛选，不能替代 flying qualities 评估。", "source": "MIL-F-8785C; Roskam",
    },
    {
        "id": "R-STAB-04", "category": "stability", "level": "P1", "status": "needed",
        "name": "操纵面效能/操纵功率",
        "formula": "Cl_delta_a、Cm_delta_e、Cn_delta_r；按舵面面积/尾臂/动压估算",
        "inputs": ["control area", "tail arm", "q", "deflection"], "outputs": ["control power"],
        "note": "当前只有粗略经验值；需要舵面几何、铰链力矩和限位。", "source": "DATCOM; Roskam",
    },
    {
        "id": "R-STAB-05", "category": "stability", "level": "P1", "status": "needed",
        "name": "配平与重心包线",
        "formula": "ΣM_CG=0；按装载/燃油/载荷组合求 CG 包线并复算配平",
        "inputs": ["component masses", "arm", "payload/fuel"], "outputs": ["CG envelope", "trim state"],
        "note": "没有质量布置和配平，不得宣称静稳定或任务性能。", "source": "Roskam; Torenbeek",
    },
    # ---------------- performance ----------------
    {
        "id": "R-PERF-01", "category": "performance", "level": "P1", "status": "needed",
        "name": "起飞/着陆滑跑距离",
        "formula": "S_g ≈ V_LOF^2/(2 a_mean) + 空中段；a_mean = g(T/W - mu - CD/mu CL)",
        "inputs": ["T/W", "W/S", "CL", "mu", "rho", "V"], "outputs": ["takeoff/landing distance"],
        "note": "需要推力/升力/阻力随速度变化与刹车/反推假设。", "source": "Raymer; Roskam",
    },
    {
        "id": "R-PERF-02", "category": "performance", "level": "P1", "status": "needed",
        "name": "爬升率与升限",
        "formula": "ROC = (P_avail - P_req)/W；绝对升限由 ROC→0 求",
        "inputs": ["P_avail", "P_req", "W"], "outputs": ["ROC", "ceiling"],
        "note": "需按高度更新 rho/P_avail，不能只用海平面值。", "source": "Torenbeek; Raymer",
    },
    {
        "id": "R-PERF-03", "category": "performance", "level": "P1", "status": "needed",
        "name": "盘旋/过载与失速边界",
        "formula": "n = 1/cos(phi)；turn radius R = V^2/(g sqrt(n^2-1))；受 CLmax 限制",
        "inputs": ["V", "phi", "CLmax", "W/S"], "outputs": ["turn rate/radius", "n_limit"],
        "note": "结构限制过载和升力限制需同时检查。", "source": "Raymer; Roskam",
    },
    {
        "id": "R-PERF-04", "category": "performance", "level": "P2", "status": "needed",
        "name": "起飞/着陆能量与刹车",
        "formula": "E = 0.5 m V^2 + 势能；刹车距离/热负荷估算",
        "inputs": ["mass", "V", "brake force"], "outputs": ["energy", "brake margin"],
        "note": "需要起落架/刹车数据；概念阶段可标记未评估。", "source": "Torenbeek",
    },
    # ---------------- mass / inertia ----------------
    {
        "id": "R-MASS-01", "category": "mass", "level": "P1", "status": "needed",
        "name": "部件质量与空重估算",
        "formula": "m_empty = Σ k_i * 几何/载荷指标；或用 Raymer/Roskam 统计式",
        "inputs": ["geometry", "material", "load", "statistical coefficients"], "outputs": ["component mass", "empty mass"],
        "note": "当前质量仅作粗略质量/惯量估算；不能作为结构设计结论。", "source": "Raymer; Roskam",
    },
    {
        "id": "R-MASS-02", "category": "mass", "level": "P1", "status": "partial",
        "name": "重心与转动惯量",
        "formula": "x_CG=Σm_i x_i/Σm_i；I_xx/I_yy/I_zz=Σ(m_i r_i^2 + I_i)",
        "inputs": ["component masses", "locations", "radii of gyration"], "outputs": ["CG", "inertia"],
        "note": "质量布置不确定度应进入稳定性和动力学计算。", "source": "Roskam; Torenbeek",
    },
    # ---------------- propulsion / aeroelastic ----------------
    {
        "id": "R-PROP-01", "category": "drag", "level": "P1", "status": "needed",
        "name": "螺旋桨推力/功率与前进比",
        "formula": "J=V/(nD)；T=C_T rho n^2 D^4；P=C_P rho n^3 D^5；eta=J C_T/C_P",
        "inputs": ["V", "n", "D", "C_T/C_P map"], "outputs": ["T", "P", "eta"],
        "note": "必须有螺旋桨 map；不能用常数效率替代全包线。", "source": "Torenbeek; Raymer",
    },
    {
        "id": "R-AE-01", "category": "aeroelastic", "level": "P2", "status": "needed",
        "name": "颤振/发散边界与速度裕度",
        "formula": "V_D >= k * V_H；需结构/气动耦合分析；概念阶段只做筛查",
        "inputs": ["stiffness", "mass distribution", "flutter speed"], "outputs": ["V_D margin"],
        "note": "不能由 VSPAERO 静气动结果直接给颤振结论。", "source": "Roskam; airworthiness practice",
    },
    {
        "id": "R-UNC-01", "category": "uncertainty", "level": "P0", "status": "needed",
        "name": "适用域与不确定性声明",
        "formula": "记录 Re/Mach/线化假设/网格/尾翼薄面/未建模项，并给敏感性和容差",
        "inputs": ["run metadata", "VLM assumptions"], "outputs": ["applicability", "uncertainty"],
        "note": "没有适用域/证据分级，不得把 VLM 结果作为工程合格判据。", "source": "开发方案 OVSP-RD-SDP-001; ATP",
    },
]


def list_rules(category: str | None = None, level: str | None = None,
               query: str | None = None) -> dict[str, Any]:
    """Return advisory rules filtered by category/level/keyword."""
    items = list(RULES)
    if category:
        items = [r for r in items if r["category"] == category]
    if level:
        items = [r for r in items if r["level"] == level.upper()]
    if query:
        needle = query.lower()
        items = [r for r in items if needle in (r["name"] + " " + r["formula"] + " " + r["id"]).lower()]
    counts: dict[str, int] = {}
    for item in RULES:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    return {
        "ok": True,
        "authority": "advisory",
        "note": "这些公式和规则只辅助技术输入与 LLM 决策，不是硬性合格判据。",
        "categories": CATEGORY_LABELS,
        "counts": counts,
        "rules": items,
    }


__all__ = ["RULES", "CATEGORY_LABELS", "list_rules"]
