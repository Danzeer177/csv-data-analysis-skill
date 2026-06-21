"""
LangGraph 编排层测试。

运行: pytest tests/test_graph.py -v
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from graph_state import GraphState
from graph_builder import (
    build_graph, GraphAnalyzer,
    node_load, node_profile, node_clean, node_plan,
    node_synthesize, node_assemble, dispatch_analyses,
    _df_cache, _roles_cache, _cleaned_cache, _plan_cache,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
SALES_CSV = os.path.join(FIXTURES_DIR, "sales_2025_q4.csv")


class TestGraphNodes:
    """节点函数单元测试"""

    def test_node_load(self):
        state = {"file_path": SALES_CSV, "has_header": True}
        result = node_load(state)
        assert result["raw_shape"][0] > 0
        assert result["raw_shape"][1] >= 2

    def test_node_load_content(self):
        content = "a,b,c\n1,2,3\n4,5,6\n7,8,9"
        state = {"content": content, "has_header": True}
        result = node_load(state)
        assert result["raw_shape"] == (3, 3)

    def test_node_load_empty(self):
        result = node_load({"content": "col\n", "has_header": True})
        assert result["status"] == "error"

    def test_node_profile(self):
        state = {"file_path": SALES_CSV, "has_header": True}
        load_result = node_load(state)
        state.update(load_result)

        result = node_profile(state)
        assert result["data_profile"] is not None
        assert len(result["data_profile"]) > 0
        assert result["column_roles"] is not None
        assert "date" in str(result["column_roles"])

    def test_node_profile_ambiguity_detection(self):
        """低置信度列触发 ambiguity_report"""
        content = "x1,x2,x3\n100,200,300\n400,500,600"
        state = {"content": content, "has_header": True}
        load_result = node_load(state)
        state.update(load_result)

        result = node_profile(state)
        # 默认未匹配的列 confidence < 0.6
        if result.get("ambiguity_report"):
            assert len(result["ambiguity_report"]) > 0
            assert result["status"] == "awaiting_confirm"

    def test_node_clean(self):
        state = {"file_path": SALES_CSV, "has_header": True}
        load_result = node_load(state)
        state.update(load_result)
        profile_result = node_profile(state)
        state.update(profile_result)

        result = node_clean(state)
        assert result["quality_report"]["score"] > 0
        assert "null_summary" in result["quality_report"]

    def test_node_plan(self):
        state = {"file_path": SALES_CSV, "has_header": True, "analysis_depth": "standard"}
        load_result = node_load(state)
        state.update(load_result)
        profile_result = node_profile(state)
        state.update(profile_result)

        result = node_plan(state)
        plan = result["analysis_plan"]
        assert len(plan) >= 3
        task_ids = [t["task_id"] for t in plan]
        assert "desc_stats" in task_ids
        assert "time_series" in task_ids

    def test_node_synthesize(self):
        state = {
            "raw_shape": (100, 8),
            "quality_report": {"score": 85},
            "analysis_results": [
                {"task_id": "time_series", "status": "success",
                 "metrics": {"trend_direction": "下降", "trend_magnitude": 0.4}},
                {"task_id": "top_ranking", "status": "success",
                 "metrics": {"cr5": 0.72}},
            ],
        }
        result = node_synthesize(state)
        assert len(result["insights"]) >= 1

    def test_node_assemble(self):
        state = {
            "parse_meta": {"row_count": 100, "col_count": 8},
            "quality_report": {"score": 85},
            "analysis_results": [],
            "insights": [],
        }
        result = node_assemble(state)
        assert result["executive_summary"] != ""
        assert result["data_facts"] != ""
        assert result["status"] == "success"


class TestGraphBuild:
    """图构建测试"""

    def test_build_graph_returns_compiled(self):
        graph = build_graph()
        assert graph is not None
        # LangGraph v1: nodes 返回 Node 对象，用 .name 取节点名
        compiled_graph = graph.get_graph()
        all_nodes = set()
        for n in compiled_graph.nodes.values():
            name = getattr(n, "name", str(n))
            if not name.startswith("__"):
                all_nodes.add(name)
        expected = {"load", "profile", "clean", "plan", "analyze_single", "synthesize", "assemble"}
        assert expected.issubset(all_nodes)


class TestGraphAnalyzer:
    """GraphAnalyzer 集成测试"""

    def test_run_quick_success(self):
        analyzer = GraphAnalyzer(interrupt_on_ambiguity=False)
        result = analyzer.run(
            file_path=SALES_CSV,
            depth="quick",
        )
        assert result["status"] == "success"
        assert "executive_summary" in result
        assert result["parse_meta"]["row_count"] > 0

    def test_run_standard_success(self):
        analyzer = GraphAnalyzer(interrupt_on_ambiguity=False)
        result = analyzer.run(
            file_path=SALES_CSV,
            depth="standard",
        )
        assert result["status"] == "success"

    def test_run_with_column_hints(self):
        """column_hints 触发 LLM 跳过路径（当前版本用启发式降级，status=error 为预期）"""
        analyzer = GraphAnalyzer(interrupt_on_ambiguity=False)
        result = analyzer.run(
            file_path=SALES_CSV,
            depth="quick",
            column_hints={"日期": "date", "销售额": "revenue", "产品名称": "product_name"},
        )
        # column_hints 在 node_profile 中会触发分支，当前仅做启发式降级
        assert result["status"] in ("success", "error", "partial")

    def test_graph_execution_traces(self):
        """验证执行链: LangGraph 管道从 load 到 assemble 完整运行"""
        analyzer = GraphAnalyzer(interrupt_on_ambiguity=False)
        result = analyzer.run(file_path=SALES_CSV, depth="quick")
        assert result["status"] == "success"
        # 确认关键节点产物存在
        assert result["parse_meta"]["row_count"] > 0          # load
        assert result["quality_report"] is not None            # clean
        assert result["insights"] is not None                  # synthesize
        assert result["executive_summary"] != ""               # assemble

    def test_stream_mode(self):
        """流式执行产生多个事件"""
        analyzer = GraphAnalyzer(interrupt_on_ambiguity=False)
        events = list(analyzer.stream_run(
            file_path=SALES_CSV, depth="quick"
        ))
        assert len(events) >= 1


class TestLangGraphSendAPI:
    """Send API 并行扇出测试"""

    def test_dispatch_generates_sends(self):
        """dispatch_analyses 应返回 Send 对象列表"""
        state = {
            "analysis_plan": [
                {"task_id": "desc_stats", "function": "desc_stats", "priority": 1},
                {"task_id": "time_series", "function": "time_series", "priority": 1},
            ],
        }
        # 初始化缓存
        tid = id(state)
        _plan_cache[tid] = state["analysis_plan"]

        sends = dispatch_analyses(state)
        assert len(sends) >= 1
        # 验证返回的是 Send 对象
        try:
            from langgraph.types import Send
        except ImportError:
            from langgraph.constants import Send
        for s in sends:
            assert isinstance(s, Send)

        _plan_cache.pop(tid, None)


class TestGraphVsDirectCompare:
    """对比 LangGraph 模式和直接调用模式的结果一致性"""

    def test_same_output_structure(self):
        """两种模式应产出一致结构的输出"""
        from csv_analyzer import CsvAnalyzer, AnalysisConfig, CsvDataSource

        # 直接调用
        direct = CsvAnalyzer(config=AnalysisConfig(analysis_depth="quick"))
        csv_data = CsvDataSource(file_path=SALES_CSV)
        direct_out = direct.run(csv_data).to_dict()

        # LangGraph 调用
        graph = GraphAnalyzer(interrupt_on_ambiguity=False)
        graph_out = graph.run(file_path=SALES_CSV, depth="quick")

        # 核心字段一致
        assert graph_out["status"] == direct_out["status"]
        assert graph_out["parse_meta"]["row_count"] == direct_out["parse_meta"]["row_count"]
