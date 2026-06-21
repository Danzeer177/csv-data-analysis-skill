"""
LangGraph 图构建器 — 将 8 步分析流水线编排为 StateGraph。

核心特性:
    - Send API 并行扇出 Step 5 (6 个分析函数并发执行)
    - interrupt 人机协同 (低置信度列时暂停等待确认)
    - 条件路由 (根据数据特征动态选择分析维度)
    - 流式进度 (stream_mode="updates")

使用方式:
    from graph_builder import build_graph

    graph = build_graph()
    result = graph.invoke(initial_state)
"""

import os
import sys
import time
import uuid
import io
from typing import List, Dict, Any, Optional, Literal

import pandas as pd
import numpy as np

from langgraph.graph import StateGraph, START, END
try:
    from langgraph.types import Send  # LangGraph >= v1.0
except ImportError:
    from langgraph.constants import Send
from langgraph.checkpoint.memory import MemorySaver

# 兼容直接运行和包导入两种方式
try:
    from .graph_state import GraphState
    from .csv_analyzer import (
        CsvAnalyzer, AnalysisConfig, CsvDataSource,
        detect_encoding, detect_delimiter, detect_delimiter_from_file,
        ColumnRole, SubAnalysisResult, Insight, QualityReport,
    )
except ImportError:
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    from graph_state import GraphState
    from csv_analyzer import (
        CsvAnalyzer, AnalysisConfig, CsvDataSource,
        detect_encoding, detect_delimiter, detect_delimiter_from_file,
        ColumnRole, SubAnalysisResult, Insight, QualityReport,
    )


# ============================================================================
# 工具函数
# ============================================================================

def _make_df_from_state(state: GraphState) -> pd.DataFrame:
    """从 GraphState 重建 DataFrame（从 content 或 file_path）"""
    if state.get("content"):
        delimiter = state.get("delimiter") or detect_delimiter(state["content"])
        return pd.read_csv(
            io.StringIO(state["content"]),
            sep=delimiter,
            header=0 if state.get("has_header", True) else None,
            skiprows=state.get("skip_rows") or 0,
            comment=state.get("comment_char") if state.get("comment_char") else None,
        )
    elif state.get("file_path"):
        path = state["file_path"]
        encoding = state.get("encoding") or detect_encoding(path)
        delimiter = state.get("delimiter") or detect_delimiter_from_file(path, encoding)
        return pd.read_csv(
            path,
            sep=delimiter,
            encoding=encoding,
            header=0 if state.get("has_header", True) else None,
        )
    raise ValueError("state 中无 file_path 或 content")


# ============================================================================
# 节点函数 —— 每个节点对应流水线中的一步
# ============================================================================

def node_load(state: GraphState) -> GraphState:
    """
    Step 1: 数据加载与校验。

    从 file_path 或 content 加载 CSV，检测编码和分隔符，
    校验行数/列数/大小限制。结果存入 state 的 DataFrame 缓存中。
    """
    df = _make_df_from_state(state)
    if df.empty:
        return {"status": "error", "errors": [{"code": "EMPTY_DATASET", "message": "数据为空"}]}

    if len(df.columns) < 2:
        return {"status": "error", "errors": [{"code": "TOO_FEW_COLUMNS", "message": f"仅{len(df.columns)}列"}]}

    # 将 DataFrame 引用存到内部缓存（不放入 TypedDict，因其不可序列化）
    key = _ensure_cache_key(state)
    _df_cache[key] = df

    return _safe_value({
        "_cache_key": key,
        "raw_columns": list(df.columns),
        "raw_shape": (len(df), len(df.columns)),
        "parse_meta": {
            "row_count": len(df),
            "col_count": len(df.columns),
            "delimiter": state.get("delimiter", ""),
            "encoding": state.get("encoding", ""),
        },
        "source_info": {
            "type": "csv_inline" if state.get("content") else "csv_file",
            "file_name": state.get("file_path", "").split("/")[-1] if state.get("file_path") else None,
        },
    })


def node_profile(state: GraphState) -> GraphState:
    """
    Step 2: 结构感知 — 数据画像 + 列语义识别。

    对每列生成统计画像，并通过关键词匹配推断语义角色。
    低置信度列标记为 ambiguity_report。
    """
    df = _df_cache.get(_cache_key_from_state(state))
    if df is None:
        return {"status": "error", "errors": [{"code": "INTERNAL", "message": "DataFrame 缓存丢失"}]}

    config = _config_from_state(state)
    analyzer = CsvAnalyzer(config=config)
    analyzer.raw_df = df

    # 数据画像
    profiles = analyzer._build_profile()
    profile_dicts = [
        {
            "name": p.name, "dtype": p.dtype, "null_count": p.null_count,
            "null_rate": p.null_rate, "unique_count": p.unique_count,
            "min_val": p.min_val, "max_val": p.max_val, "mean_val": p.mean_val,
            "median_val": p.median_val, "std_val": p.std_val,
            "q1_val": p.q1_val, "q3_val": p.q3_val, "skew_val": p.skew_val,
            "zero_rate": p.zero_rate, "avg_length": p.avg_length,
            "time_span_days": p.time_span_days, "is_continuous": p.is_continuous,
        }
        for p in profiles
    ]

    # 列语义识别
    hints = state.get("column_hints")
    if hints:
        # 用户手动标注
        roles = {
            col: {"role": hints.get(col, "ignore"), "confidence": 1.0, "reason": "用户手动标注"}
            for col in df.columns
        }
    else:
        fake_data = CsvDataSource(file_path=state.get("file_path", "/tmp/fake.csv"))
        column_roles = analyzer._infer_roles(fake_data)
        roles = {
            col: {"role": r.role, "confidence": r.confidence, "reason": r.reason}
            for col, r in column_roles.items()
        }

    # 检测低置信度列
    ambiguity = []
    for col, role_info in roles.items():
        if role_info["confidence"] < 0.6:
            ambiguity.append({
                "column": col,
                "guessed_role": role_info["role"],
                "confidence": role_info["confidence"],
                "candidates": ["metric", "ignore", "revenue", "date"],
                "message": f"列'{col}'的语义角色不明确，推测为'{role_info['role']}'"
            })

    # 缓存中间产物
    key = _cache_key_from_state(state)
    _roles_cache[key] = column_roles
    _df_cache[key] = df

    result = {
        "data_profile": profile_dicts,
        "column_roles": roles,
        "ambiguity_report": ambiguity if ambiguity else None,
    }
    if ambiguity:
        result["status"] = "awaiting_confirm"  # 触发 interrupt
    return result


def node_clean(state: GraphState) -> GraphState:
    """
    Step 3: 数据清洗。

    按列角色差异化处理缺失值和异常值，
    生成质量评分和清洗操作记录。
    """
    df = _df_cache.get(_cache_key_from_state(state))
    column_roles = _roles_cache.get(_cache_key_from_state(state))

    if df is None or column_roles is None:
        return {"status": "error", "errors": [{"code": "INTERNAL", "message": "缓存丢失"}]}

    config = _config_from_state(state)
    analyzer = CsvAnalyzer(config=config)
    analyzer.raw_df = df.copy()
    analyzer.roles = column_roles
    analyzer.profile = analyzer._build_profile()

    cleaned, quality = analyzer._clean()

    # 缓存清洗后的 DataFrame
    _cleaned_cache[_cache_key_from_state(state)] = cleaned

    return {
        "quality_report": {
            "score": quality.score,
            "null_summary": quality.null_summary,
            "outlier_summary": quality.outlier_summary,
            "duplicate_rows": quality.duplicate_rows,
        },
        "cleaning_log": [
            {"column": a.column, "action": a.action, "affected_rows": a.affected_rows, "description": a.description}
            for a in quality.cleaning_actions
        ],
    }


def node_plan(state: GraphState) -> GraphState:
    """
    Step 4: 分析编排。

    根据可用维度生成分析计划。
    生产环境中此节点调用 LLM 动态决策。
    """
    roles = _roles_cache.get(_cache_key_from_state(state))
    if roles is None:
        return {"status": "error", "errors": [{"code": "INTERNAL", "message": "角色缓存丢失"}]}

    has_date = any(r.role == "date" for r in roles.values())
    has_revenue = any(r.role == "revenue" for r in roles.values())
    has_product = any(r.role in ("product_id", "product_name") for r in roles.values())
    has_region = any(r.role == "region" for r in roles.values())
    has_customer = any(r.role in ("customer_id", "customer_name") for r in roles.values())

    numeric_cols = [
        c for c, r in roles.items()
        if r.role in ("revenue", "quantity", "cost", "profit", "metric")
    ]
    has_multi_numeric = len(numeric_cols) >= 3
    has_multi_dim = sum([has_product, has_region, has_customer]) >= 2

    depth = state.get("analysis_depth", "standard")
    max_tasks = {"quick": 3, "standard": 7, "deep": 10}.get(depth, 7)

    # 构建计划（按优先级排序）
    plan = []

    # P0: 始终执行
    plan.append({"task_id": "desc_stats", "function": "desc_stats", "priority": 1})

    # 时序分析
    if has_date and numeric_cols:
        plan.append({"task_id": "time_series", "function": "time_series", "priority": 1})

    # 排名分析
    if has_product and numeric_cols:
        plan.append({"task_id": "top_ranking", "function": "top_ranking", "priority": 1})

    # ABC 帕累托
    if has_product and has_revenue:
        plan.append({"task_id": "pareto_abc", "function": "pareto_abc", "priority": 1})

    # 异常检测
    if numeric_cols:
        plan.append({"task_id": "anomaly_detect", "function": "anomaly_detect", "priority": 1})

    # 相关性
    if has_multi_numeric:
        plan.append({"task_id": "correlation", "function": "correlation", "priority": 2})

    # 分布分析
    if numeric_cols and depth in ("standard", "deep"):
        plan.append({"task_id": "distribution", "function": "distribution", "priority": 2})

    # 区域分析
    if has_region and numeric_cols:
        plan.append({"task_id": "region_analysis", "function": "region_analysis", "priority": 2})

    # 维度下钻
    if has_multi_dim:
        plan.append({"task_id": "drill_down", "function": "drill_down", "priority": 2})

    # RFM
    if has_customer and has_date and has_revenue:
        plan.append({"task_id": "rfm_analysis", "function": "rfm_analysis", "priority": 2})

    # 同比
    if has_date and numeric_cols:
        plan.append({"task_id": "yoy_comparison", "function": "yoy_comparison", "priority": 3})

    # 预测
    if depth == "deep" and has_date and numeric_cols:
        plan.append({"task_id": "simple_forecast", "function": "simple_forecast", "priority": 3})

    # 截断
    plan = plan[:max_tasks]

    # 缓存分析计划
    _plan_cache[_cache_key_from_state(state)] = plan

    return {"analysis_plan": plan}


def dispatch_analyses(state: GraphState) -> List[Send]:
    """
    条件路由: 根据 analysis_plan 并行派发分析任务。

    每个分析任务通过 Send API 发送到对应的分析节点，
    LangGraph 会自动并行执行并在全部完成后汇聚。
    通过 _cache_key 传递缓存索引，解决 Send arg 隔离问题。
    """
    plan = _plan_cache.get(_cache_key_from_state(state), [])
    cache_key = _cache_key_from_state(state)
    sends = []
    for task in plan:
        sends.append(Send("analyze_single", {
            "current_task": task,
            "_cache_key": cache_key,  # 传递缓存索引
        }))
    if not sends:
        sends.append(Send("analyze_single", {
            "current_task": {"task_id": "noop"},
            "_cache_key": cache_key,
        }))
    return sends


def node_analyze_single(state: GraphState) -> GraphState:
    """
    Step 5: 单个分析执行节点（由 Send API 并行调用）。

    每个实例独立执行一个分析任务。
    结果通过 Annotated[list, add] 自动合并到 analysis_results。
    """
    task = state.get("current_task", {})
    task_id = task.get("task_id", "unknown")
    if task_id == "noop":
        return {"analysis_results": []}

    cache_key = state.get("_cache_key", "")
    df = _cleaned_cache.get(cache_key)
    column_roles = _roles_cache.get(cache_key)
    if df is None:
        return {"analysis_results": [{"task_id": task_id, "status": "error", "error_message": "DataFrame 缓存丢失"}]}

    config = _config_from_state(state)
    analyzer = CsvAnalyzer(config=config)
    analyzer.cleaned_df = df
    analyzer.roles = column_roles

    # 映射 task_id → 分析方法
    method_map = {
        "desc_stats": lambda: _analyze_desc_via_cache(df, analyzer),
        "time_series": lambda: _run_time_series(analyzer, df, column_roles),
        "top_ranking": lambda: _run_ranking(analyzer, df, column_roles),
        "pareto_abc": lambda: _run_pareto(analyzer, df, column_roles),
        "anomaly_detect": lambda: _run_anomaly(analyzer, df, column_roles),
        "correlation": lambda: _run_correlation(analyzer, df, column_roles),
        "distribution": lambda: _run_distribution(analyzer, df, column_roles),
        "region_analysis": lambda: _run_region(analyzer, df, column_roles),
        "drill_down": lambda: _run_drilldown(analyzer, df, column_roles),
        "rfm_analysis": lambda: _run_rfm(analyzer, df, column_roles),
        "yoy_comparison": lambda: _run_yoy(analyzer, df, column_roles),
        "simple_forecast": lambda: _run_forecast(analyzer, df, column_roles),
    }

    fn = method_map.get(task_id)
    if fn is None:
        return {"analysis_results": [{"task_id": task_id, "status": "skipped"}]}

    try:
        sub_result = fn()
        return {"analysis_results": [{
            "task_id": task_id,
            "status": sub_result.status,
            "metrics": sub_result.metrics,
            "narration": sub_result.narration,
            "chart_path": sub_result.chart_path,
            "error_message": sub_result.error_message,
        }]}
    except Exception as exc:
        return {"analysis_results": [{"task_id": task_id, "status": "error", "error_message": str(exc)}]}


def node_synthesize(state: GraphState) -> GraphState:
    """
    Step 6: 综合洞察。

    汇总所有分析结果，生成 3-8 条分级洞察。
    """
    analyses = state.get("analysis_results", [])
    quality = state.get("quality_report", {})

    insights = []
    idx = 0

    # 数据规模洞察
    if state.get("raw_shape"):
        r, c = state["raw_shape"]
        insights.append({
            "id": f"i{idx:02d}", "severity": "info",
            "title": f"数据集包含 {r} 条记录，{c} 个字段",
            "evidence": [f"{r} 行 × {c} 列"],
            "interpretation": "数据规模适中，适合进行多维度统计分析。",
        })
        idx += 1

    # 时序洞察
    ts = next((a for a in analyses if a["task_id"] == "time_series" and a["status"] == "success"), None)
    if ts:
        metrics = ts.get("metrics", {})
        direction = metrics.get("trend_direction", "")
        mag = metrics.get("trend_magnitude", 0)
        if direction == "下降" and mag > 0.1:
            insights.append({
                "id": f"i{idx:02d}", "severity": "warning",
                "title": f"整体趋势下降，变动幅度 {mag:.1%}",
                "evidence": [f"趋势方向: {direction}", f"变动幅度: {mag:.1%}"],
                "interpretation": "数据呈下降趋势，建议排查下降原因。",
            })
            idx += 1
        elif direction:
            insights.append({
                "id": f"i{idx:02d}", "severity": "info",
                "title": f"整体趋势{direction}，变动幅度 {mag:.1%}",
                "evidence": [f"变动幅度: {mag:.1%}"],
                "interpretation": f"数据呈{direction}趋势。",
            })
            idx += 1

    # 质量洞察
    score = quality.get("score", 100)
    if score < 70:
        insights.append({
            "id": f"i{idx:02d}", "severity": "critical" if score < 50 else "warning",
            "title": f"数据质量偏低（{score}分）",
            "evidence": [f"质量评分: {score}/100"],
            "interpretation": "数据质量问题可能影响分析准确性。",
        })
        idx += 1
    else:
        insights.append({
            "id": f"i{idx:02d}", "severity": "info",
            "title": f"数据质量良好（{score}分）",
            "evidence": [f"质量评分: {score}/100"],
            "interpretation": "数据质量可以支撑可靠的统计分析。",
        })
        idx += 1

    # 集中度洞察
    rank = next((a for a in analyses if a["task_id"] == "top_ranking" and a["status"] == "success"), None)
    if rank:
        cr5 = rank["metrics"].get("cr5", 0)
        if cr5 > 0.6:
            insights.append({
                "id": f"i{idx:02d}", "severity": "warning",
                "title": f"头部集中度较高（CR5={cr5:.0%}）",
                "evidence": [f"前5名合计占比 {cr5:.1%}"],
                "interpretation": "少数核心产品/客户贡献了大部分收入，存在依赖风险。",
            })
            idx += 1

    return {"insights": insights[:8]}


def node_assemble(state: GraphState) -> GraphState:
    """
    Step 7 & 8: 聚焦回答 + 输出组装。

    生成 executive_summary 和 data_facts，
    将所有中间结果打包为最终输出。
    """
    # 执行摘要
    meta = state.get("parse_meta", {})
    quality = state.get("quality_report", {})
    parts = [f"数据集 {meta.get('row_count', '?')} 行 × {meta.get('col_count', '?')} 列。"]

    ts = next((a for a in state.get("analysis_results", []) if a["task_id"] == "time_series"), None)
    if ts and ts.get("status") == "success":
        total = ts["metrics"].get("total", 0)
        parts.append(f"总计 {total:,.0f}。")

    if quality:
        parts.append(f"数据质量 {quality.get('score', '?')}/100。")

    warnings = [i for i in state.get("insights", []) if i["severity"] in ("critical", "warning")]
    if warnings:
        parts.append(f"关注: {warnings[0]['title']}。")

    executive_summary = "".join(parts)

    # data_facts
    lines = ["## 数据概览", ""]
    if meta.get("row_count"):
        lines.append(f"- 总行数: {meta['row_count']}")
        lines.append(f"- 总列数: {meta['col_count']}")
    if quality.get("score"):
        lines.append(f"- 数据质量评分: {quality['score']}/100")
    lines.append("")

    return {
        "executive_summary": executive_summary,
        "data_facts": "\n".join(lines),
        "status": "success",
        "elapsed_seconds": round(time.time() - _start_times.get(_cache_key_from_state(state), time.time()), 1),
    }


# ============================================================================
# 分析函数分发适配器 — 从 column_roles 提取参数，调用 CsvAnalyzer 方法
# ============================================================================

def _get_col_by_role(roles: Dict, role_set: set) -> Optional[str]:
    """从 column_roles 中提取第一个匹配角色的列名"""
    for col, r in roles.items():
        if r.role in role_set:
            return col
    return None


def _run_time_series(analyzer, df, roles):
    date_col = _get_col_by_role(roles, {"date"})
    rev_col = _get_col_by_role(roles, {"revenue"}) or _get_col_by_role(roles, {"metric", "quantity", "cost"})
    if date_col and rev_col:
        return analyzer._analyze_time_series(df, date_col, rev_col)
    return SubAnalysisResult(task_id="time_series", status="skipped", error_message="缺少日期或数值列")

def _run_ranking(analyzer, df, roles):
    product_col = _get_col_by_role(roles, {"product_id", "product_name"})
    rev_col = _get_col_by_role(roles, {"revenue"}) or _get_col_by_role(roles, {"metric"})
    if product_col and rev_col:
        return analyzer._analyze_ranking(df, product_col, rev_col)
    return SubAnalysisResult(task_id="top_ranking", status="skipped")

def _run_pareto(analyzer, df, roles):
    product_col = _get_col_by_role(roles, {"product_id", "product_name"})
    rev_col = _get_col_by_role(roles, {"revenue"})
    if product_col and rev_col:
        return analyzer._analyze_pareto(df, product_col, rev_col)
    return SubAnalysisResult(task_id="pareto_abc", status="skipped")

def _run_anomaly(analyzer, df, roles):
    num_cols = [c for c, r in roles.items() if r.role in ("revenue", "quantity", "cost", "profit", "metric")]
    if num_cols:
        return analyzer._analyze_anomalies(df, num_cols[:4])
    return SubAnalysisResult(task_id="anomaly_detect", status="skipped")

def _run_correlation(analyzer, df, roles):
    num_cols = [c for c, r in roles.items() if r.role in ("revenue", "quantity", "cost", "profit", "metric")]
    if len(num_cols) >= 3:
        return analyzer._analyze_correlations(df, num_cols)
    return SubAnalysisResult(task_id="correlation", status="skipped")

def _run_distribution(analyzer, df, roles):
    num_cols = [c for c, r in roles.items() if r.role in ("revenue", "quantity", "cost", "profit", "metric")]
    if num_cols:
        return analyzer._analyze_distribution(df, num_cols[:5])
    return SubAnalysisResult(task_id="distribution", status="skipped")

def _run_region(analyzer, df, roles):
    region_col = _get_col_by_role(roles, {"region"})
    rev_col = _get_col_by_role(roles, {"revenue"}) or _get_col_by_role(roles, {"metric"})
    if region_col and rev_col:
        return analyzer._analyze_region(df, region_col, rev_col)
    return SubAnalysisResult(task_id="region_analysis", status="skipped")

def _run_drilldown(analyzer, df, roles):
    dim_cols = []
    p = _get_col_by_role(roles, {"product_id", "product_name"})
    rgn = _get_col_by_role(roles, {"region"})
    if p: dim_cols.append(p)
    if rgn: dim_cols.append(rgn)
    rev_col = _get_col_by_role(roles, {"revenue"}) or _get_col_by_role(roles, {"metric"})
    if len(dim_cols) >= 2 and rev_col:
        return analyzer._analyze_drilldown(df, dim_cols, rev_col)
    return SubAnalysisResult(task_id="drill_down", status="skipped")

def _run_rfm(analyzer, df, roles):
    cust = _get_col_by_role(roles, {"customer_id", "customer_name"})
    date = _get_col_by_role(roles, {"date"})
    rev = _get_col_by_role(roles, {"revenue"})
    if cust and date and rev:
        return analyzer._analyze_rfm(df, cust, date, rev)
    return SubAnalysisResult(task_id="rfm_analysis", status="skipped")

def _run_yoy(analyzer, df, roles):
    date_col = _get_col_by_role(roles, {"date"})
    rev_col = _get_col_by_role(roles, {"revenue"}) or _get_col_by_role(roles, {"metric"})
    if date_col and rev_col:
        return analyzer._analyze_yoy(df, date_col, rev_col)
    return SubAnalysisResult(task_id="yoy_comparison", status="skipped")

def _run_forecast(analyzer, df, roles):
    date_col = _get_col_by_role(roles, {"date"})
    rev_col = _get_col_by_role(roles, {"revenue"}) or _get_col_by_role(roles, {"metric"})
    if date_col and rev_col:
        return analyzer._analyze_forecast(df, date_col, rev_col)
    return SubAnalysisResult(task_id="simple_forecast", status="skipped")


# ============================================================================
# 内部缓存 — 节点间传递不可序列化对象
# ============================================================================

_df_cache: Dict[str, pd.DataFrame] = {}
_roles_cache: Dict[str, Any] = {}
_cleaned_cache: Dict[str, pd.DataFrame] = {}
_plan_cache: Dict[str, List[Dict]] = {}
_start_times: Dict[str, float] = {}


def _cache_key_from_state(state: GraphState) -> str:
    """从 state 中提取或生成缓存键。使用 _cache_key 或生成稳定的 key"""
    return state.get("_cache_key") or str(id(state))


def _ensure_cache_key(state: GraphState) -> str:
    """确保 state 中有 _cache_key"""
    key = state.get("_cache_key")
    if not key:
        key = uuid.uuid4().hex[:12]
    return key


def _analyze_desc_via_cache(df, analyzer):
    """desc_stats 适配器 —— 使用 cleaned_df 作为数据源"""
    old_raw = analyzer.raw_df
    analyzer.raw_df = df
    result = analyzer._describe_statistics()
    analyzer.raw_df = old_raw
    return result


def _safe_value(v):
    """将 numpy/msgpack 不兼容的类型转为原生 Python"""
    import numpy as np
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, np.ndarray):
        return v.tolist()
    if isinstance(v, dict):
        return {kk: _safe_value(vv) for kk, vv in v.items()}
    if isinstance(v, list):
        return [_safe_value(vv) for vv in v]
    return v


def _config_from_state(state: GraphState) -> AnalysisConfig:
    return AnalysisConfig(
        analysis_depth=state.get("analysis_depth", "standard"),
        focus_questions=state.get("focus_questions"),
        column_hints=state.get("column_hints"),
        encoding=state.get("encoding"),
        delimiter=state.get("delimiter"),
        output_locale=state.get("output_locale", "zh"),
    )


# ============================================================================
# 图构建
# ============================================================================

def build_graph(interrupt_on: Optional[List[str]] = None):
    """
    构建 LangGraph StateGraph。

    Args:
        interrupt_on: 中断条件列表，可选:
            - "ambiguity"  — 存在低置信度列时中断
            - "low_quality" — 质量分 < 60 时中断

    Returns:
        compiled graph
    """
    builder = StateGraph(GraphState)

    # 注册节点
    builder.add_node("load", node_load)
    builder.add_node("profile", node_profile)
    builder.add_node("clean", node_clean)
    builder.add_node("plan", node_plan)
    builder.add_node("analyze_single", node_analyze_single)  # Send 目标节点
    builder.add_node("synthesize", node_synthesize)
    builder.add_node("assemble", node_assemble)

    # 主流程边
    builder.add_edge(START, "load")
    builder.add_edge("load", "profile")
    builder.add_edge("profile", "clean")
    builder.add_edge("clean", "plan")

    # 条件路由: plan → 并行 ausend_analysis
    builder.add_conditional_edges("plan", dispatch_analyses, ["analyze_single"])

    # 汇聚: analyze_single → synthesize
    builder.add_edge("analyze_single", "synthesize")
    builder.add_edge("synthesize", "assemble")
    builder.add_edge("assemble", END)

    # 编译图
    if interrupt_on and "ambiguity" in interrupt_on:
        checkpointer = MemorySaver()
        return builder.compile(checkpointer=checkpointer, interrupt_before=["profile"])
    else:
        return builder.compile()


# ============================================================================
# 对外接口
# ============================================================================

class GraphAnalyzer:
    """
    基于 LangGraph 的 CSV 数据分析器。

    使用方式:
        analyzer = GraphAnalyzer()
        result = analyzer.run(file_path="/data/sales.csv", depth="standard")
    """

    def __init__(self, interrupt_on_ambiguity: bool = True):
        self._graph = build_graph(
            interrupt_on=["ambiguity"] if interrupt_on_ambiguity else None
        )

    def run(
        self,
        file_path: Optional[str] = None,
        content: Optional[str] = None,
        depth: str = "standard",
        focus_questions: Optional[List[str]] = None,
        column_hints: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        执行分析流水线。

        Args:
            file_path: CSV 文件路径（与 content 二选一）
            content:   CSV 文本内容
            depth:     quick / standard / deep
            focus_questions: 业务问题
            column_hints:    列语义标注

        Returns:
            分析结果 dict
        """
        tid = uuid.uuid4().hex[:12]  # 唯一标识本次调用
        _start_times[tid] = time.time()

        initial_state: GraphState = {
            "file_path": file_path,
            "content": content,
            "analysis_depth": depth,
            "focus_questions": focus_questions,
            "column_hints": column_hints,
            "output_locale": "zh",
            "has_header": True,
            "analysis_results": [],
            "errors": [],
            "status": "running",
        }

        config = {"configurable": {"thread_id": uuid.uuid4().hex[:8]}}

        # 使用 invoke 获取完整累积状态（stream 只返回节点增量）
        # try/finally 确保缓存始终清理，防止内存数据残留
        try:
            final_state = self._graph.invoke(initial_state, config)
        except Exception as exc:
            return {"status": "error", "message": str(exc)}
        finally:
            # 无论成功/失败/interrupt，始终清理缓存
            _df_cache.pop(tid, None)
            _roles_cache.pop(tid, None)
            _cleaned_cache.pop(tid, None)
            _plan_cache.pop(tid, None)
            _start_times.pop(tid, None)

        if final_state is None:
            return {"status": "error", "message": "流水线未产生输出"}

        # 检查 interrupt
        if final_state.get("status") == "awaiting_confirm":
            ambiguity = final_state.get("ambiguity_report", [])
            return {
                "status": "awaiting_confirm",
                "ambiguity_report": ambiguity,
                "message": f"发现 {len(ambiguity)} 列语义不明确，请确认后重新调用",
                "thread_id": config["configurable"]["thread_id"],
            }

        return {
            "status": final_state.get("status", "partial"),
            "executive_summary": final_state.get("executive_summary", ""),
            "data_facts": final_state.get("data_facts", ""),
            "insights": final_state.get("insights", []),
            "analysis_results": final_state.get("analysis_results", []),
            "quality_report": final_state.get("quality_report", {}),
            "data_profile": final_state.get("data_profile", []),
            "column_roles": final_state.get("column_roles", {}),
            "parse_meta": final_state.get("parse_meta", {}),
            "source_info": final_state.get("source_info", {}),
            "errors": final_state.get("errors", []),
            "elapsed_seconds": final_state.get("elapsed_seconds", 0),
        }

    def stream_run(self, **kwargs):
        """流式执行，逐节点返回状态更新"""
        tid = uuid.uuid4().hex[:12]
        _start_times[tid] = time.time()

        initial_state: GraphState = {
            "file_path": kwargs.get("file_path"),
            "content": kwargs.get("content"),
            "analysis_depth": kwargs.get("depth", "standard"),
            "focus_questions": kwargs.get("focus_questions"),
            "column_hints": kwargs.get("column_hints"),
            "output_locale": "zh",
            "has_header": True,
            "analysis_results": [],
            "errors": [],
            "status": "running",
        }

        config = {"configurable": {"thread_id": uuid.uuid4().hex[:8]}}
        try:
            for event in self._graph.stream(initial_state, config):
                yield event
        finally:
            # 确保流式调用异常时也清理缓存
            _df_cache.pop(tid, None)
            _roles_cache.pop(tid, None)
            _cleaned_cache.pop(tid, None)
            _plan_cache.pop(tid, None)
            _start_times.pop(tid, None)
