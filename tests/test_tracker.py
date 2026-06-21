"""
用量追踪模块测试。

运行: pytest tests/test_tracker.py -v
"""

import os
import sys
import pytest
import tempfile
import json
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from tracker import (
    UsageTracker, CallRecord, DailyReport, MonthlyReport,
    PRICING,
)


@pytest.fixture
def tracker():
    """创建临时 JSONL 日志文件"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        path = f.name
    tracker = UsageTracker(path)
    yield tracker
    os.unlink(path)


class TestLogging:
    """记录调用"""

    def test_log_success(self, tracker):
        record = tracker.log(
            tenant_id="team_a", user_id="u1", user_name="张三",
            depth="standard", model="claude-sonnet-4-6",
            token_input=5000, token_output=2500,
            status="success", elapsed_seconds=8.5,
        )
        assert record.status == "success"
        assert record.cost_usd > 0
        assert record.tenant_id == "team_a"

    def test_log_cost_calculation(self, tracker):
        """成本计算应正确"""
        record = tracker.log(
            tenant_id="team_a", user_id="u1", user_name="张三",
            depth="quick", model="claude-sonnet-4-6",
            token_input=3000, token_output=1000,
            status="success", elapsed_seconds=3.0,
        )
        # Sonnet: $3/MTok in, $15/MTok out
        expected = 3000/1e6*3 + 1000/1e6*15
        assert abs(record.cost_usd - expected) < 0.001

    def test_log_multiple_records(self, tracker):
        tracker.log("team_a", "u1", "张三", "quick", "claude-sonnet-4-6", 2000, 1000, "success", 2.0)
        tracker.log("team_a", "u2", "李四", "standard", "claude-sonnet-4-6", 5000, 2000, "success", 6.0)
        tracker.log("team_a", "u1", "张三", "deep", "claude-opus-4-8", 8000, 4000, "success", 15.0)
        # 应该都成功写入
        report = tracker.daily_report("team_a")
        assert report.total_calls == 3

    def test_log_error_record(self, tracker):
        tracker.log("team_a", "u1", "张三", "quick", "claude-sonnet-4-6", 1000, 0, "error", 1.0)
        report = tracker.daily_report("team_a")
        assert report.error_calls == 1


class TestDailyReport:
    """日报"""

    def test_daily_report_empty(self, tracker):
        report = tracker.daily_report("team_a")
        assert report.total_calls == 0
        assert report.total_cost_usd == 0.0

    def test_daily_report_with_data(self, tracker):
        tracker.log("team_a", "u1", "张三", "quick", "claude-sonnet-4-6", 2000, 1000, "success", 2.0)
        tracker.log("team_a", "u1", "张三", "standard", "claude-sonnet-4-6", 5000, 2000, "success", 5.0)
        tracker.log("team_a", "u2", "李四", "quick", "claude-sonnet-4-6", 2000, 1000, "success", 2.0)

        report = tracker.daily_report("team_a")
        assert report.total_calls == 3
        assert report.success_calls == 3
        assert report.total_cost_usd > 0
        assert "quick" in report.by_depth
        assert "standard" in report.by_depth
        assert len(report.top_3_expensive) >= 1

    def test_daily_report_by_user(self, tracker):
        tracker.log("team_a", "u1", "张三", "quick", "claude-sonnet-4-6", 2000, 1000, "success", 2.0)
        tracker.log("team_a", "u1", "张三", "quick", "claude-sonnet-4-6", 2000, 1000, "success", 2.0)
        tracker.log("team_a", "u2", "李四", "quick", "claude-sonnet-4-6", 2000, 1000, "success", 2.0)

        report = tracker.daily_report("team_a")
        assert report.by_user["张三"] == 2
        assert report.by_user["李四"] == 1


class TestMonthlyReport:
    """月报"""

    def test_monthly_report(self, tracker):
        tracker.log("team_a", "u1", "张三", "standard", "claude-sonnet-4-6", 5000, 2500, "success", 8.0)
        tracker.log("team_a", "u1", "张三", "deep", "claude-sonnet-4-6", 8000, 4000, "success", 15.0)

        report = tracker.monthly_report("team_a")
        assert report.total_calls == 2
        assert report.total_cost_usd > 0
        assert report.avg_cost_per_call > 0
        assert report.avg_elapsed_seconds > 0
        assert len(report.daily_breakdown) >= 1

    def test_monthly_report_empty(self, tracker):
        report = tracker.monthly_report("team_nonexistent")
        assert report.total_calls == 0


class TestUserStats:
    """用户统计"""

    def test_get_user_stats(self, tracker):
        tracker.log("team_a", "u1", "张三", "quick", "claude-sonnet-4-6", 2000, 1000, "success", 2.0)
        tracker.log("team_a", "u1", "张三", "standard", "claude-sonnet-4-6", 5000, 2000, "success", 5.0)
        tracker.log("team_a", "u2", "李四", "quick", "claude-sonnet-4-6", 2000, 1000, "success", 2.0)

        stats = tracker.get_user_stats("team_a", "u1", days=7)
        assert stats["total_calls"] == 2
        assert stats["total_tokens"] > 0
        assert "by_depth" in stats


class TestBudgetAlert:
    """预算预警"""

    def test_budget_not_exceeded(self, tracker):
        tracker.log("team_a", "u1", "张三", "quick", "claude-sonnet-4-6", 2000, 1000, "success", 2.0)
        alert = tracker.budget_alert("team_a", budget_usd=200.0, threshold=0.8)
        assert alert is None

    def test_budget_exceeded_threshold(self, tracker):
        # 模拟大量调用
        for i in range(100):
            tracker.log("team_a", "u1", "张三", "standard", "claude-opus-4-8",
                         5000, 2500, "success", 8.0)
        alert = tracker.budget_alert("team_a", budget_usd=10.0, threshold=0.1)
        assert alert is not None
        assert "预算告警" in alert


class TestPricing:
    """定价表"""

    def test_all_models_have_pricing(self):
        assert "claude-sonnet-4-6" in PRICING
        assert "claude-opus-4-8" in PRICING


class TestJSONLPersistence:
    """JSONL 持久化"""

    def test_records_survive_reopen(self, tracker):
        path = tracker._log_path
        tracker.log("team_a", "u1", "张三", "quick", "claude-sonnet-4-6", 2000, 1000, "success", 2.0)

        # 重新打开
        tracker2 = UsageTracker(path)
        report = tracker2.daily_report("team_a")
        assert report.total_calls == 1
