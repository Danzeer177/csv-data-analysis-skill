"""
CSV 数据分析 Skill — Agent Tool 注册
=====================================
将数据分析能力注册为 LangChain StructuredTool，
Host Agent 可通过 tool calling 直接触发。

使用方式:
    from agent_tool import create_csv_analysis_tool

    tool = create_csv_analysis_tool()
    agent = create_agent(tools=[tool, ...])
    agent.invoke({"messages": [{"role": "user", "content": "分析 sales.csv"}]})
"""

import os
import sys
import tempfile

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.join(_PKG_DIR, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from graph_builder import GraphAnalyzer
from auth import AuthManager, AuthError
from tracker import UsageTracker

# ============================================================================
# 工具 Schema
# ============================================================================

TOOL_NAME = "analyze_csv_data"
TOOL_DESCRIPTION = """
分析 CSV 表格数据，返回多维度统计分析和经营洞察。

支持两种输入方式:
- file_path: 本地 CSV/Excel 文件路径
- content: CSV 文本内容字符串（Agent 间传递无需落盘）

分析深度: quick(快速,≤3项分析) / standard(标准,≤7项) / deep(深度,≤10项含预测)

返回: 执行摘要、数据画像、质量评分、多维度分析结果、分级洞察、数据事实块
""".strip()


TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "CSV 或 Excel 文件的本地绝对路径。与 content 二选一"
        },
        "content": {
            "type": "string",
            "description": "CSV 文本内容字符串。Agent 间数据传递无需落盘。与 file_path 二选一"
        },
        "depth": {
            "type": "string",
            "enum": ["quick", "standard", "deep"],
            "description": "分析深度。quick=快速扫描(30s), standard=全维度(60-90s), deep=含预测(2-3min)",
            "default": "standard"
        },
        "focus_questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "用户关注的具体业务问题，如 ['为什么Q3下滑？', '哪个产品毛利最高？']"
        },
        "column_hints": {
            "type": "object",
            "description": "手动标注列语义，如 {'下单日期': 'date', '实收金额': 'revenue'}。提供后跳过自动检测"
        },
    },
    "required": []
}


# ============================================================================
# 工具实现
# ============================================================================

def _run_analysis(
    file_path: str = None,
    content: str = None,
    depth: str = "standard",
    focus_questions: list = None,
    column_hints: dict = None,
) -> str:
    """
    执行分析并返回格式化结果。

    工具函数签名需匹配 TOOL_SCHEMA 定义的参数。
    返回字符串供 Agent 直接阅读。
    """
    if file_path and content:
        return "错误: file_path 和 content 只能提供一个，不能同时指定"
    if not file_path and not content:
        return "错误: 必须提供 file_path 或 content"

    analyzer = GraphAnalyzer(interrupt_on_ambiguity=False)

    try:
        result = analyzer.run(
            file_path=file_path,
            content=content,
            depth=depth,
            focus_questions=focus_questions,
            column_hints=column_hints,
        )
    except FileNotFoundError:
        return f"错误: 文件不存在 - {file_path}"
    except ValueError as e:
        return f"错误: 数据格式问题 - {str(e)}"
    except Exception as e:
        return f"错误: 分析失败 - {str(e)}"

    if result["status"] == "error":
        return f"分析失败: {result.get('executive_summary', result.get('message', '未知错误'))}"

    # 构建 Agent 友好格式
    lines = []
    lines.append(f"## 执行摘要\n{result.get('executive_summary', '无')}")

    # 质量
    quality = result.get("quality_report") or result.get("quality", {})
    score = quality.get("score", "?")
    lines.append(f"\n## 数据质量\n评分: {score}/100")

    # 分析结果
    analyses = result.get("analysis_results") or result.get("analyses", [])
    lines.append(f"\n## 分析结果 ({len(analyses)} 项)")
    for a in analyses:
        status = "OK" if a.get("status") == "success" else a.get("status", "?")
        lines.append(f"- [{status}] {a.get('task_id', '?')}: {a.get('narration', '')[:150]}")

    # 洞察
    insights = result.get("insights", [])
    if insights:
        lines.append(f"\n## 关键发现 ({len(insights)} 条)")
        for ins in insights:
            sev = {"critical": "[严重]", "warning": "[警告]", "info": "[信息]"}.get(
                ins.get("severity"), ""
            )
            lines.append(f"- {sev} {ins.get('title', '')}")

    # 数据事实
    lines.append(f"\n## 数据事实\n{result.get('data_facts', '')}")

    return "\n".join(lines)


# ============================================================================
# 工厂函数
# ============================================================================

def create_csv_analysis_tool():
    """
    创建 LangChain StructuredTool 实例。

    需要: pip install langchain-core

    Returns:
        BaseTool: 可注册到 Agent 的工具
    """
    try:
        from langchain_core.tools import StructuredTool
    except ImportError:
        raise ImportError("需要安装 langchain-core: pip install langchain-core")

    return StructuredTool.from_function(
        name=TOOL_NAME,
        description=TOOL_DESCRIPTION,
        args_schema=TOOL_SCHEMA,
        func=_run_analysis,
    )


def create_csv_analysis_tool_with_auth(auth_config_path: str = None):
    """
    创建带团队鉴权的工具。

    Args:
        auth_config_path: team.yaml 路径。默认使用 config/team.yaml

    Returns:
        工具函数（接收 api_key 参数）
    """
    config_path = auth_config_path or os.path.join(_PKG_DIR, "config", "team.yaml")
    auth = AuthManager(config_path)

    def _run_with_auth(
        api_key: str,
        file_path: str = None,
        content: str = None,
        depth: str = "standard",
        focus_questions: list = None,
        column_hints: dict = None,
    ) -> str:
        try:
            user = auth.authenticate(api_key)
            auth.authorize(user, depth)
            auth.check_rate_limit(user.tenant_id)
            auth.check_daily_quota(user)
        except AuthError as e:
            return f"鉴权失败: {str(e)}"

        result = _run_analysis(
            file_path=file_path,
            content=content,
            depth=depth,
            focus_questions=focus_questions,
            column_hints=column_hints,
        )

        auth.record_call(user, depth)
        return f"[用户: {user.user_name}, 剩余配额: ~{user.daily_limit}]\n\n{result}"

    return _run_with_auth


# ============================================================================
# 直接使用示例
# ============================================================================

if __name__ == "__main__":
    # 方式 A: 注册为 LangChain Tool
    print("=" * 60)
    print("  方式 A: 注册为 LangChain StructuredTool")
    print("=" * 60)
    tool = create_csv_analysis_tool()
    print(f"  工具名: {tool.name}")
    print(f"  描述长度: {len(tool.description)} 字符")
    print()

    # 方式 B: 直接调用函数
    print("=" * 60)
    print("  方式 B: 直接调用函数")
    print("=" * 60)
    test_csv = os.path.join(_PKG_DIR, "tests", "fixtures", "sales_2025_q4.csv")
    if os.path.exists(test_csv):
        output = _run_analysis(file_path=test_csv, depth="quick")
        print(output[:500])
    else:
        print(f"  测试文件不存在: {test_csv}")
