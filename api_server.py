"""
CSV 数据分析 Skill — HTTP API 服务
===================================
基于 FastAPI，提供 RESTful 接口。

启动:
    python api_server.py
    uvicorn api_server:app --host 0.0.0.0 --port 8080 --reload

端点:
    POST /analyze          — 分析 CSV（文件上传 或 JSON 内容直传）
    POST /analyze/batch    — 批量分析多个 CSV
    GET  /health           — 健康检查
    GET  /usage            — 团队用量（需 API Key）
"""

import os
import sys
import uuid
import time
import tempfile
import contextlib
import logging
from typing import Optional, List
from datetime import datetime
from collections import defaultdict

from fastapi import FastAPI, File, UploadFile, Form, Header, HTTPException, Body, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# 路径 — 兼容源码克隆和 pip install 两种部署方式
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.join(_PKG_DIR, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from graph_builder import GraphAnalyzer
from auth import AuthManager, AuthError, RateLimitError, QuotaExceededError
from tracker import UsageTracker

logger = logging.getLogger(__name__)


# ============================================================================
# 工具函数
# ============================================================================

@contextlib.contextmanager
def _managed_tempfile(upload_data: bytes, filename: str = "data.csv"):
    """
    自动清理的临时文件上下文管理器。

    确保即使分析过程中抛出异常，临时文件也会被删除。
    使用 os.fsync 保证数据刷写完成后再进行分析。

    Args:
        upload_data: 上传文件的字节内容
        filename:    原始文件名（用于提取后缀）

    Yields:
        str: 临时文件的绝对路径
    """
    suffix = os.path.splitext(filename)[1] or ".csv"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = tmp.name
    try:
        tmp.write(upload_data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        yield tmp_path
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass  # 已被其他机制清理
        except OSError as e:
            logger.warning(f"清理临时文件失败 {tmp_path}: {e}")


# ============================================================================
# 应用初始化
# ============================================================================

app = FastAPI(
    title="CSV Data Analysis Skill API",
    version="2.0.0",
    description="基于 LangGraph 的 CSV 数据分析服务 — 上传 CSV 即得结构化分析报告",
)

# CORS 配置 — 仅允许白名单域名，禁止通配符
ALLOWED_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:3000,http://localhost:5173"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS if o.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key"],
    max_age=3600,
)

# 分析器（无鉴权模式，单实例）
_analyzer = GraphAnalyzer(interrupt_on_ambiguity=False)

# 团队模式（可选启用）
_auth: Optional[AuthManager] = None
_tracker: Optional[UsageTracker] = None

CONFIG_DIR = os.path.join(_PKG_DIR, "config")


def init_team_mode():
    """启用团队鉴权 + 用量追踪"""
    global _auth, _tracker
    config_path = os.path.join(CONFIG_DIR, "team.yaml")
    log_path = os.path.join(_PKG_DIR, "logs", "usage.jsonl")
    if os.path.exists(config_path):
        _auth = AuthManager(config_path)
        _tracker = UsageTracker(log_path)


init_team_mode()


# ============================================================================
# 请求/响应模型
# ============================================================================

class ContentRequest(BaseModel):
    """CSV 内容直传请求"""
    content: str = Field(..., description="CSV 文本内容", min_length=1)
    depth: str = Field(default="standard", pattern="^(quick|standard|deep)$")
    column_hints: Optional[dict] = Field(default=None, description="手动列语义标注")


class BatchRequest(BaseModel):
    """批量分析请求"""
    files_content: List[ContentRequest] = Field(..., max_length=20)


class AnalyzeResponse(BaseModel):
    """分析响应"""
    execution_id: str
    status: str
    elapsed_seconds: float
    file_info: dict
    quality_score: Optional[int]
    insights_count: int
    analysis_count: int
    executive_summary: str
    # 完整的分析结果（可选）
    full_result: Optional[dict] = None


class HealthResponse(BaseModel):
    status: str = "healthy"
    version: str = "2.0.0"
    team_mode: bool = False


# ============================================================================
# 速率限制 — 滑动窗口 IP 级别限流
# ============================================================================

_ip_buckets: dict[str, list[float]] = defaultdict(list)
MAX_RPS = int(os.getenv("RATE_LIMIT_RPS", "10"))         # 每秒最大请求数
RATE_WINDOW_SECONDS = float(os.getenv("RATE_WINDOW", "1.0"))  # 窗口大小

# 定期清理：每 1000 次请求清理一次过期 IP 记录，防止内存泄漏
_request_counter = 0
CLEANUP_INTERVAL = 1000


def _cleanup_stale_buckets():
    """清理过期 IP 记录，防止内存泄漏"""
    now = time.time()
    cutoff = now - RATE_WINDOW_SECONDS * 2
    stale = [ip for ip, bucket in _ip_buckets.items()
             if not bucket or max(bucket) < cutoff]
    for ip in stale:
        del _ip_buckets[ip]


def _check_ip_rate(ip: str):
    """
    滑动窗口 IP 限流。

    每次请求记录时间戳，仅统计窗口内的请求数。
    超限返回 429 Too Many Requests 配合 Retry-After 头。

    Args:
        ip: 客户端 IP 地址

    Raises:
        HTTPException(429): 速率超限
    """
    global _request_counter
    _request_counter += 1

    # 定期清理过期记录
    if _request_counter % CLEANUP_INTERVAL == 0:
        _cleanup_stale_buckets()

    now = time.time()
    bucket = _ip_buckets[ip]
    cutoff = now - RATE_WINDOW_SECONDS

    # 清理窗口外的旧记录
    _ip_buckets[ip] = [t for t in bucket if t > cutoff]

    if len(_ip_buckets[ip]) >= MAX_RPS:
        oldest = min(_ip_buckets[ip])
        retry_after = max(1, int(RATE_WINDOW_SECONDS - (now - oldest)))
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers={"Retry-After": str(retry_after)}
        )

    _ip_buckets[ip].append(now)


# ============================================================================
# 端点
# ============================================================================

@app.get("/health", response_model=HealthResponse)
async def health():
    """健康检查"""
    return HealthResponse(team_mode=_auth is not None)


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(
    request: Request,
    # 方式 A: 文件上传
    file: Optional[UploadFile] = File(None),
    # 方式 B: 内容直传
    content: Optional[str] = Form(None),
    depth: str = Form("standard"),
    column_hints: Optional[str] = Form(None),
    include_full: bool = Form(False),
    # 团队鉴权（可选）
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """
    分析 CSV 数据 — 支持文件上传和内容直传两种方式。

    示例:
        curl -X POST http://localhost:8080/analyze \
             -F "file=@sales.csv" \
             -F "depth=standard"

        curl -X POST http://localhost:8080/analyze \
             -F "content=date,revenue\n2025-01-01,1000" \
             -F "depth=quick"
    """
    # IP 级别速率限制（无鉴权模式下的第一道防线）
    client_ip = request.client.host if request.client else "unknown"
    _check_ip_rate(client_ip)

    hints = None
    if column_hints:
        import json
        hints = json.loads(column_hints)

    # 团队鉴权
    if _auth and x_api_key:
        try:
            user = _auth.authenticate(x_api_key)
            _auth.authorize(user, depth)
            _auth.check_rate_limit(user.tenant_id)
            _auth.check_daily_quota(user)
        except AuthError as e:
            logger.warning(f"鉴权失败: {e}")
            raise HTTPException(401, str(e))
        except RateLimitError as e:
            logger.warning(f"速率限制: {e}")
            raise HTTPException(429, str(e))
        except QuotaExceededError as e:
            logger.warning(f"配额超限: {e}")
            raise HTTPException(429, str(e))

    # 校验输入互斥
    if file and content:
        raise HTTPException(400, "file 和 content 二选一，不能同时提供")
    if not file and not content:
        raise HTTPException(400, "必须提供 file 或 content")

    try:
        start = time.time()

        if file:
            # 保存上传文件到临时目录（自动清理）
            with _managed_tempfile(await file.read(), file.filename) as tmp_path:
                result = _analyzer.run(
                    file_path=tmp_path,
                    depth=depth,
                    column_hints=hints,
                )
        else:
            result = _analyzer.run(
                content=content,
                depth=depth,
                column_hints=hints,
            )

        elapsed = time.time() - start

        # 用量追踪
        if _auth and _tracker and x_api_key:
            user = _auth.authenticate(x_api_key)
            _auth.record_call(user, depth, 5000, result.get("status", "success"))
            _tracker.log(
                tenant_id=user.tenant_id, user_id=user.user_id,
                user_name=user.user_name, depth=depth,
                model="claude-sonnet-4-6",
                token_input=5600, token_output=2800,
                status=result.get("status", "error"),
                elapsed_seconds=round(elapsed, 1),
            )

        # 构建响应
        quality = result.get("quality_report") or result.get("quality", {})
        response = AnalyzeResponse(
            execution_id=uuid.uuid4().hex[:12],
            status=result.get("status", "error"),
            elapsed_seconds=round(elapsed, 2),
            file_info={
                "name": file.filename if file else "(inline)",
                "rows": result.get("parse_meta", {}).get("row_count", 0),
                "cols": result.get("parse_meta", {}).get("col_count", 0),
            },
            quality_score=quality.get("score"),
            insights_count=len(result.get("insights", [])),
            analysis_count=len(result.get("analysis_results", [])),
            executive_summary=result.get("executive_summary", ""),
            full_result=result if include_full else None,
        )

        status_code = 200 if result.get("status") != "error" else 422
        return JSONResponse(content=response.model_dump(), status_code=status_code)

    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        request_id = uuid.uuid4().hex[:8]
        logger.error(f"[{request_id}] 分析失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"分析服务内部错误，请联系管理员 (ID: {request_id})"
        )


@app.post("/analyze/batch")
async def analyze_batch(
    requests: BatchRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """
    批量分析多个 CSV 内容。

    示例:
        curl -X POST http://localhost:8080/analyze/batch \
             -H "Content-Type: application/json" \
             -d '{"files_content": [{"content": "a,b\n1,2", "depth": "quick"}]}'
    """
    results = []
    for req in requests.files_content:
        try:
            r = _analyzer.run(content=req.content, depth=req.depth, column_hints=req.column_hints)
            results.append({"status": r.get("status"), "summary": r.get("executive_summary", "")})
        except Exception as e:
            request_id = uuid.uuid4().hex[:8]
            logger.error(f"[{request_id}] 批量分析子任务失败: {e}", exc_info=True)
            results.append({
                "status": "error",
                "summary": f"分析失败 (ID: {request_id})"
            })
    return {"total": len(requests.files_content), "results": results}


@app.get("/usage")
async def get_usage(x_api_key: str = Header(..., alias="X-API-Key")):
    """
    查询团队用量（需管理员 API Key）。
    """
    if not _auth:
        raise HTTPException(501, "团队模式未启用")
    try:
        return _auth.get_usage_report(_auth.authenticate(x_api_key).tenant_id)
    except AuthError as e:
        logger.warning(f"用量查询鉴权失败: {e}")
        raise HTTPException(403, str(e))


# ============================================================================
# 启动
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    print(f"[API] 启动 CSV 数据分析服务...")
    print(f"   安装目录: {_PKG_DIR}")
    print(f"   团队模式: {'启用' if _auth else '关闭'}")
    print(f"   端点: http://localhost:8080")
    print(f"   文档: http://localhost:8080/docs")
    uvicorn.run(app, host="0.0.0.0", port=8080)
