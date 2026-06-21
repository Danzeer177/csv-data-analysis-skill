"""
LangGraph 状态定义 — 8 步分析流水线的状态 Schema。

每个节点读取状态中的对应字段，处理后写入新字段。
状态单向流动，不支持回退（需回退时由 Host Agent 发起新调用）。
"""

from typing import TypedDict, List, Dict, Any, Optional, Annotated
from operator import add


class GraphState(TypedDict, total=False):
    """
    CSV 数据分析 Skill 的 LangGraph 状态。

    字段分为三组:
    1. 输入透传 — 调用时传入，全程不变
    2. 中间产出 — 各节点逐步填充
    3. 最终输出 — assemble 节点打包
    """

    # ========== 输入透传 ==========
    file_path: Optional[str]
    content: Optional[str]
    analysis_depth: str                    # quick / standard / deep
    focus_questions: Optional[List[str]]
    column_hints: Optional[Dict[str, str]]
    date_range: Optional[tuple]
    output_locale: str                     # zh / en
    # CSV 解析参数
    encoding: Optional[str]
    delimiter: Optional[str]
    has_header: bool
    skip_rows: Optional[int]
    comment_char: Optional[str]
    excel_sheet: Optional[str]

    # ========== Step 1: 数据加载 ==========
    source_info: Optional[Dict[str, Any]]
    parse_meta: Optional[Dict[str, Any]]
    raw_columns: Optional[List[str]]
    raw_shape: Optional[tuple]             # (rows, cols)

    # ========== Step 2: 结构感知 ==========
    data_profile: Optional[List[Dict]]
    column_roles: Optional[Dict[str, Dict]]
    ambiguity_report: Optional[List[Dict]]  # 低置信度列

    # ========== Step 3: 数据清洗 ==========
    quality_report: Optional[Dict[str, Any]]
    cleaning_log: Optional[List[Dict]]

    # ========== Step 4: 分析编排 ==========
    analysis_plan: Optional[List[Dict]]     # [{task_id, function, params, priority}]

    # ========== Step 5: 多维分析 ==========
    # 使用 Annotated[list, add] 实现并行节点结果合并
    analysis_results: Annotated[List[Dict], add]

    # ========== Step 6: 综合洞察 ==========
    insights: Optional[List[Dict]]

    # ========== Step 7: 聚焦回答 ==========
    answers: Optional[List[Dict]]

    # ========== 内部传递键（节点间缓存索引） ==========
    _cache_key: Optional[str]              # DataFrame 缓存索引
    current_task: Optional[Dict]           # Send API 传递的当前任务

    # ========== Step 8 / 输出 ==========
    executive_summary: Optional[str]
    data_facts: Optional[str]
    charts: Optional[List[Dict]]
    status: str                            # success / partial / error
    errors: Annotated[List[Dict], add]     # 累积非致命错误
    elapsed_seconds: Optional[float]
