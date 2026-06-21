"""
鉴权模块测试。

运行: pytest tests/test_auth.py -v
"""

import os
import sys
import pytest
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from auth import AuthManager, AuthError, RateLimitError, QuotaExceededError, TeamUser


# 最小团队配置
MINIMAL_CONFIG = """
teams:
  - id: "team_test"
    name: "测试团队"
    monthly_budget: 100.0
    max_concurrent: 5
    data_retention: "none"
    members:
      - id: "admin_01"
        name: "管理员"
        role: admin
        api_key: "sk-admin-test-123"
        default_depth: standard
        daily_limit: 50
        output_locale: zh
      - id: "analyst_01"
        name: "分析师"
        role: analyst
        api_key: "sk-analyst-test-456"
        default_depth: deep
        daily_limit: 20
        output_locale: zh
      - id: "viewer_01"
        name: "查看者"
        role: viewer
        api_key: "sk-viewer-test-789"
        default_depth: quick
        daily_limit: 5
        output_locale: zh

roles:
  admin:
    can_use_depth: [quick, standard, deep]
    can_export_charts: true
    can_configure: true
  analyst:
    can_use_depth: [quick, standard, deep]
    can_export_charts: true
    can_configure: false
  viewer:
    can_use_depth: [quick]
    can_export_charts: false
    can_configure: false

rate_limits:
  requests_per_second: 3
  burst_size: 5
  cooldown_seconds: 1
"""


@pytest.fixture
def config_file():
    """创建临时配置文件"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(MINIMAL_CONFIG)
        path = f.name
    yield path
    os.unlink(path)


@pytest.fixture
def auth(config_file):
    """鉴权管理器"""
    return AuthManager(config_file)


class TestAuthentication:
    """鉴权"""

    def test_valid_admin_key(self, auth):
        user = auth.authenticate("sk-admin-test-123")
        assert user.user_id == "admin_01"
        assert user.role == "admin"
        assert user.tenant_id == "team_test"

    def test_valid_analyst_key(self, auth):
        user = auth.authenticate("sk-analyst-test-456")
        assert user.role == "analyst"
        assert user.daily_limit == 20

    def test_valid_viewer_key(self, auth):
        user = auth.authenticate("sk-viewer-test-789")
        assert user.role == "viewer"
        assert user.default_depth == "quick"

    def test_invalid_key(self, auth):
        with pytest.raises(AuthError, match="无效"):
            auth.authenticate("sk-invalid-key")

    def test_user_structure(self, auth):
        user = auth.authenticate("sk-admin-test-123")
        assert isinstance(user, TeamUser)
        assert user.user_id
        assert user.tenant_id
        assert user.role in ("admin", "analyst", "viewer")


class TestAuthorization:
    """权限校验"""

    def test_admin_can_use_deep(self, auth):
        user = auth.authenticate("sk-admin-test-123")
        auth.authorize(user, "deep")  # 不应抛异常

    def test_viewer_cannot_use_standard(self, auth):
        user = auth.authenticate("sk-viewer-test-789")
        with pytest.raises(AuthError, match="不支持"):
            auth.authorize(user, "standard")

    def test_viewer_can_use_quick(self, auth):
        user = auth.authenticate("sk-viewer-test-789")
        auth.authorize(user, "quick")  # 不应抛异常


class TestRateLimit:
    """速率限制"""

    def test_rate_limit_within_bounds(self, auth):
        user = auth.authenticate("sk-admin-test-123")
        # 连续 5 次不超限
        for _ in range(5):
            auth.check_rate_limit(user.tenant_id)

    def test_rate_limit_exceeded(self, auth):
        user = auth.authenticate("sk-admin-test-123")
        # 填满 burst
        for _ in range(5):
            auth.check_rate_limit(user.tenant_id)
        # 第 6 次应超限
        with pytest.raises(RateLimitError):
            auth.check_rate_limit(user.tenant_id)

    def test_rate_limit_independent_per_tenant(self, auth):
        """不同团队的速率限制独立"""
        # 使用同一 key 对应同一 tenant，多团队需要多个团队配置


class TestDailyQuota:
    """日配额"""

    def test_quota_within_limit(self, auth):
        user = auth.authenticate("sk-viewer-test-789")
        for _ in range(5):  # 限额 5
            auth.check_daily_quota(user)
            auth.record_call(user, "quick")

    def test_quota_exceeded(self, auth):
        user = auth.authenticate("sk-viewer-test-789")
        for _ in range(5):
            auth.check_daily_quota(user)
            auth.record_call(user, "quick")
        with pytest.raises(QuotaExceededError):
            auth.check_daily_quota(user)


class TestUsageReport:
    """用量报告"""

    def test_usage_report(self, auth):
        user = auth.authenticate("sk-admin-test-123")
        auth.record_call(user, "standard", 5000, "success")
        auth.record_call(user, "quick", 3000, "success")

        report = auth.get_usage_report(user.tenant_id)
        assert report["tenant_id"] == "team_test"
        assert report["today_calls"] >= 2

    def test_list_tenants(self, auth):
        tenants = auth.list_tenants()
        assert "team_test" in tenants


class TestEnvVarResolution:
    """环境变量注入"""

    def test_env_var_api_key(self):
        os.environ["TEST_API_KEY"] = "sk-from-env-999"
        config = """
teams:
  - id: "team_env"
    name: "环境变量团队"
    monthly_budget: 50.0
    members:
      - id: "u1"
        name: "用户"
        role: admin
        api_key: "${TEST_API_KEY}"
        daily_limit: 10
roles:
  admin:
    can_use_depth: [quick]
rate_limits:
  requests_per_second: 1
  burst_size: 1
  cooldown_seconds: 1
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            f.write(config)
            path = f.name

        try:
            auth = AuthManager(path)
            user = auth.authenticate("sk-from-env-999")
            assert user.user_name == "用户"
        finally:
            os.unlink(path)
            del os.environ["TEST_API_KEY"]
