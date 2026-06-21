"""
团队鉴权模块 — 多租户隔离、角色权限、速率限制
==============================================
使用方式:
    from .auth import AuthManager

    auth = AuthManager("config/team.yaml")
    user = auth.authenticate(api_key)
    auth.authorize(user, "standard")  # 校验角色是否有权使用该深度
    auth.check_rate_limit(user.tenant_id)  # 速率限制
"""

import os
import time
import hashlib
import hmac
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from collections import defaultdict

import yaml  # PyYAML


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class TeamUser:
    """经过鉴权的用户上下文"""
    user_id: str
    user_name: str
    tenant_id: str
    role: str                        # admin / analyst / viewer
    default_depth: str               # quick / standard / deep
    daily_limit: int                 # 日调用上限
    output_locale: str               # zh / en
    allowed_depths: List[str] = field(default_factory=list)
    can_export_charts: bool = False
    can_configure: bool = False


class AuthError(Exception):
    """鉴权异常"""
    pass


class RateLimitError(Exception):
    """速率限制异常"""
    pass


class QuotaExceededError(Exception):
    """配额超限异常"""
    pass


# ============================================================================
# 鉴权管理器
# ============================================================================

class AuthManager:
    """
    多租户鉴权管理器。

    支持:
    - API Key 验证（支持环境变量占位符 ${VAR}）
    - 角色权限矩阵
    - 每日调用配额
    - 内存级速率限制（生产环境应换 Redis）

    使用示例:
        auth = AuthManager("config/team.yaml")
        user = auth.authenticate("sk-xxx")
        auth.authorize(user, "standard")
        auth.record_call(user)  # 调用完成后记录
    """

    def __init__(self, config_path: str):
        """
        加载团队配置文件。

        Args:
            config_path: team.yaml 的路径

        Raises:
            FileNotFoundError: 配置文件不存在
            ValueError: 配置格式错误
        """
        with open(config_path, "r", encoding="utf-8") as f:
            self._config = yaml.safe_load(f)

        # 构建查找索引: api_key → user
        self._key_index: Dict[str, TeamUser] = {}
        # 每日调用计数器: user_id → {date_str → count}
        self._daily_counts: Dict[str, Dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        # 速率限制: tenant_id → [request_timestamps]
        self._rate_buckets: Dict[str, List[float]] = defaultdict(list)
        # 每月用量: tenant_id → month_str → token_count
        self._monthly_usage: Dict[str, Dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )

        self._build_index()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def authenticate(self, api_key: str) -> TeamUser:
        """
        验证 API Key 并返回用户上下文。

        支持环境变量占位符: 配置中的 ${VAR} 会自动替换为 os.environ["VAR"]

        Args:
            api_key: 用户的 API Key

        Returns:
            TeamUser: 鉴权通过的用户上下文

        Raises:
            AuthError: API Key 无效
        """
        # 支持环境变量注入的 key
        resolved_key = self._resolve_env_var(api_key)

        if resolved_key not in self._key_index:
            raise AuthError("无效的 API Key")

        return self._key_index[resolved_key]

    def authorize(self, user: TeamUser, requested_depth: str) -> None:
        """
        校验用户是否有权限使用指定的分析深度。

        Args:
            user:            鉴权后的用户上下文
            requested_depth: 请求的分析深度

        Raises:
            AuthError: 权限不足
        """
        if requested_depth not in user.allowed_depths:
            raise AuthError(
                f"角色 '{user.role}' 不支持深度 '{requested_depth}'。"
                f"允许: {', '.join(user.allowed_depths)}"
            )

    def check_rate_limit(self, tenant_id: str) -> None:
        """
        检查团队速率限制。

        使用滑动窗口算法，清理过期记录。

        Args:
            tenant_id: 团队 ID

        Raises:
            RateLimitError: 超过速率限制
        """
        now = time.time()
        limits = self._config.get("rate_limits", {})
        rps = limits.get("requests_per_second", 5)
        burst = limits.get("burst_size", 10)
        window = limits.get("cooldown_seconds", 1)

        bucket = self._rate_buckets[tenant_id]

        # 清理 window 外的旧记录
        bucket[:] = [t for t in bucket if now - t < window]

        if len(bucket) >= burst:
            raise RateLimitError(
                f"团队 '{tenant_id}' 速率超限 ({burst} req/{window}s)。请稍后重试"
            )

        bucket.append(now)

    def check_daily_quota(self, user: TeamUser) -> None:
        """
        检查用户日调用配额。

        Args:
            user: 用户上下文

        Raises:
            QuotaExceededError: 超过日配额
        """
        today = time.strftime("%Y-%m-%d")
        count = self._daily_counts[user.user_id][today]

        if count >= user.daily_limit:
            raise QuotaExceededError(
                f"用户 '{user.user_name}' 已达今日配额 ({user.daily_limit} 次)。"
                f"请明日再试或联系管理员"
            )

    def record_call(
        self,
        user: TeamUser,
        depth: str,
        token_count: int = 0,
        status: str = "success",
    ) -> None:
        """
        记录一次调用（用于用量统计）。

        Args:
            user:       用户上下文
            depth:      使用的分析深度
            token_count: 消耗的 token 数
            status:     调用结果
        """
        today = time.strftime("%Y-%m-%d")
        month = time.strftime("%Y-%m")

        # 日计数
        self._daily_counts[user.user_id][today] += 1

        # 月用量（token）
        self._monthly_usage[user.tenant_id][month] += token_count

    def get_usage_report(self, tenant_id: str) -> Dict[str, Any]:
        """
        获取团队用量报告。

        Args:
            tenant_id: 团队 ID

        Returns:
            {today_count, month_token, month_budget, budget_pct, members}
        """
        today = time.strftime("%Y-%m-%d")
        month = time.strftime("%Y-%m")

        # 团队月度预算
        team = self._get_team(tenant_id)
        budget = team.get("monthly_budget", 200.0) if team else 200.0

        # 本月 token
        month_tokens = self._monthly_usage.get(tenant_id, {}).get(month, 0)

        # 今日调用
        today_total = sum(
            counts.get(today, 0)
            for counts in self._daily_counts.values()
        )

        # 成员用量
        members = []
        for member in (team.get("members", []) if team else []):
            uid = member["id"]
            members.append({
                "name": member["name"],
                "role": member["role"],
                "today": self._daily_counts.get(uid, {}).get(today, 0),
                "limit": member.get("daily_limit", 0),
            })

        return {
            "tenant_id": tenant_id,
            "today_calls": today_total,
            "month_tokens": month_tokens,
            "month_budget_usd": budget,
            "budget_usage_pct": round(month_tokens / 15_000 * 100, 1) if budget > 0 else 0,
            "members": members,
        }

    def list_tenants(self) -> List[str]:
        """列出所有团队 ID"""
        return [t["id"] for t in self._config.get("teams", [])]

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _build_index(self) -> None:
        """构建 api_key → user 快速查找索引"""
        for team in self._config.get("teams", []):
            role_configs = self._config.get("roles", {})
            for member in team.get("members", []):
                raw_key = member.get("api_key", "")
                resolved_key = self._resolve_env_var(raw_key)
                role = member.get("role", "viewer")
                role_cfg = role_configs.get(role, {})

                user = TeamUser(
                    user_id=member["id"],
                    user_name=member["name"],
                    tenant_id=team["id"],
                    role=role,
                    default_depth=member.get("default_depth", "standard"),
                    daily_limit=member.get("daily_limit", 50),
                    output_locale=member.get("output_locale", "zh"),
                    allowed_depths=role_cfg.get("can_use_depth", ["quick"]),
                    can_export_charts=role_cfg.get("can_export_charts", False),
                    can_configure=role_cfg.get("can_configure", False),
                )
                self._key_index[resolved_key] = user

    @staticmethod
    def _resolve_env_var(value: str) -> str:
        """
        解析环境变量占位符 ${VAR_NAME}。

        Args:
            value: 可能包含 ${VAR} 的字符串

        Returns:
            替换后的字符串
        """
        if not isinstance(value, str):
            return value
        if value.startswith("${") and value.endswith("}"):
            env_var = value[2:-1]
            return os.environ.get(env_var, value)
        return value

    def _get_team(self, tenant_id: str) -> Optional[Dict]:
        """查找团队配置"""
        for team in self._config.get("teams", []):
            if team["id"] == tenant_id:
                return team
        return None
