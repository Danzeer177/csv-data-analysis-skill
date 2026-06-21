"""
CSV 数据分析 Agent — 关键发现报告生成器
========================================
使用 csv-data-analysis Skill 分析 CSV 文件，生成关键发现报告。

用法:
    python run_analysis.py <csv_file_path> [深度: quick|standard|deep]

示例:
    python run_analysis.py tests/fixtures/sales_2025_q4.csv standard
"""

import os
import sys
import json
import time
import io
from datetime import datetime

# 强制 UTF-8 输出，解决 Windows GBK 终端乱码
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 将 Skill 脚本目录加入路径 — 兼容源码克隆和 pip install
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.join(_PKG_DIR, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from graph_builder import GraphAnalyzer


# ============================================================================
# 报告模板
# ============================================================================

def render_report(result: dict, file_path: str, elapsed: float) -> str:
    """
    将分析结果渲染为「关键发现」报告。

    兼容 CsvAnalyzer (output.to_dict()) 和 GraphAnalyzer (直接返回 dict) 两种格式。
    """
    lines = []
    sep = "=" * 68

    # ── 标题 ──
    lines.append(sep)
    lines.append("  [CSV 数据分析] 关键发现报告  (LangGraph 编排)")
    lines.append(sep)
    lines.append(f"  文件: {os.path.basename(file_path)}")
    lines.append(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  耗时: {elapsed:.1f}s")
    lines.append(f"  状态: {result['status']}")
    lines.append("")

    if result["status"] == "error":
        lines.append(f"  [ERROR] 分析失败: {result.get('executive_summary', result.get('message', '未知错误'))}")
        return "\n".join(lines)

    # ── 1. 执行摘要 ──
    lines.append(sep)
    lines.append("  一、执行摘要")
    lines.append(sep)
    lines.append(f"  {result.get('executive_summary', '无')}")
    lines.append("")

    # ── 2. 数据画像 ──
    lines.append(sep)
    lines.append("  二、数据画像")
    lines.append(sep)
    pm = result.get("parse_meta", {})
    lines.append(f"  行数: {pm.get('row_count', '?')}    列数: {pm.get('col_count', '?')}")
    src = result.get("source_info", {})
    if src.get("file_name"):
        lines.append(f"  文件: {src['file_name']}    类型: {src.get('type', '?')}")
    # 列语义
    roles = result.get("column_roles", {})
    if roles:
        role_lines = []
        for col, info in roles.items():
            role = info if isinstance(info, str) else info.get("role", str(info))
            conf = 1.0 if isinstance(info, str) else info.get("confidence", 0)
            role_lines.append(f"{col} -> {role} ({conf:.0%})")
        lines.append(f"  列语义: {' | '.join(role_lines[:6])}")
    lines.append("")

    # ── 3. 数据质量 ──
    lines.append(sep)
    lines.append("  三、数据质量")
    lines.append(sep)
    quality = result.get("quality_report") or result.get("quality", {})
    score = quality.get("score", 0) if quality else 0
    score = int(score) if score else 0
    if score > 0:
        score_icon = "[优秀]" if score >= 90 else ("[良好]" if score >= 70 else ("[一般]" if score >= 50 else "[较差]"))
        lines.append(f"  质量评分: {score_icon} {score}/100")
    else:
        lines.append("  质量评分: 暂无")
    if quality.get("duplicate_rows", 0) > 0:
        lines.append(f"  重复行: {quality['duplicate_rows']}")
    nulls = quality.get("null_summary", {})
    high_nulls = {k: v for k, v in nulls.items() if v > 0.1}
    if high_nulls:
        lines.append(f"  高缺失列: {', '.join(f'{k}({v:.0%})' for k, v in high_nulls.items())}")
    else:
        lines.append("  缺失值: 正常范围内")
    lines.append("")

    # ── 4. 分析结果 ──
    lines.append(sep)
    lines.append("  四、分析结果")
    lines.append(sep)
    analyses = result.get("analysis_results") or result.get("analyses", [])
    for a in analyses:
        status_icon = "[OK]" if a.get("status") == "success" else ("[WARN]" if a.get("status") == "partial" else "[SKIP]" if a.get("status") == "skipped" else "[FAIL]")
        task_id = a.get("task_id", "?")
        narration = a.get("narration", "") or ""
        lines.append(f"  {status_icon} [{task_id}] {narration[:120]}")
    lines.append("")

    # ── 5. 关键发现 ──
    lines.append(sep)
    lines.append("  五、关键发现")
    lines.append(sep)
    insights = result.get("insights", [])

    # 按严重程度分组
    critical = [i for i in insights if i.get("severity") == "critical"]
    warning = [i for i in insights if i.get("severity") == "warning"]
    info = [i for i in insights if i.get("severity") == "info"]

    if not insights:
        lines.append("  无可呈现的洞察")
    else:
        # Critical
        if critical:
            lines.append("  [严重]")
            for i, ins in enumerate(critical):
                lines.append(f"     {i+1}. {ins.get('title', '')}")
                for ev in ins.get("evidence", []):
                    lines.append(f"        -> {ev}")
                lines.append(f"        => {ins.get('interpretation', '')}")
                lines.append("")

        if warning:
            lines.append("  [警告]")
            for i, ins in enumerate(warning):
                lines.append(f"     {i+1}. {ins.get('title', '')}")
                for ev in ins.get("evidence", []):
                    lines.append(f"        -> {ev}")
                lines.append(f"        => {ins.get('interpretation', '')}")
                lines.append("")

        if info:
            lines.append("  [信息]")
            for i, ins in enumerate(info):
                lines.append(f"     {i+1}. {ins.get('title', '')}")
            lines.append("")

    # ── 6. 数据事实 ──
    lines.append(sep)
    lines.append("  六、数据事实（供下游 Agent 消费）")
    lines.append(sep)
    lines.append(result.get("data_facts", "无"))
    lines.append("")
    lines.append(sep)
    lines.append("  报告结束")
    lines.append(sep)

    return "\n".join(lines)


def render_json(output: dict) -> str:
    """JSON 格式输出（供机器消费）"""
    return json.dumps(output, ensure_ascii=False, indent=2, default=str)


# ============================================================================
# 主入口
# ============================================================================

def main():
    # 解析参数
    if len(sys.argv) < 2:
        print("用法: python run_analysis.py <csv_file_path> [quick|standard|deep] [--json]")
        print("示例: python run_analysis.py data.csv standard")
        sys.exit(1)

    file_path = sys.argv[1]
    depth = sys.argv[2] if len(sys.argv) > 2 else "standard"
    output_json = "--json" in sys.argv

    # 校验文件存在
    if not os.path.exists(file_path):
        print(f"[ERROR] 文件不存在: {file_path}")
        sys.exit(1)

    # 校验深度参数
    if depth not in ("quick", "standard", "deep"):
        print(f"[WARN] 无效深度 '{depth}'，使用默认值 'standard'")
        depth = "standard"

    print(f"[分析] {file_path} (深度: {depth})")
    print(f" 安装目录: {_PKG_DIR}")
    print()

    # 使用 LangGraph 编排层执行
    analyzer = GraphAnalyzer(interrupt_on_ambiguity=False)

    start = time.time()
    result = analyzer.run(file_path=file_path, depth=depth)
    elapsed = time.time() - start

    # 输出报告
    if output_json:
        print(render_json(result))
    else:
        print(render_report(result, file_path, elapsed))

    # 返回退出码
    if result["status"] == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
