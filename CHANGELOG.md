# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0] — 2026-06-21

### Added

- **12 项多维分析引擎**: 描述统计、时序趋势、同比对比、Top排名、ABC帕累托、异常检测、相关性矩阵、分布分析、区域分析、维度下钻、RFM客户分析、趋势预测
- **LangGraph 工作流编排**: 8 步分析流水线，Send API 并行扇出，条件路由
- **LLM 语义解读**: 批量解读模式（batch_narrate），N→1 次 LLM 调用，节省 ~81% token
- **Token 效率优化**: correlation O(N²) 上限保护（MAX_CORR_COLS=15）、时序降采样（>24期）、输出断路器（MAX_OUTPUT_TOKENS=8000）、slim_output 配置选项
- **安全纵深防御（6层）**: 路径沙箱（`_safe_path`）、魔数校验（`_verify_csv_magic`）、文件大小限制、IP 速率限制（滑动窗口+429）、错误脱敏（request_id）、CORS 白名单
- **多租户鉴权**: API Key 鉴权 + 角色权限（admin/analyst/viewer）+ 日配额 + 速率限制 + 用量追踪
- **三种部署模式**: Python 直接调用、FastAPI HTTP 服务、LangChain StructuredTool
- **101 项自动化测试**: 覆盖功能/安全/边界/集成，通过率 100%
- **完整文档体系**: skill.md、统计指标参考手册、USER_GUIDE、TEST_REPORT、安全审计方案

### Security

- SEC-01: 路径遍历攻击防护 — `_safe_path()` + `os.path.realpath()` + 前缀白名单
- SEC-02: 临时文件竞态修复 — `_managed_tempfile()` 上下文管理器 + `try/finally`
- SEC-03: 日志路径注入防护 — `_validate_log_path()` 拒绝 `..` 和空字节
- SEC-04: API 速率限制 — IP 级滑动窗口 + 429 + Retry-After + 定期清理
- SEC-05: 文件魔数校验 — 拒绝 ELF/PE/ZIP/PDF/JPEG/PNG 二进制
- SEC-06: 错误消息脱敏 — `request_id` + `exc_info=True`
- SEC-07: 内存数据清理 — `try/finally` 保证缓存释放
- SEC-08: 依赖版本锁定 — `requirements.lock.txt`
- SEC-09: CORS 白名单 — `CORSMiddleware` + 环境变量配置

---

## Types of changes

- `Added` for new features
- `Changed` for changes in existing functionality
- `Deprecated` for soon-to-be removed features
- `Removed` for now removed features
- `Fixed` for any bug fixes
- `Security` in case of vulnerabilities
