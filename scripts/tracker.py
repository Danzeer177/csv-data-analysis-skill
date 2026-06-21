"""
用量追踪模块 — 调用记录、成本核算、用量报告
============================================
使用方式:
    from .tracker import UsageTracker

    tracker = UsageTracker("logs/usage.jsonl")
    tracker.log(tenant_id, user_id, depth, token_in, token_out, status, elapsed)
    tracker.daily_report("team_default")
"""

import os
import json
import time
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict
from collections import defaultdict


# ============================================================================
# 定价表 (2026年6月)
# ============================================================================

PRICING = {
    "claude-opus-4-8":   {"input": 15.0,  "output": 75.0},   # $/MTok
    "claude-sonnet-4-6": {"input": 3.0,   "output": 15.0},
    "claude-haiku-4-5":  {"input": 0.80,  "output": 4.0},
    "gpt-4o":            {"input": 2.5,   "output": 10.0},
    "deepseek-v3":       {"input": 0.27,  "output": 1.10},
}


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class CallRecord:
    """单次调用记录"""
    timestamp: str                          # ISO 格式
    tenant_id: str
    user_id: str
    user_name: str
    depth: str                              # quick / standard / deep
    model: str                              # 使用的模型
    token_input: int
    token_output: int
    cost_usd: float
    elapsed_seconds: float
    status: str                             # success / partial / error


@dataclass
class DailyReport:
    """日报"""
    date: str
    total_calls: int
    success_calls: int
    error_calls: int
    total_tokens: int
    total_cost_usd: float
    by_depth: Dict[str, int]               # depth → call count
    by_user: Dict[str, int]                # user_id → call count
    top_3_expensive: List[CallRecord]


@dataclass
class MonthlyReport:
    """月报"""
    month: str
    total_calls: int
    total_tokens: int
    total_cost_usd: float
    avg_cost_per_call: float
    avg_elapsed_seconds: float
    daily_breakdown: Dict[str, int]        # date → call count


# ============================================================================
# 用量追踪器
# ============================================================================

class UsageTracker:
    """
    用量追踪器 — JSONL 持久化 + 内存聚合。

    每条调用记录追加写入 JSONL 文件。
    日报/月报从 JSONL 聚合计算。

    使用示例:
        tracker = UsageTracker("logs/usage.jsonl")
        tracker.log("team_a", "u01", "standard", "claude-sonnet-4-6",
                     4000, 1100, "success", 8.3)
        report = tracker.daily_report("team_a")
    """

    def __init__(self, log_path: str = "logs/usage.jsonl"):
        """
        Args:
            log_path: JSONL 日志文件路径。自动创建目录

        Raises:
            ValueError: 日志路径不安全（绝对路径或包含 ..）
        """
        self._log_path = self._validate_log_path(log_path)
        os.makedirs(os.path.dirname(self._log_path), exist_ok=True)

    @staticmethod
    def _validate_log_path(log_path: str) -> str:
        """
        校验日志路径安全性，防止路径注入。

        规则:
            1. 拒绝包含 .. 的路径遍历
            2. 拒绝空字节注入
            3. 规范化后再次校验（纵深防御）

        注意: 允许绝对路径，因为 UsageTracker 由应用代码实例化，
        路径来自配置文件而非直接用户输入。

        Args:
            log_path: 日志路径

        Returns:
            规范化后的安全路径

        Raises:
            ValueError: 路径不安全
        """
        if not log_path:
            raise ValueError("日志路径不能为空")

        # 1. 拒绝空字节注入
        if "\x00" in log_path:
            raise ValueError(f"路径包含非法字符: {log_path}")

        # 2. 拒绝路径遍历
        if ".." in log_path.split(os.sep):
            raise ValueError(f"路径包含非法字符: {log_path}")

        # 3. 规范化后再次校验（纵深防御）
        normalized = os.path.normpath(log_path)
        if normalized.startswith(".."):
            raise ValueError(f"路径越界: {log_path}")

        return normalized

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def log(
        self,
        tenant_id: str,
        user_id: str,
        user_name: str,
        depth: str,
        model: str,
        token_input: int,
        token_output: int,
        status: str,
        elapsed_seconds: float,
    ) -> CallRecord:
        """
        记录一次调用。

        Args:
            tenant_id:       团队 ID
            user_id:         用户 ID
            user_name:       用户名称
            depth:           分析深度
            model:           使用的模型 ID
            token_input:     输入 token 数
            token_output:    输出 token 数
            status:          success / partial / error
            elapsed_seconds: 调用耗时

        Returns:
            CallRecord: 记录对象
        """
        # 计算成本
        pricing = PRICING.get(model, PRICING["claude-sonnet-4-6"])
        cost = (
            token_input / 1_000_000 * pricing["input"]
            + token_output / 1_000_000 * pricing["output"]
        )

        record = CallRecord(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            tenant_id=tenant_id,
            user_id=user_id,
            user_name=user_name,
            depth=depth,
            model=model,
            token_input=token_input,
            token_output=token_output,
            cost_usd=round(cost, 6),
            elapsed_seconds=round(elapsed_seconds, 1),
            status=status,
        )

        # 追加写入 JSONL
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

        return record

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def daily_report(self, tenant_id: str, date: Optional[str] = None) -> DailyReport:
        """
        生成日报。

        Args:
            tenant_id: 团队 ID
            date:      日期 YYYY-MM-DD。默认今天

        Returns:
            DailyReport
        """
        if date is None:
            date = time.strftime("%Y-%m-%d")

        records = self._read_records(tenant_id=tenant_id, date=date)

        by_depth = defaultdict(int)
        by_user = defaultdict(int)
        total_tokens = 0
        total_cost = 0.0
        success = 0
        errors = 0

        for r in records:
            by_depth[r.depth] += 1
            by_user[r.user_name] += 1
            total_tokens += r.token_input + r.token_output
            total_cost += r.cost_usd
            if r.status == "success":
                success += 1
            else:
                errors += 1

        # Top 3 最贵调用
        sorted_by_cost = sorted(records, key=lambda r: r.cost_usd, reverse=True)

        return DailyReport(
            date=date,
            total_calls=len(records),
            success_calls=success,
            error_calls=errors,
            total_tokens=total_tokens,
            total_cost_usd=round(total_cost, 4),
            by_depth=dict(by_depth),
            by_user=dict(by_user),
            top_3_expensive=sorted_by_cost[:3],
        )

    def monthly_report(self, tenant_id: str, month: Optional[str] = None) -> MonthlyReport:
        """
        生成月报。

        Args:
            tenant_id: 团队 ID
            month:     月份 YYYY-MM。默认当月

        Returns:
            MonthlyReport
        """
        if month is None:
            month = time.strftime("%Y-%m")

        records = self._read_records(tenant_id=tenant_id, month=month)

        if not records:
            return MonthlyReport(
                month=month, total_calls=0, total_tokens=0,
                total_cost_usd=0.0, avg_cost_per_call=0.0,
                avg_elapsed_seconds=0.0, daily_breakdown={},
            )

        total_tokens = 0
        total_cost = 0.0
        total_elapsed = 0.0
        daily = defaultdict(int)

        for r in records:
            total_tokens += r.token_input + r.token_output
            total_cost += r.cost_usd
            total_elapsed += r.elapsed_seconds
            daily[r.timestamp[:10]] += 1

        n = len(records)
        return MonthlyReport(
            month=month,
            total_calls=n,
            total_tokens=total_tokens,
            total_cost_usd=round(total_cost, 4),
            avg_cost_per_call=round(total_cost / n, 4),
            avg_elapsed_seconds=round(total_elapsed / n, 1),
            daily_breakdown=dict(sorted(daily.items())),
        )

    def get_user_stats(
        self, tenant_id: str, user_id: str, days: int = 7
    ) -> Dict[str, Any]:
        """
        获取单个用户近 N 天的统计。

        Args:
            tenant_id: 团队 ID
            user_id:   用户 ID
            days:      回溯天数

        Returns:
            {total_calls, total_tokens, total_cost, by_depth, by_date}
        """
        records = self._read_records(tenant_id=tenant_id, user_id=user_id, days=days)

        by_depth = defaultdict(int)
        by_date = defaultdict(int)
        total_tokens = 0
        total_cost = 0.0

        for r in records:
            by_depth[r.depth] += 1
            by_date[r.timestamp[:10]] += 1
            total_tokens += r.token_input + r.token_output
            total_cost += r.cost_usd

        return {
            "user_id": user_id,
            "days": days,
            "total_calls": len(records),
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 4),
            "by_depth": dict(by_depth),
            "by_date": dict(sorted(by_date.items())),
        }

    def budget_alert(
        self, tenant_id: str, budget_usd: float, threshold: float = 0.8
    ) -> Optional[str]:
        """
        预算预警。当月成本 > 预算 × threshold 时返回告警消息。

        Args:
            tenant_id:   团队 ID
            budget_usd:  月度预算上限
            threshold:   告警阈值 (0.0-1.0)

        Returns:
            告警消息字符串，未触发返回 None
        """
        month = time.strftime("%Y-%m")
        report = self.monthly_report(tenant_id, month)

        if report.total_cost_usd > budget_usd * threshold:
            pct = report.total_cost_usd / budget_usd * 100
            return (
                f"⚠️ 预算告警: 团队 '{tenant_id}' 本月已花费 "
                f"${report.total_cost_usd:.2f} / ${budget_usd:.2f} "
                f"({pct:.0f}%)。剩余 ${budget_usd - report.total_cost_usd:.2f}"
            )
        return None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _read_records(
        self,
        tenant_id: str,
        date: Optional[str] = None,
        month: Optional[str] = None,
        user_id: Optional[str] = None,
        days: Optional[int] = None,
    ) -> List[CallRecord]:
        """从 JSONL 读取并过滤记录"""
        if not os.path.exists(self._log_path):
            return []

        records = []
        cutoff = None
        if days:
            cutoff = time.strftime(
                "%Y-%m-%d",
                time.localtime(time.time() - days * 86400)
            )

        with open(self._log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = CallRecord(**json.loads(line))
                except (json.JSONDecodeError, TypeError):
                    continue

                # 过滤
                if r.tenant_id != tenant_id:
                    continue
                if date and r.timestamp[:10] != date:
                    continue
                if month and r.timestamp[:7] != month:
                    continue
                if user_id and r.user_id != user_id:
                    continue
                if cutoff and r.timestamp[:10] < cutoff:
                    continue

                records.append(r)

        return records
