"""
测试夹具 — 共享的测试数据和工具函数。
"""

import os
import sys
import io
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from csv_analyzer import (
    CsvAnalyzer, AnalysisConfig, CsvDataSource,
    ColumnStats, QualityReport, SubAnalysisResult, Insight,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
SALES_CSV = os.path.join(FIXTURES_DIR, "sales_2025_q4.csv")


@pytest.fixture
def sales_csv_path():
    """返回模拟销售 CSV 文件的路径"""
    return SALES_CSV


@pytest.fixture
def sales_csv_content():
    """返回模拟销售 CSV 的内存字符串内容"""
    with open(SALES_CSV, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def default_config():
    """默认分析配置"""
    return AnalysisConfig(analysis_depth="standard", output_locale="zh")


@pytest.fixture
def quick_config():
    """快速分析配置"""
    return AnalysisConfig(analysis_depth="quick")


@pytest.fixture
def analyzer(default_config):
    """标准分析器实例"""
    return CsvAnalyzer(config=default_config)


@pytest.fixture
def tiny_df():
    """小型手工 DataFrame 用于单元测试"""
    import pandas as pd
    df = pd.DataFrame({
        "日期": pd.date_range("2025-10-01", periods=12, freq="ME"),
        "产品": ["A", "A", "B", "B", "C", "C", "A", "B", "C", "A", "B", "C"],
        "区域": ["华东", "华南", "华东", "华北", "华南", "华北", "华东", "华南", "华北", "华东", "华南", "华北"],
        "销售额": [10000, 12000, 8000, 9500, 5000, 6000, 11000, 9000, 5500, 13000, 10000, 7000],
        "数量": [10, 12, 8, 9, 5, 6, 11, 9, 5, 13, 10, 7],
        "成本": [7000, 8400, 5000, 6000, 3000, 3800, 7700, 5600, 3400, 9100, 6400, 4200],
    })
    return df


@pytest.fixture
def analyzer_with_tiny_df(tiny_df, default_config):
    """预加载 tiny_df 的分析器"""
    a = CsvAnalyzer(config=default_config)
    a.raw_df = tiny_df
    a.roles = {
        "日期": type("Role", (), {"role": "date", "confidence": 0.9, "reason": ""}),
        "产品": type("Role", (), {"role": "product_name", "confidence": 0.9, "reason": ""}),
        "区域": type("Role", (), {"role": "region", "confidence": 0.85, "reason": ""}),
        "销售额": type("Role", (), {"role": "revenue", "confidence": 0.95, "reason": ""}),
        "数量": type("Role", (), {"role": "quantity", "confidence": 0.9, "reason": ""}),
        "成本": type("Role", (), {"role": "cost", "confidence": 0.9, "reason": ""}),
    }
    return a
