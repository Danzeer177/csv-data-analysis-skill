"""
团队级集成测试 — TeamCsvAnalyzer 端到端。

运行: pytest tests/test_team.py -v
"""

import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from csv_analyzer import TeamCsvAnalyzer, CsvDataSource, AnalysisConfig
from auth import AuthManager, AuthError, RateLimitError, QuotaExceededError
from tracker import UsageTracker

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
SALES_CSV = os.path.join(FIXTURES_DIR, "sales_2025_q4.csv")


# 团队配置（含 admin 和 viewer）
TEAM_CONFIG = """
teams:
  - id: "team_integration"
    name: "集成测试团队"
    monthly_budget: 500.0
    max_concurrent: 10
    data_retention: "none"
    members:
      - id: "admin"
        name: "管理员"
        role: admin
        api_key: "sk-integration-admin"
        default_depth: standard
        daily_limit: 100
        output_locale: zh
      - id: "viewer"
        name: "查看者"
        role: viewer
        api_key: "sk-integration-viewer"
        default_depth: quick
        daily_limit: 10
        output_locale: zh

roles:
  admin:
    can_use_depth: [quick, standard, deep]
    can_export_charts: true
    can_configure: true
  viewer:
    can_use_depth: [quick]
    can_export_charts: false
    can_configure: false

rate_limits:
  requests_per_second: 10
  burst_size: 20
  cooldown_seconds: 1
"""


@pytest.fixture
def team_analyzer():
    """创建团队分析器"""
    # 配置文件
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(TEAM_CONFIG)
        config_path = f.name

    # 追踪日志
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        log_path = f.name

    auth = AuthManager(config_path)
    tracker = UsageTracker(log_path)
    analyzer = TeamCsvAnalyzer(auth=auth, tracker=tracker, model="claude-sonnet-4-6")

    yield analyzer

    os.unlink(config_path)
    os.unlink(log_path)


class TestTeamIntegration:
    """团队集成"""

    def test_admin_run_standard(self, team_analyzer):
        """admin 用户执行 standard 分析"""
        result = team_analyzer.run_for_user(
            api_key="sk-integration-admin",
            data=CsvDataSource(file_path=SALES_CSV),
            depth="standard",
        )
        assert result["status"] == "success"
        assert "_team_meta" in result
        assert result["_team_meta"]["role"] == "admin"

    def test_viewer_run_quick(self, team_analyzer):
        """viewer 用户执行 quick 分析"""
        result = team_analyzer.run_for_user(
            api_key="sk-integration-viewer",
            data=CsvDataSource(file_path=SALES_CSV),
            depth="quick",
        )
        assert result["status"] == "success"

    def test_viewer_cannot_run_standard(self, team_analyzer):
        """viewer 用户无权执行 standard 分析"""
        with pytest.raises(AuthError, match="不支持"):
            team_analyzer.run_for_user(
                api_key="sk-integration-viewer",
                data=CsvDataSource(file_path=SALES_CSV),
                depth="standard",
            )

    def test_invalid_api_key(self, team_analyzer):
        """无效 API Key"""
        with pytest.raises(AuthError, match="无效"):
            team_analyzer.run_for_user(
                api_key="sk-wrong-key",
                data=CsvDataSource(file_path=SALES_CSV),
                depth="quick",
            )

    def test_admin_get_team_usage(self, team_analyzer):
        """管理员查看团队用量"""
        # 先执行几次调用
        team_analyzer.run_for_user(
            "sk-integration-admin",
            CsvDataSource(file_path=SALES_CSV),
            "quick",
        )
        team_analyzer.run_for_user(
            "sk-integration-viewer",
            CsvDataSource(file_path=SALES_CSV),
            "quick",
        )

        usage = team_analyzer.get_team_usage("sk-integration-admin")
        assert usage["tenant_id"] == "team_integration"
        assert usage["today"]["calls"] >= 2
        assert usage["this_month"]["calls"] >= 2
        assert "budget_alert" in usage

    def test_viewer_cannot_get_usage(self, team_analyzer):
        """viewer 无权查看团队用量"""
        with pytest.raises(AuthError, match="仅管理员"):
            team_analyzer.get_team_usage("sk-integration-viewer")

    def test_result_contains_team_meta(self, team_analyzer):
        """结果中附加团队信息"""
        result = team_analyzer.run_for_user(
            api_key="sk-integration-admin",
            data=CsvDataSource(file_path=SALES_CSV),
            depth="standard",
        )
        meta = result["_team_meta"]
        assert meta["tenant_id"] == "team_integration"
        assert meta["user_name"] == "管理员"
        assert meta["role"] == "admin"
        assert "daily_remaining" in meta

    def test_daily_quota_counted(self, team_analyzer):
        """日配额应正确递减"""
        # 使用 viewers (日限额 10)
        for _ in range(3):
            team_analyzer.run_for_user(
                "sk-integration-viewer",
                CsvDataSource(file_path=SALES_CSV),
                "quick",
            )

        usage = team_analyzer.get_team_usage("sk-integration-admin")
        # viewer 用户调用应被计入
        viewer_stats = [m for m in usage.get("members", []) if m.get("name") == "查看者"]
        # 直接的 today 总数应该 >= 3
        assert usage["today"]["calls"] >= 3

    def test_exclusive_file_and_content_validation(self):
        """file_path 和 content 互斥校验"""
        data = CsvDataSource(file_path="/a.csv", content="a,b\n1,2")
        valid, _ = data.validate_exclusivity()
        assert valid is False

        data2 = CsvDataSource(file_path="/a.csv")
        valid2, _ = data2.validate_exclusivity()
        assert valid2 is True
