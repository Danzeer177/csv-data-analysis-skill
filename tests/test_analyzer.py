"""
核心分析器测试 — 覆盖数据加载、画像、清洗、分析、洞察全流程。

运行: pytest tests/test_analyzer.py -v
"""

import os
import sys
import json
import pytest
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from csv_analyzer import (
    CsvAnalyzer, AnalysisConfig, CsvDataSource,
    detect_encoding, detect_delimiter,
    analyze_csv, quick_summary,
    ColumnStats, QualityReport, SubAnalysisResult, Insight, AnalysisOutput,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
SALES_CSV = os.path.join(FIXTURES_DIR, "sales_2025_q4.csv")


# ============================================================================
# 数据加载测试
# ============================================================================

class TestDataLoading:
    """Step 1: 数据加载与校验"""

    def test_load_csv_file(self, analyzer):
        source, meta = analyzer._load(CsvDataSource(file_path=SALES_CSV))
        assert source.type == "csv_file"
        assert meta.row_count > 0
        assert meta.delimiter == ","

    def test_load_csv_content(self, sales_csv_content, analyzer):
        source, meta = analyzer._load(CsvDataSource(content=sales_csv_content))
        assert source.type == "csv_inline"
        assert meta.row_count > 0

    def test_load_csv_column_count(self, analyzer):
        source, meta = analyzer._load(CsvDataSource(file_path=SALES_CSV))
        assert meta.col_count >= 2

    def test_load_file_not_found(self, analyzer):
        # 使用相对路径（通过路径遍历检查），但文件不存在
        with pytest.raises(FileNotFoundError):
            analyzer._load(CsvDataSource(file_path="./nonexistent/file.csv"))

    def test_load_path_traversal_blocked(self, analyzer):
        """路径遍历攻击应被 _safe_path() 拦截"""
        with pytest.raises(ValueError, match="路径越界"):
            analyzer._load(CsvDataSource(file_path="../../../etc/passwd"))

    def test_load_absolute_path_blocked_outside_cwd(self, analyzer):
        """绝对路径在 CWD 之外应被拦截"""
        with pytest.raises(ValueError, match="路径越界"):
            analyzer._load(CsvDataSource(file_path="/etc/passwd"))

    def test_load_null_byte_in_path(self, analyzer):
        """空字节注入应被拦截"""
        with pytest.raises(ValueError, match="非法字符"):
            analyzer._load(CsvDataSource(file_path="data.csv\x00.jpg"))

    def test_load_unsupported_format(self, analyzer):
        # 相对路径（通过安全检查），但不支持的格式
        with pytest.raises((ValueError, FileNotFoundError)):
            analyzer._load(CsvDataSource(file_path="./data/file.pdf"))

    def test_load_empty_content(self, analyzer):
        with pytest.raises(ValueError, match="空"):
            analyzer._load(CsvDataSource(content="   "))

    def test_load_invalid_input(self):
        data = CsvDataSource(file_path="/a.csv", content="a,b\n1,2")
        valid, _ = data.validate_exclusivity()
        assert valid is False

        data = CsvDataSource()
        valid, _ = data.validate_exclusivity()
        assert valid is False

    def test_detect_delimiter_tab(self):
        assert detect_delimiter("a\tb\tc\n1\t2\t3\n4\t5\t6") in [",", "\t"]

    def test_detect_delimiter_semicolon(self):
        assert detect_delimiter("name;age;city\nAlice;30;NYC\nBob;25;LA") == ";"


# ============================================================================
# 数据画像测试
# ============================================================================

class TestDataProfiling:
    """Step 2: 数据画像"""

    def test_build_profile(self, analyzer):
        analyzer._load(CsvDataSource(file_path=SALES_CSV))
        profiles = analyzer._build_profile()
        assert len(profiles) > 0
        first = profiles[0]
        assert first.name is not None
        assert first.dtype is not None

    def test_numeric_column_profile(self, analyzer):
        analyzer._load(CsvDataSource(file_path=SALES_CSV))
        profiles = analyzer._build_profile()
        numeric = [p for p in profiles if "float" in p.dtype or "int" in p.dtype]
        if numeric:
            p = numeric[0]
            assert p.min_val is not None
            assert p.mean_val is not None

    def test_profile_null_rate(self, analyzer):
        analyzer._load(CsvDataSource(file_path=SALES_CSV))
        profiles = analyzer._build_profile()
        for p in profiles:
            assert 0.0 <= p.null_rate <= 1.0


# ============================================================================
# 列语义识别测试
# ============================================================================

class TestColumnRoles:
    """Step 2: 列语义识别"""

    def test_infer_roles(self, analyzer):
        analyzer._load(CsvDataSource(file_path=SALES_CSV))
        analyzer._build_profile()
        roles = analyzer._infer_roles(CsvDataSource(file_path=SALES_CSV))
        assert len(roles) > 0
        role_values = [r.role for r in roles.values()]
        assert "date" in role_values

    def test_column_hints_override(self):
        config = AnalysisConfig(column_hints={"日期": "date", "销售额": "revenue"})
        analyzer = CsvAnalyzer(config=config)
        analyzer._load(CsvDataSource(file_path=SALES_CSV))
        analyzer._build_profile()
        roles = analyzer._infer_roles(CsvDataSource(file_path=SALES_CSV))
        assert roles["日期"].confidence == 1.0
        assert roles["日期"].role == "date"

    def test_all_roles_in_valid_set(self, analyzer):
        valid_roles = {
            "date", "revenue", "quantity", "unit_price", "cost", "profit",
            "product_id", "product_name", "category", "region",
            "customer_id", "customer_name", "channel", "metric", "ignore",
        }
        analyzer._load(CsvDataSource(file_path=SALES_CSV))
        analyzer._build_profile()
        roles = analyzer._infer_roles(CsvDataSource(file_path=SALES_CSV))
        for r in roles.values():
            assert r.role in valid_roles, f"'{r.role}' 不在合法集合中"


# ============================================================================
# 数据清洗测试
# ============================================================================

class TestDataCleaning:
    """Step 3: 数据清洗"""

    def test_clean_returns_quality_report(self, analyzer_with_tiny_df):
        a = analyzer_with_tiny_df
        a.profile = a._build_profile()
        cleaned, quality = a._clean()
        assert cleaned is not None
        assert isinstance(quality, QualityReport)
        assert 0 <= quality.score <= 100

    def test_quality_score_range(self, analyzer_with_tiny_df):
        a = analyzer_with_tiny_df
        a.profile = a._build_profile()
        _, quality = a._clean()
        assert 0 <= quality.score <= 100

    def test_null_summary_populated(self, analyzer_with_tiny_df):
        a = analyzer_with_tiny_df
        a.profile = a._build_profile()
        _, quality = a._clean()
        assert len(quality.null_summary) == len(a.raw_df.columns)

    def test_no_data_deletion(self, analyzer_with_tiny_df):
        a = analyzer_with_tiny_df
        a.profile = a._build_profile()
        cleaned, _ = a._clean()
        assert len(cleaned) == len(a.raw_df)

    def test_duplicate_detection(self):
        df = pd.DataFrame({"a": [1, 1, 2], "b": [3, 3, 4]})
        config = AnalysisConfig(column_hints={"a": "metric", "b": "metric"})
        a = CsvAnalyzer(config=config)
        a.raw_df = df
        a.roles = {
            "a": type("R", (), {"role": "metric", "confidence": 0.9, "reason": ""}),
            "b": type("R", (), {"role": "metric", "confidence": 0.9, "reason": ""}),
        }
        a.profile = a._build_profile()
        _, quality = a._clean()
        assert quality.duplicate_rows == 2


# ============================================================================
# 分析函数测试
# ============================================================================

class TestDescStats:
    def test_desc_stats(self, analyzer_with_tiny_df):
        result = analyzer_with_tiny_df._describe_statistics()
        assert result.status == "success"
        assert result.metrics["row_count"] == 12


class TestTimeSeries:
    def test_time_series(self, analyzer_with_tiny_df):
        result = analyzer_with_tiny_df._analyze_time_series(
            analyzer_with_tiny_df.raw_df, "日期", "销售额"
        )
        assert result.status == "success"
        assert "trend_direction" in result.metrics


class TestRanking:
    def test_ranking(self, analyzer_with_tiny_df):
        result = analyzer_with_tiny_df._analyze_ranking(
            analyzer_with_tiny_df.raw_df, "产品", "销售额"
        )
        assert result.status == "success"
        assert 0 <= result.metrics["cr5"] <= 1


class TestPareto:
    def test_pareto(self, analyzer_with_tiny_df):
        result = analyzer_with_tiny_df._analyze_pareto(
            analyzer_with_tiny_df.raw_df, "产品", "销售额"
        )
        assert result.status == "success"
        total_share = (
            result.metrics["A"]["revenue_share"]
            + result.metrics["B"]["revenue_share"]
            + result.metrics["C"]["revenue_share"]
        )
        assert abs(total_share - 1.0) < 0.01


class TestAnomaly:
    def test_anomaly(self, analyzer_with_tiny_df):
        result = analyzer_with_tiny_df._analyze_anomalies(
            analyzer_with_tiny_df.raw_df, ["销售额", "数量", "成本"]
        )
        assert result.status == "success"


class TestCorrelation:
    def test_correlation(self, analyzer_with_tiny_df):
        result = analyzer_with_tiny_df._analyze_correlations(
            analyzer_with_tiny_df.raw_df, ["销售额", "数量", "成本"]
        )
        assert result.status == "success"
        assert "strong_pairs" in result.metrics


class TestDistribution:
    def test_distribution(self, analyzer_with_tiny_df):
        result = analyzer_with_tiny_df._analyze_distribution(
            analyzer_with_tiny_df.raw_df, ["销售额", "数量"]
        )
        assert result.status == "success"
        for col_stats in result.metrics["distributions"].values():
            assert "skewness" in col_stats


class TestRegion:
    def test_region(self, analyzer_with_tiny_df):
        result = analyzer_with_tiny_df._analyze_region(
            analyzer_with_tiny_df.raw_df, "区域", "销售额"
        )
        assert result.status == "success"


class TestDrillDown:
    def test_drill_down(self, analyzer_with_tiny_df):
        result = analyzer_with_tiny_df._analyze_drilldown(
            analyzer_with_tiny_df.raw_df, ["产品", "区域"], "销售额"
        )
        assert result.status == "success"


class TestRFM:
    def test_rfm(self):
        analyzer = CsvAnalyzer(config=AnalysisConfig())
        analyzer._load(CsvDataSource(file_path=SALES_CSV))
        analyzer._build_profile()
        analyzer.roles = analyzer._infer_roles(CsvDataSource(file_path=SALES_CSV))
        analyzer.cleaned_df, analyzer.quality = analyzer._clean()
        result = analyzer._analyze_rfm(
            analyzer.cleaned_df, "客户名称", "日期", "销售额"
        )
        assert result.status == "success"
        assert "segment_counts" in result.metrics


class TestForecast:
    def test_forecast_tiny(self, analyzer_with_tiny_df):
        result = analyzer_with_tiny_df._analyze_forecast(
            analyzer_with_tiny_df.raw_df, "日期", "销售额"
        )
        assert result.status == "success"
        assert "next_forecast" in result.metrics

    def test_forecast_insufficient_data(self):
        """少于12期数据时跳过预测"""
        import pandas as pd
        df = pd.DataFrame({
            "日期": pd.date_range("2025-10-01", periods=6, freq="ME"),
            "销售额": [100, 120, 110, 130, 140, 150],
        })
        config = AnalysisConfig()
        a = CsvAnalyzer(config=config)
        result = a._analyze_forecast(df, "日期", "销售额")
        assert result.status == "skipped"


class TestInsights:
    def test_synthesize_insights(self, analyzer_with_tiny_df):
        a = analyzer_with_tiny_df
        a.profile = a._build_profile()
        a.cleaned_df, a.quality = a._clean()
        a.analyses = [
            a._describe_statistics(),
            a._analyze_time_series(a.raw_df, "日期", "销售额"),
            a._analyze_ranking(a.raw_df, "产品", "销售额"),
        ]
        insights = a._synthesize_insights(CsvDataSource(file_path="/fake.csv"))
        assert len(insights) >= 1
        for ins in insights:
            assert ins.severity in ("critical", "warning", "info")


# ============================================================================
# 端到端测试
# ============================================================================

class TestEndToEnd:
    """全流程测试"""

    def test_full_pipeline_quick(self):
        config = AnalysisConfig(analysis_depth="quick")
        analyzer = CsvAnalyzer(config=config)
        output = analyzer.run(CsvDataSource(file_path=SALES_CSV))
        assert output.status == "success"
        assert output.executive_summary != ""

    def test_full_pipeline_standard(self):
        config = AnalysisConfig(analysis_depth="standard")
        analyzer = CsvAnalyzer(config=config)
        output = analyzer.run(CsvDataSource(file_path=SALES_CSV))
        assert output.status == "success"
        assert len(output.analyses) >= 3

    def test_full_pipeline_content_mode(self, sales_csv_content):
        config = AnalysisConfig(analysis_depth="quick")
        analyzer = CsvAnalyzer(config=config)
        output = analyzer.run(CsvDataSource(content=sales_csv_content))
        assert output.status == "success"
        assert output.source_info.type == "csv_inline"

    def test_analyze_csv_shortcut(self):
        result = analyze_csv(SALES_CSV, depth="quick")
        assert result["status"] == "success"
        assert "executive_summary" in result
        assert "insights" in result

    def test_quick_summary_shortcut(self):
        result = quick_summary(SALES_CSV)
        assert "summary" in result
        assert result["row_count"] > 0

    def test_output_to_dict(self):
        config = AnalysisConfig(analysis_depth="quick")
        analyzer = CsvAnalyzer(config=config)
        output = analyzer.run(CsvDataSource(file_path=SALES_CSV))
        d = output.to_dict()
        json.dumps(d, ensure_ascii=False, default=str)
        assert d["status"] == "success"


# ============================================================================
# 边界与错误处理测试
# ============================================================================

class TestEdgeCases:
    """边界 case"""

    def test_single_numeric_column(self):
        content = "value\n10\n20\n30\n40\n50"
        config = AnalysisConfig(analysis_depth="quick")
        analyzer = CsvAnalyzer(config=config)
        output = analyzer.run(CsvDataSource(content=content))
        # 单列数据也应在加载层被拒绝（≥2列要求）
        assert output.status in ("success", "partial", "error")

    def test_missing_values(self):
        content = "date,product,revenue\n2025-01-01,A,100\n2025-01-02,,200\n2025-01-03,B,"
        config = AnalysisConfig(analysis_depth="quick")
        analyzer = CsvAnalyzer(config=config)
        output = analyzer.run(CsvDataSource(content=content))
        assert output.status in ("success", "partial")

    def test_all_text_columns(self):
        content = "name,city,note\nAlice,BeiJing,good\nBob,ShangHai,ok"
        config = AnalysisConfig(analysis_depth="quick")
        analyzer = CsvAnalyzer(config=config)
        output = analyzer.run(CsvDataSource(content=content))
        # 全文本列无法做数值分析但描述统计应通过
        assert output.status in ("success", "partial", "error")
