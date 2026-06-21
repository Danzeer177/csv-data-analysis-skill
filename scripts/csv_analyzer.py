"""
CSV 数据分析器 —— 脚本层
=========================
本模块是「CSV 数据分析 Skill」的计算核心。遵循「Pandas 精确计算 + LLM 语义解读」的架构原则：
所有数值计算 100% 由 Python 完成，LLM 仅负责自然语言解读和洞察生成。

指标定义遵循 `references/分析流程指南.md`（操作指令）和 `references/统计指标参考手册.md`（完整公式）。
每个分析函数的 metrics 字段名、计算公式与判读标准均以上述参考文档为准。

模块结构:
    CsvAnalyzer      — 主分析器类，编排完整的分析流水线
    analyze_csv      — 快捷函数：分析 CSV 文件并返回完整结果
    quick_summary    — 快捷函数：分析 CSV 文件并仅返回摘要

设计原则:
    1. 计算与解读分离：metrics dict (Python) + narration (LLM)
    2. 原始数据不可变：raw_df 只读，所有变换在 cleaned_df 副本
    3. 容错优先：单个分析失败不影响其他分析
    4. 可追溯：每个结论可回溯到具体的 metrics 数据点

依赖:
    pandas, numpy, scipy, matplotlib, seaborn, chardet, pydantic

版本: 1.0.0
"""

import io
import os
import json
import uuid
import time
import logging
import warnings
from typing import Optional, List, Dict, Any, Tuple, Literal, Union
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

# ============================================================================
# 可选依赖 — 按需导入，缺失时降级而非崩溃
# ============================================================================

try:
    import chardet
    HAS_CHARDET = True
except ImportError:
    HAS_CHARDET = False

try:
    import matplotlib
    matplotlib.use("Agg")  # 非交互后端，无需 GUI
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

try:
    import seaborn as sns
    HAS_SEABORN = True
except ImportError:
    HAS_SEABORN = False

try:
    from scipy import stats as scipy_stats
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

# 抑制 Pandas 警告
warnings.filterwarnings("ignore", category=FutureWarning)
try:
    warnings.filterwarnings("ignore", category=pd.errors.SettingWithCopyWarning)
except AttributeError:
    pass  # SettingWithCopyWarning 在新版 pandas 中已移除

logger = logging.getLogger(__name__)


# ============================================================================
# 常量定义
# ============================================================================

# 列语义角色枚举
COLUMN_ROLES = [
    "date",           # 日期/时间列
    "revenue",        # 收入/销售额
    "quantity",       # 数量
    "unit_price",     # 单价
    "cost",           # 成本
    "profit",         # 利润
    "product_id",     # 产品标识
    "product_name",   # 产品名称
    "category",       # 分类
    "region",         # 区域
    "customer_id",    # 客户标识
    "customer_name",  # 客户名称
    "channel",        # 渠道
    "metric",         # 通用指标（无法精确归类的数值列）
    "ignore",         # 忽略（备注/UUID/全空列）
]

# 候选 CSV 分隔符，按常用程度排序
CANDIDATE_DELIMITERS = [",", "\t", ";", "|"]

# 编码检测回退顺序
FALLBACK_ENCODINGS = ["utf-8", "gbk", "gb2312", "utf-16", "latin-1"]

# 数据质量评分权重
QUALITY_WEIGHTS = {
    "null_penalty_per_column": 10,    # 缺失率>20% 每列扣10分
    "outlier_penalty_per_column": 5,  # 异常率>5% 每列扣5分
    "duplicate_penalty_factor": 50,   # 重复行率 × 50
}

# 分析深度 → 最大任务数
DEPTH_TASK_LIMITS = {"quick": 3, "standard": 6, "deep": 10}

# 行数分级策略
ROW_LIMITS = {
    "full_analysis": 10_000,          # ≤此值全量分析
    "chart_downsample": 50_000,       # ≤此值全量分析但图表降采样
    "chart_sample": 100_000,          # ≤此值分析全量但图表抽样20k
    "max_rows": 100_000,              # 绝对上限
}
MAX_FILE_SIZE_MB = 100
MAX_CONTENT_SIZE_MB = 10

# Token 效率保护 — 防止 O(N²) 爆炸和输出溢出
MAX_CORR_COLS = 15           # 数值列超过此值时不返回完整矩阵，仅 strong_pairs
MAX_TIMESERIES_PERIODS = 24  # 月度数据超过此值时降采样
MAX_OUTPUT_TOKENS = 8_000    # 输出总 token 断路器

# 文件魔数校验 — 拒绝明显的二进制/可执行文件
_DANGEROUS_MAGICS = {
    b"\x7fELF": "ELF 可执行文件",
    b"MZ":      "Windows PE 可执行文件",
    b"\xff\xd8\xff": "JPEG 图片",
    b"\x89PNG":  "PNG 图片",
    b"PK\x03\x04": "ZIP 压缩包",
    b"%PDF":    "PDF 文档",
}


# ============================================================================
# Pydantic 数据模型 — 定义输入输出的结构化契约
# ============================================================================

class CsvDataSource(BaseModel):
    """
    CSV 数据源定义。
    file_path 和 content 二选一：
    - file_path: 从磁盘读取 CSV/Excel 文件
    - content:   内存中的 CSV 字符串（Agent 间传递无需落盘）
    """
    file_path: Optional[str] = Field(
        default=None,
        description="CSV/Excel 文件的本地绝对路径。与 content 二选一"
    )
    content: Optional[str] = Field(
        default=None,
        description="CSV 文本内容（内存字符串）。与 file_path 二选一"
    )

    def validate_exclusivity(self) -> Tuple[bool, str]:
        """校验 file_path 和 content 互斥"""
        has_path = self.file_path is not None
        has_content = self.content is not None
        if has_path == has_content:
            return False, "必须且仅提供 file_path 或 content 其中之一"
        return True, "ok"


class AnalysisConfig(BaseModel):
    """
    分析配置参数。
    所有字段均为可选，未指定时使用默认值。
    """
    delimiter: Optional[str] = Field(
        default=None,
        description="CSV 分隔符。不指定则自动检测"
    )
    encoding: Optional[str] = Field(
        default=None,
        description="文件编码。不指定则自动检测"
    )
    has_header: bool = Field(
        default=True,
        description="第一行是否为表头"
    )
    skip_rows: Optional[int] = Field(
        default=None,
        description="跳过数据前 N 行"
    )
    comment_char: Optional[str] = Field(
        default=None,
        description="注释行前缀（如 '#'）"
    )
    analysis_depth: Literal["quick", "standard", "deep"] = Field(
        default="standard",
        description="分析深度：quick(≤3任务) | standard(≤6任务) | deep(≤10任务)"
    )
    focus_questions: Optional[List[str]] = Field(
        default=None,
        description="用户具体业务问题列表"
    )
    date_range: Optional[Tuple[str, str]] = Field(
        default=None,
        description="限定分析时间范围 (start, end)，格式 YYYY-MM-DD"
    )
    column_hints: Optional[Dict[str, str]] = Field(
        default=None,
        description="手动列语义标注，如 {'下单日期': 'date', '金额': 'revenue'}"
    )
    output_locale: Literal["zh", "en"] = Field(
        default="zh",
        description="输出语言"
    )
    excel_sheet: Optional[str] = Field(
        default=None,
        description="[仅 Excel] 目标工作表名称"
    )
    chart_dir: Optional[str] = Field(
        default=None,
        description="图表输出目录。不指定则不生成图表"
    )
    slim_output: bool = Field(
        default=False,
        description="裁剪输出：去掉完整矩阵、时序明细、top10列表，仅保留聚合值。适合上下文敏感场景"
    )


# ============================================================================
# 数据类 — 分析结果的数据容器
# ============================================================================

@dataclass
class ParseMeta:
    """CSV 解析元信息"""
    delimiter: Optional[str] = None
    encoding: Optional[str] = None
    row_count: int = 0
    col_count: int = 0
    date_range: Optional[Tuple[str, str]] = None


@dataclass
class SourceInfo:
    """数据来源信息"""
    type: Literal["csv_file", "csv_inline", "excel"] = "csv_file"
    file_name: Optional[str] = None
    file_size_mb: Optional[float] = None


@dataclass
class ColumnStats:
    """单列的统计画像"""
    name: str
    dtype: str
    null_count: int
    null_rate: float
    unique_count: int
    sample_values: List[Any] = field(default_factory=list)
    # 数值列专有
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    mean_val: Optional[float] = None
    median_val: Optional[float] = None
    std_val: Optional[float] = None
    q1_val: Optional[float] = None
    q3_val: Optional[float] = None
    skew_val: Optional[float] = None
    zero_rate: Optional[float] = None
    # 文本列专有
    avg_length: Optional[float] = None
    top_values: Optional[List[Dict]] = None
    # 日期列专有
    time_span_days: Optional[int] = None
    is_continuous: Optional[bool] = None


@dataclass
class ColumnRole:
    """列语义角色"""
    role: str
    confidence: float
    reason: str = ""


@dataclass
class CleaningAction:
    """单次清洗操作的记录"""
    column: str
    action: str           # filled_na / marked_outlier / type_converted
    affected_rows: int
    description: str


@dataclass
class QualityReport:
    """数据质量报告"""
    score: int
    null_summary: Dict[str, float]
    outlier_summary: Dict[str, int]
    duplicate_rows: int
    cleaning_actions: List[CleaningAction] = field(default_factory=list)


@dataclass
class SubAnalysisResult:
    """单个子分析的结果"""
    task_id: str
    status: Literal["success", "partial", "skipped", "error"] = "success"
    metrics: Dict[str, Any] = field(default_factory=dict)
    narration: str = ""
    chart_path: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class Insight:
    """一条综合洞察"""
    id: str
    title: str
    severity: Literal["critical", "warning", "info"] = "info"
    evidence: List[str] = field(default_factory=list)
    interpretation: str = ""


@dataclass
class AnalysisOutput:
    """
    顶层分析输出。
    此结构直接返回给 Host Agent，包含分析结果的全部信息。
    """
    status: Literal["success", "partial", "error"] = "success"
    skill_version: str = "1.0.0"
    execution_id: str = ""
    elapsed_seconds: float = 0.0

    source_info: Optional[SourceInfo] = None
    parse_meta: Optional[ParseMeta] = None
    data_profile: List[ColumnStats] = field(default_factory=list)
    quality: Optional[QualityReport] = None
    column_roles: Dict[str, ColumnRole] = field(default_factory=dict)
    analyses: List[SubAnalysisResult] = field(default_factory=list)
    insights: List[Insight] = field(default_factory=list)
    answers: Optional[List[Dict]] = None
    charts: List[Dict] = field(default_factory=list)
    executive_summary: str = ""
    data_facts: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """将输出转为字典（供 JSON 序列化）"""
        return {
            "status": self.status,
            "skill_version": self.skill_version,
            "execution_id": self.execution_id,
            "elapsed_seconds": self.elapsed_seconds,
            "source_info": asdict(self.source_info) if self.source_info else None,
            "parse_meta": asdict(self.parse_meta) if self.parse_meta else None,
            "data_profile": [asdict(c) for c in self.data_profile],
            "quality": asdict(self.quality) if self.quality else None,
            "column_roles": {
                k: asdict(v) for k, v in self.column_roles.items()
            },
            "analyses": [asdict(a) for a in self.analyses],
            "insights": [asdict(i) for i in self.insights],
            "answers": self.answers,
            "charts": self.charts,
            "executive_summary": self.executive_summary,
            "data_facts": self.data_facts,
        }


# ============================================================================
# 工具函数 — 编码与分隔符检测
# ============================================================================

def _safe_path(user_path: str, base_dir: str = ".") -> str:
    """
    规范化路径并检查是否在允许目录内，防止路径遍历攻击。

    策略:
        1. 使用 os.path.realpath() 解析符号链接和相对路径
        2. 校验解析后的真实路径必须在 base_dir 子树内
        3. 追加 os.sep 防止前缀匹配绕过（如 /data/uploads_evil）

    Args:
        user_path: 用户提供的文件路径
        base_dir:  允许访问的基础目录，默认为当前工作目录

    Returns:
        规范化后的安全绝对路径

    Raises:
        ValueError: 路径越界或包含非法字符
    """
    if not user_path or "\x00" in user_path:
        raise ValueError(f"路径包含非法字符: {user_path}")

    base_real = os.path.realpath(base_dir)
    target_real = os.path.realpath(user_path)

    # 前缀校验：目标路径必须以 base_dir + 分隔符开头
    if not target_real.startswith(base_real + os.sep) and target_real != base_real:
        raise ValueError(f"路径越界: {user_path}")

    return target_real


def _verify_csv_magic(file_path: str) -> bool:
    """
    通过魔数校验拒绝明显的非文本/二进制文件。

    策略:
        1. 读取文件头部 8 字节
        2. 与已知危险文件魔数比对
        3. 匹配到任何危险魔数则拒绝

    Args:
        file_path: 已通过 _safe_path() 校验的安全路径

    Returns:
        True 如果通过校验

    Raises:
        ValueError: 文件内容被识别为危险类型
    """
    try:
        with open(file_path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False

    for magic, file_type in _DANGEROUS_MAGICS.items():
        if head.startswith(magic):
            raise ValueError(f"文件内容被识别为 {file_type}，拒绝处理")

    # 额外检查：拒绝纯空字节和全二进制内容
    if head == b"\x00" * min(8, len(head)):
        raise ValueError("文件内容为空字节，拒绝处理")

    return True


def detect_encoding(file_path: str) -> str:
    """
    检测文本文件的字符编码。

    策略:
        1. 读取文件前 10KB 字节
        2. 使用 chardet 库进行统计推断
        3. chardet 不可用时，按优先级依次尝试常见编码

    Args:
        file_path: 文件的绝对路径

    Returns:
        检测到的编码名称，如 'utf-8', 'gbk'

    Raises:
        UnicodeDecodeError: 所有编码尝试均失败
    """
    if HAS_CHARDET:
        with open(file_path, "rb") as f:
            raw_data = f.read(10240)
        result = chardet.detect(raw_data)
        encoding = result.get("encoding", "utf-8")
        confidence = result.get("confidence", 0)
        # 置信度低于 0.7 时，退回到常见中文编码
        if confidence < 0.7 and encoding and "gb" in encoding.lower():
            return "gbk"
        return encoding or "utf-8"
    else:
        # 无 chardet 时，依次尝试回退列表
        for enc in FALLBACK_ENCODINGS:
            try:
                with open(file_path, "r", encoding=enc) as f:
                    f.read(1024)
                return enc
            except (UnicodeDecodeError, LookupError):
                continue
        raise UnicodeDecodeError(
            "unknown", b"", 0, 1,
            "无法检测文件编码，请手动指定 encoding 参数"
        )


def detect_delimiter(text_sample: str) -> str:
    """
    检测 CSV 文本的分隔符。

    策略:
        1. 取文本的前 4096 个字符
        2. 对每种候选分隔符（逗号/制表符/分号/竖线）：
           - 统计每行的出现次数
           - 如果所有非空行的出现次数一致（容差 ±1）且 > 0，
             则该分隔符是候选
        3. 返回第一个匹配的候选分隔符

    Args:
        text_sample: CSV 文本样本

    Returns:
        检测到的分隔符，默认返回 ","
    """
    lines = text_sample[:4096].splitlines()
    lines = [l for l in lines if l.strip()]  # 排除空行
    if not lines:
        return ","

    best_delimiter = ","
    best_score = 0

    for delim in CANDIDATE_DELIMITERS:
        counts = [line.count(delim) for line in lines if line.strip()]
        if not counts:
            continue
        # 所有行的分隔符数量应一致（或因为截断而有微小差异）
        median_count = int(np.median(counts))
        if median_count == 0:
            continue
        consistent = sum(1 for c in counts if c == median_count)
        score = consistent / len(counts)
        if score > best_score and median_count >= 1:
            best_score = score
            best_delimiter = delim

    return best_delimiter


def detect_delimiter_from_file(file_path: str, encoding: str) -> str:
    """
    从文件读取前 4KB 并检测分隔符。

    Args:
        file_path: 文件路径
        encoding:  文件编码

    Returns:
        检测到的分隔符
    """
    with open(file_path, "r", encoding=encoding, errors="replace") as f:
        sample = f.read(4096)
    return detect_delimiter(sample)


# ============================================================================
# 主分析器类
# ============================================================================

class CsvAnalyzer:
    """
    CSV 数据分析器 —— 编排完整的分析流水线。

    使用方式:
        analyzer = CsvAnalyzer(config=AnalysisConfig(analysis_depth="standard"))
        output = analyzer.run(data=CsvDataSource(file_path="/path/to/data.csv"))

    或直接使用模块级快捷函数:
        output = analyze_csv("/path/to/data.csv")

    内部状态:
        raw_df       — 加载后的原始 DataFrame（只读，不修改）
        cleaned_df   — 清洗后的 DataFrame（所有分析基于此）
        profile      — 每列的统计画像列表
        roles        — 列名 → 语义角色映射
    """

    def __init__(self, config: Optional[AnalysisConfig] = None, llm: Any = None):
        """
        初始化分析器。

        Args:
            config: 分析配置。None 时使用默认值（标准深度、中文输出）
            llm:    可选的 LLM 客户端（用于 batch_narrate 批量解读）。
                    不传则使用 f-string 模板生成 narration，不消耗 LLM token。
        """
        self.config = config or AnalysisConfig()
        self._execution_id = uuid.uuid4().hex[:12]
        self._start_time: float = 0.0
        self.llm = llm  # 可选的 LLM 客户端

        # 内部状态 — 在 run() 中逐步填充
        self.raw_df: Optional[pd.DataFrame] = None
        self.cleaned_df: Optional[pd.DataFrame] = None
        self.profile: List[ColumnStats] = []
        self.roles: Dict[str, ColumnRole] = {}
        self.quality: Optional[QualityReport] = None
        self.analyses: List[SubAnalysisResult] = []
        self.insights: List[Insight] = []
        self.charts: List[Dict] = []

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def run(self, data: CsvDataSource) -> AnalysisOutput:
        """
        执行完整的分析流水线。

        Args:
            data: 数据源（文件路径或内存 CSV 字符串）

        Returns:
            AnalysisOutput: 结构化分析结果
        """
        self._start_time = time.time()
        output = AnalysisOutput(execution_id=self._execution_id)

        try:
            # Step 1: 加载与校验
            source_info, parse_meta = self._load(data)
            self.parse_meta = parse_meta  # 同步到实例，供 _build_* 方法使用
            output.source_info = source_info
            output.parse_meta = parse_meta

            # Step 2: 数据画像
            self.profile = self._build_profile()
            output.data_profile = self.profile

            # Step 3: 列语义识别
            self.roles = self._infer_roles(data)
            output.column_roles = self.roles

            # Step 4: 数据清洗
            self.cleaned_df, self.quality = self._clean()
            output.quality = self.quality

            # Step 5: 多维分析
            self.analyses = self._run_analyses(data)
            output.analyses = self.analyses

            # Step 6: 综合洞察
            self.insights = self._synthesize_insights(data)
            output.insights = self.insights

            # Step 7: 生成摘要（替代 LLM 的 executive_summary 和 data_facts）
            output.executive_summary = self._build_executive_summary()
            output.data_facts = self._build_data_facts()

            # Step 8: 记录耗时
            output.elapsed_seconds = round(time.time() - self._start_time, 1)
            output.status = "success"

            # Step 9: slim_output 裁剪 + 输出断路器
            if self.config.slim_output:
                output = self._apply_slim_output(output)
            output = self._check_output_size(output)

        except Exception as exc:
            output.status = "error"
            output.executive_summary = f"分析失败: {str(exc)}"

        return output

    # ------------------------------------------------------------------
    # Step 1: 数据加载与校验
    # ------------------------------------------------------------------

    def _load(self, data: CsvDataSource) -> Tuple[SourceInfo, ParseMeta]:
        """
        加载数据并统一为 DataFrame。

        根据数据来源分发到不同的加载器：
        - content  → 内存 CSV 解析
        - file_path (.csv) → 文件 CSV 解析（含编码/分隔符检测）
        - file_path (.xlsx/.xls) → Excel 兼容解析

        Args:
            data: 数据源

        Returns:
            (SourceInfo, ParseMeta): 来源信息和解析元信息

        Raises:
            ValueError: 数据源无效或格式不支持
        """
        # 校验互斥性
        valid, msg = data.validate_exclusivity()
        if not valid:
            raise ValueError(msg)

        source_info = SourceInfo()

        if data.content is not None:
            # ----------------------------------------------------------
            # 路径 A: 内存 CSV 字符串 → io.StringIO → pd.read_csv
            # 这是 Agent-2-Agent 数据流的关键路径——零磁盘 I/O
            # ----------------------------------------------------------
            content = data.content
            if len(content.strip()) == 0:
                raise ValueError("传入的 CSV 内容为空")

            delimiter = self.config.delimiter or detect_delimiter(content)
            try:
                self.raw_df = pd.read_csv(
                    io.StringIO(content),
                    sep=delimiter,
                    header=0 if self.config.has_header else None,
                    skiprows=self.config.skip_rows or 0,
                    comment=self.config.comment_char if self.config.comment_char else None,
                    on_bad_lines="warn",
                )
            except pd.errors.ParserError as e:
                raise ValueError(f"CSV 解析失败: {str(e)}。请检查分隔符和编码")

            source_info.type = "csv_inline"
            parse_meta = ParseMeta(
                delimiter=delimiter,
                encoding="utf-8",  # 内存字符串假设为 UTF-8
                row_count=len(self.raw_df),
                col_count=len(self.raw_df.columns),
            )

        elif data.file_path is not None:
            # ----------------------------------------------------------
            # 路径 B: 文件路径 → 根据后缀分发
            # ----------------------------------------------------------
            file_path = _safe_path(data.file_path)
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"文件不存在: {file_path}")

            # 检查文件大小
            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            source_info.file_size_mb = round(file_size_mb, 2)
            source_info.file_name = os.path.basename(file_path)

            if file_size_mb > MAX_FILE_SIZE_MB:
                raise ValueError(
                    f"文件过大 ({file_size_mb:.1f}MB)，上限 {MAX_FILE_SIZE_MB}MB"
                )

            ext = os.path.splitext(file_path)[1].lower()

            # 魔数校验 — 拒绝二进制/可执行文件（纵深防御第2层）
            if ext == ".csv":
                _verify_csv_magic(file_path)

            if ext == ".csv":
                # ---------- CSV 文件 ----------
                encoding = self.config.encoding or detect_encoding(file_path)
                delimiter = self.config.delimiter or detect_delimiter_from_file(
                    file_path, encoding
                )
                try:
                    self.raw_df = pd.read_csv(
                        file_path,
                        sep=delimiter,
                        encoding=encoding,
                        header=0 if self.config.has_header else None,
                        skiprows=self.config.skip_rows or 0,
                        comment=self.config.comment_char if self.config.comment_char else None,
                        on_bad_lines="warn",
                    )
                except pd.errors.ParserError as e:
                    raise ValueError(
                        f"CSV 解析失败: {str(e)}。"
                        f"检测到编码={encoding}, 分隔符='{delimiter}'"
                    )

                source_info.type = "csv_file"
                parse_meta = ParseMeta(
                    delimiter=delimiter,
                    encoding=encoding,
                    row_count=len(self.raw_df),
                    col_count=len(self.raw_df.columns),
                )

            elif ext in (".xlsx", ".xls"):
                # ---------- Excel 兼容 ----------
                sheet = self.config.excel_sheet or 0
                self.raw_df = pd.read_excel(file_path, sheet_name=sheet)
                source_info.type = "excel"
                parse_meta = ParseMeta(
                    row_count=len(self.raw_df),
                    col_count=len(self.raw_df.columns),
                )

            else:
                raise ValueError(
                    f"不支持的文件格式 '{ext}'。"
                    f"支持: .csv, .xlsx, .xls"
                )

        # ---------- 加载后校验 ----------
        if self.raw_df is None or self.raw_df.empty:
            raise ValueError("数据为空，无可分析内容")

        if len(self.raw_df.columns) < 2:
            raise ValueError(f"至少需要 2 列数据，当前仅 {len(self.raw_df.columns)} 列")

        if len(self.raw_df) > ROW_LIMITS["max_rows"]:
            raise ValueError(
                f"数据行数 ({len(self.raw_df)}) 超过上限 ({ROW_LIMITS['max_rows']})。"
                f"请筛选或采样后重试"
            )

        # 清洗列名（去除首尾空格，统一处理空列名）
        self.raw_df.columns = [
            str(c).strip() if pd.notna(c) else f"Unnamed_{i}"
            for i, c in enumerate(self.raw_df.columns)
        ]

        return source_info, parse_meta

    # ------------------------------------------------------------------
    # Step 2: 数据画像 — 为每列生成统计摘要
    # ------------------------------------------------------------------

    def _build_profile(self) -> List[ColumnStats]:
        """
        遍历每一列，计算统计画像。

        数值列: min/max/mean/median/std/Q1/Q3/skew/零值率
        日期列: 时间跨度/是否连续
        文本列: 平均长度/高频值 Top5

        Returns:
            ColumnStats 列表
        """
        df = self.raw_df
        profiles = []

        for col_name in df.columns:
            series = df[col_name]
            stats = ColumnStats(
                name=col_name,
                dtype=str(series.dtype),
                null_count=int(series.isnull().sum()),
                null_rate=round(series.isnull().mean(), 4),
                unique_count=int(series.nunique()),
                sample_values=series.dropna().head(5).tolist(),
            )

            # --- 数值列统计 ---
            if pd.api.types.is_numeric_dtype(series):
                valid = series.dropna()
                if len(valid) > 0:
                    stats.min_val = round(float(valid.min()), 4)
                    stats.max_val = round(float(valid.max()), 4)
                    stats.mean_val = round(float(valid.mean()), 4)
                    stats.median_val = round(float(valid.median()), 4)
                    stats.std_val = round(float(valid.std()), 4)
                    stats.q1_val = round(float(valid.quantile(0.25)), 4)
                    stats.q3_val = round(float(valid.quantile(0.75)), 4)
                    stats.zero_rate = round(
                        float((valid == 0).sum() / len(valid)), 4
                    )
                    if HAS_SCIPY and len(valid) > 3:
                        stats.skew_val = round(float(scipy_stats.skew(valid)), 4)

            # --- 日期列统计 ---
            elif pd.api.types.is_datetime64_any_dtype(series):
                valid = series.dropna()
                if len(valid) >= 2:
                    stats.time_span_days = (valid.max() - valid.min()).days
                    # 检查是否为连续日期（每日都有数据）
                    date_range = pd.date_range(valid.min(), valid.max())
                    stats.is_continuous = len(set(valid.dt.date)) >= len(date_range) * 0.9

            # --- 尝试将 object 列转换为日期 ---
            elif series.dtype == object:
                # 检测是否可能是日期列
                try:
                    converted = pd.to_datetime(series, errors="coerce")
                    valid = converted.dropna()
                    if len(valid) > len(series) * 0.5:  # 超过50%可转为日期
                        if len(valid) >= 2:
                            stats.time_span_days = (valid.max() - valid.min()).days
                            date_range = pd.date_range(valid.min(), valid.max())
                            stats.is_continuous = (
                                len(set(valid.dt.date)) >= len(date_range) * 0.9
                            )
                except Exception:
                    pass

                # 文本列统计
                text_valid = series.dropna().astype(str)
                if len(text_valid) > 0:
                    stats.avg_length = round(float(text_valid.str.len().mean()), 1)
                    top = text_valid.value_counts().head(5)
                    stats.top_values = [
                        {"value": str(k), "count": int(v)}
                        for k, v in top.items()
                    ]

            profiles.append(stats)

        return profiles

    # ------------------------------------------------------------------
    # Step 3: 列语义识别
    # ------------------------------------------------------------------

    def _infer_roles(self, data: CsvDataSource) -> Dict[str, ColumnRole]:
        """
        推断每列的语义角色。

        优先使用用户手动标注的 column_hints。
        否则使用启发式规则推断（生产环境应替换为 LLM 调用）。

        启发式规则优先级：
        1. 列名包含日期关键词 → date
        2. 列名包含金额关键词 → revenue
        3. 列名包含数量关键词 → quantity
        4. 数值列 + 正数 + 分布集中 → unit_price
        5. 无法判断的数值列 → metric
        6. 其他 → ignore

        Args:
            data: 数据源（用于获取 column_hints）

        Returns:
            列名 → ColumnRole 映射
        """
        roles = {}

        # 如果用户提供了手动标注，直接使用
        if self.config.column_hints:
            for col_name, role in self.config.column_hints.items():
                if col_name in self.raw_df.columns:
                    roles[col_name] = ColumnRole(
                        role=role, confidence=1.0, reason="用户手动标注"
                    )
            return roles

        # 关键词字典 — 中文 + 英文
        KEYWORD_MAP = {
            "date": [
                "日期", "时间", "date", "time", "年", "月", "日", "timestamp",
                "下单时间", "创建时间", "交易时间",
            ],
            "revenue": [
                "金额", "收入", "销售额", "营收", "revenue", "sales", "amount",
                "实收", "应收", "总价", "交易额", "流水",
            ],
            "quantity": [
                "数量", "销量", "件数", "quantity", "qty", "count", "volume",
                "售出", "下单",
            ],
            "unit_price": [
                "单价", "价格", "price", "unit_price", "定价", "售价",
            ],
            "cost": [
                "成本", "cost", "进价", "采购价",
            ],
            "profit": [
                "利润", "毛利", "profit", "margin", "净利",
            ],
            "product_name": [
                "产品名", "商品名", "product_name", "name", "品名",
                "产品", "商品", "SKU",
            ],
            "product_id": [
                "产品ID", "商品ID", "product_id", "sku_id", "item_id",
                "编号", "代码",
            ],
            "category": [
                "分类", "类别", "category", "品类", "类目", "大类", "中类",
            ],
            "region": [
                "区域", "地区", "region", "area", "zone", "城市", "省份",
                "门店", "store", "location", "大区", "华北", "华东",
            ],
            "customer_name": [
                "客户", "顾客", "customer", "client", "买家", "用户",
            ],
            "channel": [
                "渠道", "channel", "来源", "平台", "线上", "线下",
                "门店类型",
            ],
        }

        for col_name in self.raw_df.columns:
            col_lower = col_name.lower()
            role = "ignore"
            confidence = 0.3
            reason = "未匹配到关键词"

            # 按优先级匹配关键词
            for candidate_role, keywords in KEYWORD_MAP.items():
                if any(kw.lower() in col_lower for kw in keywords):
                    role = candidate_role
                    confidence = 0.85
                    reason = f"列名匹配关键词: {keywords[0]}"
                    break

            # 如果列名未匹配，根据数据特征辅助判断
            if role == "ignore":
                profile = next(
                    (p for p in self.profile if p.name == col_name), None
                )
                if profile:
                    # 数值列 → 默认为 metric
                    if "float" in profile.dtype or "int" in profile.dtype:
                        role = "metric"
                        confidence = 0.5
                        reason = "数值列，未匹配具体语义，标记为通用指标"
                    elif profile.time_span_days is not None:
                        role = "date"
                        confidence = 0.7
                        reason = "可解析为日期列"

            roles[col_name] = ColumnRole(
                role=role, confidence=confidence, reason=reason
            )

        return roles

    # ------------------------------------------------------------------
    # Step 4: 数据清洗
    # ------------------------------------------------------------------

    def _clean(self) -> Tuple[pd.DataFrame, QualityReport]:
        """
        清洗数据并生成质量报告。

        清洗策略按列角色差异化:
        - date:     不填充，标记缺失行
        - revenue/quantity/cost/profit/metric: 中位数填充 + IQR 异常标记
        - category/region/channel: 众数填充
        - ignore:   不处理

        原则: 只标记不删除。所有清洗操作记录在 cleaning_actions 中。

        Returns:
            (cleaned_df, QualityReport)
        """
        df = self.raw_df.copy()
        actions: List[CleaningAction] = []

        # 去重检测（标记但不删除）
        dup_mask = df.duplicated(keep=False)
        dup_count = int(dup_mask.sum())
        if dup_count > 0:
            actions.append(CleaningAction(
                column="__all__",
                action="marked_duplicate",
                affected_rows=dup_count,
                description=f"检测到 {dup_count} 行重复数据（已标记未删除）",
            ))

        null_summary: Dict[str, float] = {}
        outlier_summary: Dict[str, int] = {}

        for col_name in df.columns:
            role_info = self.roles.get(col_name, ColumnRole(role="ignore", confidence=0))
            role = role_info.role

            # 缺失值统计
            null_count = int(df[col_name].isnull().sum())
            null_rate = null_count / len(df) if len(df) > 0 else 0
            null_summary[col_name] = round(null_rate, 4)

            # 按角色选择清洗策略
            if role == "date":
                # 日期列：不填充，但尝试标准化
                try:
                    df[col_name] = pd.to_datetime(df[col_name], errors="coerce")
                    actions.append(CleaningAction(
                        column=col_name,
                        action="type_converted",
                        affected_rows=len(df),
                        description="日期格式标准化",
                    ))
                except Exception:
                    pass

            elif role in ("revenue", "quantity", "cost", "profit", "metric"):
                # 数值列：中位数填充缺失值
                if null_count > 0:
                    median_val = df[col_name].median()
                    if pd.notna(median_val):
                        df[col_name] = df[col_name].fillna(median_val)
                        actions.append(CleaningAction(
                            column=col_name,
                            action="filled_na",
                            affected_rows=null_count,
                            description=f"缺失值用中位数 {median_val:.2f} 填充",
                        ))

                # 数值列：IQR 异常值检测
                q1 = df[col_name].quantile(0.25)
                q3 = df[col_name].quantile(0.75)
                iqr = q3 - q1
                lower = q1 - 1.5 * iqr
                upper = q3 + 1.5 * iqr
                outlier_mask = (df[col_name] < lower) | (df[col_name] > upper)
                outlier_count = int(outlier_mask.sum())
                outlier_summary[col_name] = outlier_count

                if outlier_count > 0:
                    actions.append(CleaningAction(
                        column=col_name,
                        action="marked_outlier",
                        affected_rows=outlier_count,
                        description=(
                            f"IQR 异常值检测: 下界={lower:.2f}, 上界={upper:.2f}, "
                            f"标记 {outlier_count} 个异常点"
                        ),
                    ))

            elif role in ("category", "region", "channel", "product_name"):
                # 分类列：众数填充缺失值
                if null_count > 0:
                    mode_val = df[col_name].mode()
                    if len(mode_val) > 0:
                        df[col_name] = df[col_name].fillna(mode_val[0])
                        actions.append(CleaningAction(
                            column=col_name,
                            action="filled_na",
                            affected_rows=null_count,
                            description=f"缺失值用众数 '{mode_val[0]}' 填充",
                        ))

        # 计算质量评分
        score = 100
        # 高缺失率列扣分
        for rate in null_summary.values():
            if rate > 0.2:
                score -= QUALITY_WEIGHTS["null_penalty_per_column"]
        # 高异常率列扣分
        total_rows = len(df)
        for count in outlier_summary.values():
            if total_rows > 0 and count / total_rows > 0.05:
                score -= QUALITY_WEIGHTS["outlier_penalty_per_column"]
        # 重复行扣分
        if total_rows > 0:
            score -= int((dup_count / total_rows) * QUALITY_WEIGHTS["duplicate_penalty_factor"])
        score = max(0, min(100, score))

        quality = QualityReport(
            score=score,
            null_summary=null_summary,
            outlier_summary=outlier_summary,
            duplicate_rows=dup_count,
            cleaning_actions=actions,
        )

        return df, quality

    # ------------------------------------------------------------------
    # Step 5: 多维分析
    # ------------------------------------------------------------------

    def _run_analyses(self, data: CsvDataSource) -> List[SubAnalysisResult]:
        """
        执行多维分析任务。

        根据 analysis_depth 和可用数据维度动态选择分析任务。
        每个分析任务独立执行，失败不影响其他任务。

        Args:
            data: 数据源（用于获取 focus_questions）

        Returns:
            分析结果列表
        """
        results: List[SubAnalysisResult] = []
        df = self.cleaned_df if self.cleaned_df is not None else self.raw_df
        depth = self.config.analysis_depth
        max_tasks = DEPTH_TASK_LIMITS.get(depth, 6)

        # ---------- 盘点可用维度 ----------
        has_date = any(r.role == "date" for r in self.roles.values())
        has_revenue = any(r.role == "revenue" for r in self.roles.values())
        has_product = any(
            r.role in ("product_id", "product_name") for r in self.roles.values()
        )
        has_region = any(r.role == "region" for r in self.roles.values())
        has_customer = any(
            r.role in ("customer_id", "customer_name") for r in self.roles.values()
        )
        numeric_cols = [
            c for c, r in self.roles.items()
            if r.role in ("revenue", "quantity", "cost", "profit", "metric")
        ]
        has_multi_numeric = len(numeric_cols) >= 3
        has_multi_dim = sum([has_product, has_region, has_customer]) >= 2

        date_col = next(
            (c for c, r in self.roles.items() if r.role == "date"), None
        )
        revenue_col = next(
            (c for c, r in self.roles.items() if r.role == "revenue"),
            numeric_cols[0] if numeric_cols else None,
        )

        # ---------- 构建任务列表 ----------
        # 各任务的指标定义见《统计指标参考手册》对应章节
        tasks = []

        # 描述统计 — 始终执行 → 手册「第三章」
        tasks.append(("desc_stats", self._describe_statistics))

        # 时序分析 — 有日期 + 有数值列 → 手册「第四章」
        if has_date and date_col and numeric_cols:
            tasks.append(("time_series", lambda: self._analyze_time_series(
                df, date_col, revenue_col or numeric_cols[0]
            )))

        # 同比对比 — 月度聚合后 ≥ 12 期 → 手册「4.3」
        if has_date and date_col and numeric_cols:
            try:
                ts_check = df[[date_col, numeric_cols[0]]].copy()
                ts_check[date_col] = pd.to_datetime(ts_check[date_col], errors="coerce")
                ts_check = ts_check.dropna()
                monthly_check = ts_check.set_index(date_col)[numeric_cols[0]].resample("ME").sum()
                ts_len_ok = len(monthly_check) >= 12
            except Exception:
                ts_len_ok = False
        if ts_len_ok:
            tasks.append(("yoy_comparison", lambda: self._analyze_yoy(
                df, date_col, revenue_col or numeric_cols[0]
            )))

        # Top/Bottom 排名 — 有维度列 + 有数值列 → 手册「第五章」
        if has_product and numeric_cols:
            product_col = next(
                c for c, r in self.roles.items()
                if r.role in ("product_id", "product_name")
            )
            tasks.append(("top_ranking", lambda: self._analyze_ranking(
                df, product_col, revenue_col or numeric_cols[0]
            )))

        # ABC 帕累托 — 有产品 + 有 revenue → 手册「第六章」
        if has_product and revenue_col:
            product_col = next(
                c for c, r in self.roles.items()
                if r.role in ("product_id", "product_name")
            )
            tasks.append(("pareto_abc", lambda: self._analyze_pareto(
                df, product_col, revenue_col
            )))

        # 异常检测 — 有数值列 → 手册「第七章」
        if numeric_cols:
            tasks.append(("anomaly_detect", lambda: self._analyze_anomalies(
                df, numeric_cols
            )))

        # 相关性矩阵 — ≥3 个数值列 → 手册「第八章」
        if has_multi_numeric and HAS_SCIPY:
            tasks.append(("correlation", lambda: self._analyze_correlations(
                df, numeric_cols
            )))

        # 分布分析 — 有数值列 + SciPy 可用 → 手册「第九章」
        if numeric_cols and HAS_SCIPY and depth in ("standard", "deep"):
            tasks.append(("distribution", lambda: self._analyze_distribution(
                df, numeric_cols[:5]  # 最多分析前5个数值列
            )))

        # 区域分析 — 有区域 + 有数值列 → 手册「10.1」
        if has_region and numeric_cols:
            region_col = next(
                c for c, r in self.roles.items() if r.role == "region"
            )
            tasks.append(("region_analysis", lambda: self._analyze_region(
                df, region_col, revenue_col or numeric_cols[0]
            )))

        # 维度下钻 — ≥2 维度列 → 手册「10.2」
        if has_multi_dim and revenue_col:
            dim_cols = []
            if has_product:
                dim_cols.append(next(
                    c for c, r in self.roles.items()
                    if r.role in ("product_id", "product_name")
                ))
            if has_region:
                dim_cols.append(next(
                    c for c, r in self.roles.items() if r.role == "region"
                ))
            tasks.append(("drill_down", lambda: self._analyze_drilldown(
                df, dim_cols, revenue_col
            )))

        # RFM 客户分析 — 有 customer + date + revenue → 手册「第十一章」
        if has_customer and has_date and revenue_col:
            customer_col = next(
                c for c, r in self.roles.items()
                if r.role in ("customer_id", "customer_name")
            )
            tasks.append(("rfm_analysis", lambda: self._analyze_rfm(
                df, customer_col, date_col, revenue_col
            )))

        # 简单预测 — deep 模式 + 月度时序≥12 → 手册「第十二章」
        if depth == "deep" and has_date and date_col and numeric_cols and ts_len_ok:
            tasks.append(("simple_forecast", lambda: self._analyze_forecast(
                df, date_col, revenue_col or numeric_cols[0]
            )))

        # 根据 depth 限制任务数
        executed = 0
        for task_id, task_fn in tasks:
            if executed >= max_tasks:
                break
            try:
                result = task_fn()
                result.task_id = task_id
                results.append(result)
                executed += 1
            except Exception as exc:
                results.append(SubAnalysisResult(
                    task_id=task_id,
                    status="error",
                    error_message=str(exc),
                ))

        # --- 批量生成 narration (企业级优化: N次LLM调用合并为1次) ---
        # 启用 batch_narrate 可将 N 次 LLM 调用合并为 1 次，节省 ~81% LLM token
        if self.llm is not None:
            results = self._batch_narrate(results)

        return results

    def _batch_narrate(self, results: List[SubAnalysisResult]) -> List[SubAnalysisResult]:
        """
        批量生成分析解读 —— 将 N 次 LLM 调用合并为 1 次。

        优化前: 6 个分析 = 6 次 LLM 调用 ≈ 6 × 500 token 上下文开销
        优化后: 6 个分析 = 1 次 LLM 调用 ≈ 1 × 500 token 上下文开销
        节省: ~2,500 token (standard模式)

        Args:
            results: 已完成 metrics 计算但 narration 为空的分析结果列表

        Returns:
            填充了 narration 的分析结果列表
        """
        # 收集所有 metrics（仅传关键字段给 LLM）
        compact = []
        for r in results:
            if r.status != "success":
                continue
            # 只传核心指标，去掉明细列表（如 top_10 只传前三）
            slim_metrics = self._slim_metrics(r.task_id, r.metrics)
            compact.append({"task_id": r.task_id, "metrics": slim_metrics})

        if not compact:
            return results

        # 构建批量 Prompt
        prompt = self._build_batch_prompt(compact)

        # TODO: 替换为实际 LLM 调用
        # response = self.llm.invoke(prompt)
        # narrations = parse_narrations(response)  # {"task_id": "narration", ...}

        # 回填 narration
        # for r in results:
        #     if r.task_id in narrations:
        #         r.narration = narrations[r.task_id]

        return results

    def _slim_metrics(self, task_id: str, metrics: Dict) -> Dict:
        """
        精简 metrics —— 只保留 LLM 解读需要的关键指标。

        去除: 全量明细列表(如top_10完整名单)、中间计算值
        保留: 聚合值、比率、趋势方向、判读相关字段
        """
        key_fields = {
            "desc_stats": ["row_count", "col_count", "numeric_col_count", "overall_null_rate", "memory_mb"],
            "time_series": ["period_count", "total", "mean", "avg_mom", "std_mom", "trend_direction", "trend_magnitude", "cagr", "positive_periods", "negative_periods", "max_growth", "max_decline", "inflection_points"],
            "yoy_comparison": ["avg_yoy", "positive_yoy", "negative_yoy", "comparable_periods"],
            "top_ranking": ["unique_items", "cr1", "cr5", "cr10", "hhi", "above_mean_rate", "mean_per_item", "median_per_item"],
            "pareto_abc": ["total_products", "A.count", "A.revenue_share", "B.count", "B.revenue_share", "C.count", "C.revenue_share"],
            "anomaly_detect": ["total_anomalies", "columns_analyzed"],
            "correlation": ["strong_pairs", "redundant_pairs"],
            "distribution": ["columns_analyzed"],
            "region_analysis": ["region_count", "top_region", "bottom_region"],
            "drill_down": ["combinations"],
            "rfm_analysis": ["total_customers", "segment_counts", "segment_revenue_share", "rfm_scores"],
            "simple_forecast": ["trend_slope", "trend_r2", "next_forecast", "forecast_interval_95"],
        }
        fields = key_fields.get(task_id, list(metrics.keys())[:5])
        return {k: metrics[k] for k in fields if k in metrics}

    def _build_batch_prompt(self, compact: List[Dict]) -> str:
        """构建批量解读 Prompt（合并N个分析，一次LLM调用）"""
        parts = ["请为以下数据分析结果生成简洁解读（每条150-300字）：\n"]
        for item in compact:
            parts.append(f"## {item['task_id']}")
            parts.append(json.dumps(item["metrics"], ensure_ascii=False, default=str))
            parts.append("")
        parts.append("返回JSON: {task_id: narration, ...}")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 子分析函数 — 每个都是独立的分析单元
    # ------------------------------------------------------------------

    def _describe_statistics(self) -> SubAnalysisResult:
        """
        描述性统计 — 生成全列统计摘要。

        包含: 行数/列数/总体缺失率/内存占用/数值列核心统计量。
        """
        df = self.raw_df
        # 数值列整体统计
        numeric_df = df.select_dtypes(include=[np.number])
        # 内存占用 (手册 3.1)
        memory_mb = round(df.memory_usage(deep=True).sum() / (1024 * 1024), 2)
        metrics = {
            "row_count": len(df),
            "col_count": len(df.columns),
            "numeric_col_count": len(numeric_df.columns),
            "overall_null_rate": round(df.isnull().mean().mean(), 4),
            "memory_mb": memory_mb,
        }
        if len(numeric_df.columns) > 0:
            desc = numeric_df.describe().to_dict()
            metrics["numeric_summary"] = {
                col: {k: round(v, 2) if isinstance(v, float) else v
                      for k, v in stats.items()}
                for col, stats in desc.items()
            }

        # 生成自然语言解读
        narration = (
            f"数据集包含 {metrics['row_count']} 行, {metrics['col_count']} 列, "
            f"其中 {metrics['numeric_col_count']} 列为数值类型。"
            f"整体数据缺失率为 {metrics['overall_null_rate']:.1%}。"
        )
        if metrics["numeric_col_count"] > 0:
            col_names = list(desc.keys())
            narration += f" 主要数值指标: {', '.join(col_names[:5])}。"

        return SubAnalysisResult(
            task_id="desc_stats",
            metrics=metrics,
            narration=narration,
        )

    def _analyze_time_series(
        self, df: pd.DataFrame, date_col: str, metric_col: str
    ) -> SubAnalysisResult:
        """
        时序趋势分析。

        执行:
        1. 按日期排序并重采样为月度数据
        2. 计算环比增长率（MoM）
        3. 计算 3 期移动平均
        4. 识别趋势拐点和异常月份

        Args:
            df:        清洗后的 DataFrame
            date_col:  日期列名
            metric_col: 分析指标列名

        Returns:
            含有时序指标和解读的分析结果
        """
        ts = df.copy()
        ts[date_col] = pd.to_datetime(ts[date_col], errors="coerce")
        ts = ts.dropna(subset=[date_col, metric_col])
        ts = ts.sort_values(date_col)
        ts = ts.set_index(date_col)

        # 月度聚合
        monthly = ts[metric_col].resample("ME").sum()

        # 环比增长率
        mom_growth = monthly.pct_change()

        # 3 期移动平均
        moving_avg = monthly.rolling(window=3, min_periods=1).mean()

        # 识别拐点（环比符号变化）
        inflection_points = []
        for i in range(1, len(mom_growth)):
            if i > 0 and pd.notna(mom_growth.iloc[i]) and pd.notna(mom_growth.iloc[i-1]):
                if mom_growth.iloc[i] * mom_growth.iloc[i-1] < 0:
                    inflection_points.append({
                        "period": str(mom_growth.index[i].date()),
                        "change": f"{mom_growth.iloc[i]:+.1%}",
                        "direction": "上升" if mom_growth.iloc[i] > 0 else "下降",
                    })

        # --- 环比统计量 (手册 4.2) ---
        valid_mom = mom_growth.dropna()
        avg_mom = round(float(valid_mom.mean()), 4) if len(valid_mom) > 0 else None
        std_mom = round(float(valid_mom.std()), 4) if len(valid_mom) > 1 else None
        positive_periods = int((valid_mom > 0).sum())
        negative_periods = int((valid_mom < 0).sum())

        # --- 趋势方向与幅度 (手册 4.5) ---
        if len(moving_avg) >= 2:
            trend_direction = "上升" if moving_avg.iloc[-1] > moving_avg.iloc[0] else "下降"
            if abs(moving_avg.iloc[0]) > 0:
                trend_magnitude = round(
                    float(abs(moving_avg.iloc[-1] - moving_avg.iloc[0]) / abs(moving_avg.iloc[0])), 4
                )
            else:
                trend_magnitude = 0.0
        else:
            trend_direction = "数据不足"
            trend_magnitude = 0.0

        # --- CAGR 复合年增长率 (手册 4.5) ---
        if len(monthly) >= 12:
            years = len(monthly) / 12
            first_val = float(monthly.iloc[0])
            last_val = float(monthly.iloc[-1])
            if first_val > 0 and last_val > 0:
                cagr = round((last_val / first_val) ** (1 / years) - 1, 4)
            else:
                cagr = None
        else:
            cagr = None

        # --- 最大增幅/降幅 (手册 4.2) ---
        max_growth = round(float(valid_mom.max()), 4) if len(valid_mom) > 0 else None
        max_decline = round(float(valid_mom.min()), 4) if len(valid_mom) > 0 else None

        metrics = {
            "period_count": len(monthly),
            "total": round(float(monthly.sum()), 2),
            "mean": round(float(monthly.mean()), 2),
            "peak_period": str(monthly.idxmax().date()) if monthly.idxmax() is not pd.NaT else None,
            "trough_period": str(monthly.idxmin().date()) if monthly.idxmin() is not pd.NaT else None,
            # 环比指标 (手册 4.2)
            "avg_mom": avg_mom,
            "std_mom": std_mom,
            "positive_periods": positive_periods,
            "negative_periods": negative_periods,
            "max_growth": max_growth,
            "max_decline": max_decline,
            # 趋势指标 (手册 4.5)
            "trend_direction": trend_direction,
            "trend_magnitude": trend_magnitude,
            "cagr": cagr,
            "inflection_points": inflection_points,
        }

        # 时序数据降采样：>24期时仅保留年度汇总+最近12个月明细
        if len(monthly) > MAX_TIMESERIES_PERIODS:
            metrics["_sample_note"] = (
                f"月度数据 {len(monthly)} 期 > {MAX_TIMESERIES_PERIODS}，"
                f"已降采样为年度汇总 + 最近12个月明细"
            )
            # 年度汇总
            annual = monthly.resample("YE").sum()
            metrics["annual_summary"] = {
                str(k.year): round(v, 2) for k, v in annual.items()
            }
            # 仅最近12个月明细
            recent = monthly.tail(12)
            recent_moving = moving_avg.tail(12)
            recent_mom = mom_growth.tail(12)
            metrics["recent_monthly_values"] = {
                str(k.date()): round(v, 2) for k, v in recent.items()
            }
            metrics["recent_moving_avg"] = {
                str(k.date()): round(v, 2) if pd.notna(v) else None
                for k, v in recent_moving.items()
            }
            metrics["recent_mom_growth"] = {
                str(k.date()): round(v, 4) if pd.notna(v) else None
                for k, v in recent_mom.items()
            }
            # 保留全量序列引用被省略的标记
            metrics["monthly_values"] = None
            metrics["moving_avg_3"] = None
            metrics["mom_growth"] = None
        else:
            metrics["monthly_values"] = {
                str(k.date()): round(v, 2) for k, v in monthly.items()
            }
            metrics["moving_avg_3"] = {
                str(k.date()): round(v, 2) if pd.notna(v) else None
                for k, v in moving_avg.items()
            }
            metrics["mom_growth"] = {
                str(k.date()): round(v, 4) if pd.notna(v) else None
                for k, v in mom_growth.items()
            }

        narration = (
            f"共 {metrics['period_count']} 期数据，整体呈{trend_direction}趋势"
            f"（变动幅度 {trend_magnitude:.1%}）。"
            f"平均环比{'+' if avg_mom and avg_mom > 0 else ''}{avg_mom if avg_mom is not None else 'N/A'}，"
            f"其中 {positive_periods} 期正增长、{negative_periods} 期负增长。"
        )
        if cagr is not None:
            narration += f" CAGR={cagr:+.1%}。"
        if inflection_points:
            pts = inflection_points[:3]
            pts_desc = "；".join(
                f"{p['period']} 转为{p['direction']}" for p in pts
            )
            narration += f" 关键拐点: {pts_desc}。"

        return SubAnalysisResult(
            task_id="time_series",
            metrics=metrics,
            narration=narration,
        )

    def _analyze_ranking(
        self, df: pd.DataFrame, dim_col: str, metric_col: str
    ) -> SubAnalysisResult:
        """
        Top/Bottom 排名分析。

        计算:
        - Top 10 和 Bottom 5（按指标汇总值）
        - 集中度 CR5: 前 5 名占比
        - 均值线：高于/低于均值的数量

        Args:
            df:         清洗后的 DataFrame
            dim_col:    维度列（如产品名）
            metric_col: 排序指标列

        Returns:
            含有排名数据的分析结果
        """
        grouped = df.groupby(dim_col)[metric_col].sum().sort_values(ascending=False)
        total = grouped.sum()

        top_10 = grouped.head(10).to_dict()
        bottom_5 = grouped.tail(5).to_dict()

        # 集中度指标 (手册 第五章)
        cr1 = float(grouped.head(1).sum() / total) if total > 0 else 0
        cr5 = float(grouped.head(5).sum() / total) if total > 0 else 0
        cr10 = float(grouped.head(10).sum() / total) if total > 0 else 0

        # HHI (Herfindahl-Hirschman Index) = Σ(share_i)²
        shares = grouped / total
        hhi = round(float((shares ** 2).sum()), 4)

        metrics = {
            "dimension": dim_col,
            "metric": metric_col,
            "unique_items": len(grouped),
            "total": round(float(total), 2),
            "top_10": {str(k): round(float(v), 2) for k, v in top_10.items()},
            "bottom_5": {str(k): round(float(v), 2) for k, v in bottom_5.items()},
            "cr1": round(cr1, 4),
            "cr5": round(cr5, 4),
            "cr10": round(cr10, 4),
            "hhi": hhi,
            "mean_per_item": round(float(grouped.mean()), 2),
            "median_per_item": round(float(grouped.median()), 2),
            "above_mean_rate": round(float((grouped > grouped.mean()).sum() / len(grouped)), 4),
        }

        # 集中度判读 → 手册第五章判读标准
        if cr5 > 0.7:
            concentration = "高度集中，少数头部主导"
        elif cr5 > 0.4:
            concentration = "中度集中"
        else:
            concentration = "分布分散，长尾显著"

        narration = (
            f"{dim_col} 共 {metrics['unique_items']} 个，"
            f"CR5={cr5:.1%}（{concentration}），HHI={hhi:.4f}。"
        )

        return SubAnalysisResult(
            task_id="top_ranking",
            metrics=metrics,
            narration=narration,
        )

    def _analyze_pareto(
        self, df: pd.DataFrame, product_col: str, revenue_col: str
    ) -> SubAnalysisResult:
        """
        ABC 帕累托分类。

        按收入降序排列后计算累计占比:
        - A 类: 累计占比 0%～70%（核心产品）
        - B 类: 累计占比 70%～90%（重要产品）
        - C 类: 累计占比 90%～100%（长尾产品）

        Args:
            df:          清洗后的 DataFrame
            product_col: 产品列名
            revenue_col: 收入列名

        Returns:
            含有 ABC 分类结果的分析结果
        """
        grouped = df.groupby(product_col)[revenue_col].sum().sort_values(ascending=False)
        total = grouped.sum()

        cumsum_pct = grouped.cumsum() / total
        a_mask = cumsum_pct <= 0.7
        b_mask = (cumsum_pct > 0.7) & (cumsum_pct <= 0.9)
        c_mask = cumsum_pct > 0.9

        a_count = int(a_mask.sum())
        b_count = int(b_mask.sum())
        c_count = int(c_mask.sum())

        metrics = {
            "total_revenue": round(float(total), 2),
            "total_products": len(grouped),
            "A": {"count": a_count, "revenue_share": round(float(grouped[a_mask].sum() / total), 4)},
            "B": {"count": b_count, "revenue_share": round(float(grouped[b_mask].sum() / total), 4)},
            "C": {"count": c_count, "revenue_share": round(float(grouped[c_mask].sum() / total), 4)},
        }

        narration = (
            f"ABC 分类结果: A 类 {a_count} 个产品贡献 {metrics['A']['revenue_share']:.0%} 收入, "
            f"B 类 {b_count} 个产品贡献 {metrics['B']['revenue_share']:.0%}, "
            f"C 类 {c_count} 个长尾产品仅占 {metrics['C']['revenue_share']:.0%}。"
        )
        if c_count > a_count * 3:
            narration += " C 类产品数量远超 A/B 类，建议评估长尾清理机会。"

        return SubAnalysisResult(
            task_id="pareto_abc",
            metrics=metrics,
            narration=narration,
        )

    def _analyze_anomalies(
        self, df: pd.DataFrame, numeric_cols: List[str]
    ) -> SubAnalysisResult:
        """
        IQR 异常值检测。

        对每个数值列计算 IQR 边界，标记超出 1.5×IQR 的数据点。

        Args:
            df:           清洗后的 DataFrame
            numeric_cols: 待检测的数值列名列表

        Returns:
            含有异常点统计的分析结果
        """
        anomaly_report = {}
        total_anomalies = 0

        for col in numeric_cols:
            q1 = df[col].quantile(0.25)
            q3 = df[col].quantile(0.75)
            iqr = q3 - q1
            lower = q1 - 1.5 * iqr
            upper = q3 + 1.5 * iqr
            outliers = df[(df[col] < lower) | (df[col] > upper)]
            anomaly_report[col] = {
                "lower_bound": round(float(lower), 2),
                "upper_bound": round(float(upper), 2),
                "outlier_count": len(outliers),
                "outlier_rate": round(len(outliers) / len(df), 4),
            }
            total_anomalies += len(outliers)

        metrics = {
            "total_anomalies": total_anomalies,
            "columns_analyzed": len(numeric_cols),
            "details": anomaly_report,
        }

        # 找出异常最严重的列
        worst_col = max(anomaly_report.items(), key=lambda x: x[1]["outlier_rate"])
        narration = (
            f"共检测到 {total_anomalies} 个异常点，分布在 {len(numeric_cols)} 个数值列。"
            f"异常率最高的列为 '{worst_col[0]}'（{worst_col[1]['outlier_rate']:.1%}）。"
        )

        return SubAnalysisResult(
            task_id="anomaly_detect",
            metrics=metrics,
            narration=narration,
        )

    def _analyze_correlations(
        self, df: pd.DataFrame, numeric_cols: List[str]
    ) -> SubAnalysisResult:
        """
        相关性矩阵分析 → 手册「第八章」

        计算 Pearson 和 Spearman 相关系数矩阵，标记强相关对。

        当数值列超过 MAX_CORR_COLS 时，仅返回 strong_pairs + redundant_pairs，
        不返回完整矩阵 — 防止 O(N²) token 爆炸（50列=~9,400 token）。

        Args:
            df:           清洗后的 DataFrame
            numeric_cols: 数值列名列表

        Returns:
            含有相关性矩阵和 p-value 的分析结果
        """
        cols_exceeded = len(numeric_cols) > MAX_CORR_COLS

        # Pearson 相关
        corr_matrix = df[numeric_cols].corr(method="pearson")

        # Spearman 相关（手册 8.1）
        spearman_matrix = df[numeric_cols].corr(method="spearman")

        # 提取强相关对（排除自相关）→ 手册判读标准：|r| > 0.7 为强相关
        strong_pairs = []
        redundant_pairs = []  # |r| > 0.95 疑似共线
        for i, col_a in enumerate(numeric_cols):
            for j, col_b in enumerate(numeric_cols):
                if i < j:
                    val = corr_matrix.loc[col_a, col_b]
                    if abs(val) > 0.7:
                        pair_data = {
                            "col_a": col_a,
                            "col_b": col_b,
                            "pearson_r": round(float(val), 4),
                            "spearman_rho": round(float(spearman_matrix.loc[col_a, col_b]), 4),
                            "type": "正相关" if val > 0 else "负相关",
                        }
                        if abs(val) > 0.95:
                            redundant_pairs.append(pair_data)
                        else:
                            strong_pairs.append(pair_data)

        # p-value 矩阵（仅当 SciPy 可用，列数超标时仅计算强相关对的）
        p_values = {}
        if HAS_SCIPY:
            if cols_exceeded:
                # 仅计算 strong_pairs 的 p-value
                for pair in strong_pairs + redundant_pairs:
                    col_a, col_b = pair["col_a"], pair["col_b"]
                    clean = df[[col_a, col_b]].dropna()
                    if len(clean) >= 3:
                        try:
                            _, p = scipy_stats.pearsonr(clean[col_a], clean[col_b])
                            p_values[f"{col_a}|{col_b}"] = round(float(p), 6)
                        except Exception:
                            p_values[f"{col_a}|{col_b}"] = None
            else:
                for i, col_a in enumerate(numeric_cols):
                    for j, col_b in enumerate(numeric_cols):
                        if i < j:
                            clean = df[[col_a, col_b]].dropna()
                            if len(clean) >= 3:
                                try:
                                    _, p = scipy_stats.pearsonr(clean[col_a], clean[col_b])
                                    p_values[f"{col_a}|{col_b}"] = round(float(p), 6)
                                except Exception:
                                    p_values[f"{col_a}|{col_b}"] = None

        # 当列数超标时，不返回完整矩阵（O(N²) 爆炸的核心来源）
        if cols_exceeded:
            metrics = {
                "columns": numeric_cols,
                "column_count": len(numeric_cols),
                "_slim_note": f"数值列 {len(numeric_cols)} > {MAX_CORR_COLS}，已省略完整矩阵",
                "pearson_matrix": None,
                "spearman_matrix": None,
                "p_values": p_values,
                "strong_pairs": strong_pairs,
                "redundant_pairs": redundant_pairs,
            }
        else:
            metrics = {
                "columns": numeric_cols,
                "pearson_matrix": {
                    col_a: {col_b: round(float(corr_matrix.loc[col_a, col_b]), 4)
                            for col_b in numeric_cols}
                    for col_a in numeric_cols
                },
                "spearman_matrix": {
                    col_a: {col_b: round(float(spearman_matrix.loc[col_a, col_b]), 4)
                            for col_b in numeric_cols}
                    for col_a in numeric_cols
                },
                "p_values": p_values,
                "strong_pairs": strong_pairs,
                "redundant_pairs": redundant_pairs,
            }

        if strong_pairs:
            pair_descs = [
                f"{p['col_a']} ↔ {p['col_b']} ({p['type']}, r={p['pearson_r']:.2f})"
                for p in strong_pairs[:5]
            ]
            narration = f"发现 {len(strong_pairs)} 对强相关关系: {'; '.join(pair_descs)}。"
        else:
            narration = "未发现强相关关系（|r| > 0.7）。"

        if redundant_pairs:
            narration += (
                f" 其中 {len(redundant_pairs)} 对相关系数 >0.95，"
                f"可能存在信息冗余列。"
            )

        return SubAnalysisResult(
            task_id="correlation",
            metrics=metrics,
            narration=narration,
        )

    def _analyze_region(
        self, df: pd.DataFrame, region_col: str, metric_col: str
    ) -> SubAnalysisResult:
        """
        区域/分组分析。

        按区域聚合指标，计算各区域占比和均值。

        Args:
            df:         清洗后的 DataFrame
            region_col: 区域列名
            metric_col: 分析指标列名

        Returns:
            含有区域对比数据的分析结果
        """
        grouped = df.groupby(region_col)[metric_col].agg(["sum", "mean", "count"])
        total = grouped["sum"].sum()

        regional_data = {}
        for region_name, row in grouped.iterrows():
            regional_data[str(region_name)] = {
                "total": round(float(row["sum"]), 2),
                "share": round(float(row["sum"] / total), 4) if total > 0 else 0,
                "mean": round(float(row["mean"]), 2),
                "count": int(row["count"]),
            }

        # 找出贡献最大和最小的区域
        sorted_regions = sorted(
            regional_data.items(), key=lambda x: x[1]["share"], reverse=True
        )
        top_region = sorted_regions[0] if sorted_regions else (None, None)
        bottom_region = sorted_regions[-1] if sorted_regions else (None, None)

        metrics = {
            "total": round(float(total), 2),
            "region_count": len(grouped),
            "regions": regional_data,
        }

        narration = (
            f"共 {metrics['region_count']} 个{region_col}。"
        )
        if top_region and top_region[1]:
            narration += (
                f"贡献最高: {top_region[0]}（{top_region[1]['share']:.1%}），"
                f"贡献最低: {bottom_region[0]}（{bottom_region[1]['share']:.1%}）。"
            )

        return SubAnalysisResult(
            task_id="region_analysis",
            metrics=metrics,
            narration=narration,
        )

    def _analyze_drilldown(
        self, df: pd.DataFrame, dim_cols: List[str], metric_col: str
    ) -> SubAnalysisResult:
        """
        维度下钻（交叉分析）。

        对多个维度列做 GroupBy 聚合，生成交叉表并计算每个分组的贡献率。

        Args:
            df:         清洗后的 DataFrame
            dim_cols:   维度列名列表（2个）
            metric_col: 聚合指标列名

        Returns:
            含有交叉表数据的分析结果
        """
        if len(dim_cols) < 2:
            return SubAnalysisResult(
                task_id="drill_down",
                status="skipped",
                error_message="需要至少 2 个维度列才能做交叉分析",
            )

        grouped = df.groupby(dim_cols)[metric_col].sum()
        total = grouped.sum()

        cross_table = {}
        for (dim1_val, dim2_val), val in grouped.items():
            key = f"{dim1_val}|{dim2_val}"
            cross_table[key] = {
                "value": round(float(val), 2),
                "share": round(float(val / total), 4) if total > 0 else 0,
            }

        metrics = {
            "dimensions": dim_cols,
            "metric": metric_col,
            "total": round(float(total), 2),
            "cross_table": cross_table,
            "combinations": len(cross_table),
        }

        # 找出贡献最大的组合
        top_combos = sorted(
            cross_table.items(), key=lambda x: x[1]["share"], reverse=True
        )[:3]
        top_desc = "；".join(
            f"{k}: {v['share']:.1%}" for k, v in top_combos
        )

        narration = (
            f"{dim_cols[0]} × {dim_cols[1]} 交叉分析: "
            f"共 {metrics['combinations']} 种组合。"
            f"贡献前三: {top_desc}。"
        )

        return SubAnalysisResult(
            task_id="drill_down",
            metrics=metrics,
            narration=narration,
        )

    def _analyze_distribution(
        self, df: pd.DataFrame, numeric_cols: List[str]
    ) -> SubAnalysisResult:
        """
        数值分布分析 → 手册「第九章」

        对每个数值列计算偏度、峰度、正态性检验（Shapiro-Wilk），
        判断分布形态。

        Args:
            df:           清洗后的 DataFrame
            numeric_cols: 待分析的数值列名列表（最多5列）

        Returns:
            含有分布参数和形态判断的分析结果
        """
        dist_report = {}
        shapes = []

        for col in numeric_cols:
            clean = df[col].dropna()
            if len(clean) < 3:
                continue

            col_stats = {
                "skewness": round(float(scipy_stats.skew(clean)), 4),
                "kurtosis": round(float(scipy_stats.kurtosis(clean)), 4),
            }

            # Shapiro-Wilk 正态性检验 (手册 9.1: 3 ≤ n ≤ 5000)
            if 3 <= len(clean) <= 5000:
                shapiro_stat, shapiro_p = scipy_stats.shapiro(clean)
                col_stats["shapiro_stat"] = round(float(shapiro_stat), 4)
                col_stats["shapiro_p"] = round(float(shapiro_p), 6)
                col_stats["is_normal"] = shapiro_p > 0.05
            else:
                col_stats["shapiro_p"] = None
                col_stats["is_normal"] = None

            # 分布形态判断 → 手册第九章判读标准
            sk = col_stats["skewness"]
            kt = col_stats["kurtosis"]
            if abs(sk) < 0.5 and abs(kt) < 1:
                shape = "近似正态"
            elif sk > 1:
                shape = "严重右偏（长尾在高值端）"
            elif sk < -1:
                shape = "严重左偏（长尾在低值端）"
            elif sk > 0.5:
                shape = "轻度右偏"
            elif sk < -0.5:
                shape = "轻度左偏"
            else:
                shape = "对称但厚尾" if kt > 1 else "近似正态"
            col_stats["dist_shape"] = shape

            dist_report[col] = col_stats
            shapes.append(f"{col}: {shape}")

        metrics = {
            "columns_analyzed": len(dist_report),
            "distributions": dist_report,
        }

        narration = (
            f"分析 {len(dist_report)} 个数值列的分布形态。"
            + " ".join(shapes[:5]) + "。"
        )

        return SubAnalysisResult(
            task_id="distribution",
            metrics=metrics,
            narration=narration,
        )

    def _analyze_yoy(
        self, df: pd.DataFrame, date_col: str, metric_col: str
    ) -> SubAnalysisResult:
        """
        同比增长率分析 → 手册「4.3」

        对月度数据计算同期对比（YoY）。

        Args:
            df:         清洗后的 DataFrame
            date_col:   日期列名
            metric_col: 分析指标列名

        Returns:
            含有同比数据的分析结果
        """
        ts = df.copy()
        ts[date_col] = pd.to_datetime(ts[date_col], errors="coerce")
        ts = ts.dropna(subset=[date_col, metric_col])
        ts = ts.set_index(date_col).sort_index()

        monthly = ts[metric_col].resample("ME").sum()

        # 同比: 与12个月前对比
        if len(monthly) < 13:
            return SubAnalysisResult(
                task_id="yoy_comparison",
                status="skipped",
                error_message="数据不足12个月，无法计算同比",
            )

        yoy_growth = {}
        for i in range(12, len(monthly)):
            curr = monthly.iloc[i]
            prev = monthly.iloc[i - 12]
            if prev != 0:
                yoy_growth[str(monthly.index[i].date())] = round(
                    float((curr - prev) / prev), 4
                )

        valid_yoy = [v for v in yoy_growth.values() if v is not None]
        avg_yoy = round(float(np.mean(valid_yoy)), 4) if valid_yoy else None

        metrics = {
            "yoy_growth": yoy_growth,
            "avg_yoy": avg_yoy,
            "comparable_periods": len(yoy_growth),
            "positive_yoy": sum(1 for v in valid_yoy if v > 0),
            "negative_yoy": sum(1 for v in valid_yoy if v < 0),
        }

        narration = (
            f"共 {metrics['comparable_periods']} 期可计算同比，"
            f"平均同比{'增长' if avg_yoy and avg_yoy > 0 else '下降'}"
            f"{abs(avg_yoy):.1%}。" if avg_yoy else "数据不足。"
        )

        return SubAnalysisResult(
            task_id="yoy_comparison",
            metrics=metrics,
            narration=narration,
        )

    def _analyze_rfm(
        self, df: pd.DataFrame, customer_col: str,
        date_col: str, revenue_col: str
    ) -> SubAnalysisResult:
        """
        RFM 客户分析 → 手册「第十一章」

        计算每个客户的 R/F/M 值，打分并分层。

        Args:
            df:           清洗后的 DataFrame
            customer_col: 客户列名
            date_col:     日期列名
            revenue_col:  收入列名

        Returns:
            含有客户分层结果的分析结果
        """
        rfm_df = df.copy()
        rfm_df[date_col] = pd.to_datetime(rfm_df[date_col], errors="coerce")
        rfm_df = rfm_df.dropna(subset=[customer_col, date_col, revenue_col])

        if rfm_df.empty:
            return SubAnalysisResult(
                task_id="rfm_analysis",
                status="skipped",
                error_message="有效 RFM 数据为空",
            )

        # 分析截止日 = 数据中最大日期 + 1天
        analysis_date = rfm_df[date_col].max() + pd.Timedelta(days=1)

        # 计算 R/F/M 原始值
        rfm = rfm_df.groupby(customer_col).agg(
            r_value=(date_col, lambda x: (analysis_date - x.max()).days),
            f_value=(date_col, "count"),
            m_value=(revenue_col, "sum"),
        )

        if len(rfm) < 2:
            return SubAnalysisResult(
                task_id="rfm_analysis",
                status="skipped",
                error_message="客户数不足（需≥2）",
            )

        # 5分制打分（手册 11.2）
        # R: 值越小分越高（越近越好）
        rfm["r_score"] = pd.qcut(
            rfm["r_value"].rank(method="first"), 5, labels=[5, 4, 3, 2, 1]
        ).astype(int)

        # F: 值越大分越高
        rfm["f_score"] = pd.qcut(
            rfm["f_value"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5]
        ).astype(int)

        # M: 值越大分越高
        rfm["m_score"] = pd.qcut(
            rfm["m_value"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5]
        ).astype(int)

        rfm["rfm_total"] = rfm["r_score"] + rfm["f_score"] + rfm["m_score"]

        # 客户分层（手册 11.3）
        def classify(row):
            if row["r_score"] >= 4 and row["f_score"] >= 4 and row["m_score"] >= 4:
                return "high_value"
            elif row["r_score"] >= 3 and row["f_score"] <= 3 and row["m_score"] >= 3:
                return "important_develop"
            elif row["r_score"] <= 2 and row["f_score"] >= 3 and row["m_score"] >= 3:
                return "important_retain"
            elif row["r_score"] >= 3 and row["f_score"] <= 3 and row["m_score"] <= 3:
                return "general"
            elif row["r_score"] <= 2 and row["f_score"] <= 2:
                return "at_risk"
            else:
                return "general"

        rfm["segment"] = rfm.apply(classify, axis=1)

        segment_counts = rfm["segment"].value_counts().to_dict()
        segment_revenue = (
            rfm.groupby("segment")["m_value"].sum()
            / rfm["m_value"].sum()
        ).to_dict()

        metrics = {
            "total_customers": len(rfm),
            "rfm_scores": {
                "r_mean": round(float(rfm["r_value"].mean()), 1),
                "f_mean": round(float(rfm["f_value"].mean()), 1),
                "m_mean": round(float(rfm["m_value"].mean()), 1),
            },
            "segment_counts": {k: int(v) for k, v in segment_counts.items()},
            "segment_revenue_share": {
                k: round(float(v), 4) for k, v in segment_revenue.items()
            },
        }

        at_risk = segment_counts.get("at_risk", 0)
        high_val = segment_counts.get("high_value", 0)
        narration = (
            f"共 {metrics['total_customers']} 个客户。"
            f"高价值 {high_val} 人，流失风险 {at_risk} 人。"
        )
        if at_risk > high_val:
            narration += " 流失风险客户多于高价值客户，需重点关注客户留存。"

        return SubAnalysisResult(
            task_id="rfm_analysis",
            metrics=metrics,
            narration=narration,
        )

    def _analyze_forecast(
        self, df: pd.DataFrame, date_col: str, metric_col: str
    ) -> SubAnalysisResult:
        """
        简单趋势预测 → 手册「第十二章」

        使用线性回归对时序数据进行趋势外推。

        Args:
            df:         清洗后的 DataFrame
            date_col:   日期列名
            metric_col: 预测指标列名

        Returns:
            含有预测值和置信区间的分析结果
        """
        ts = df.copy()
        ts[date_col] = pd.to_datetime(ts[date_col], errors="coerce")
        ts = ts.dropna(subset=[date_col, metric_col])
        ts = ts.set_index(date_col).sort_index()

        monthly = ts[metric_col].resample("ME").sum()

        if len(monthly) < 12:
            return SubAnalysisResult(
                task_id="simple_forecast",
                status="skipped",
                error_message="数据不足12期，无法做趋势预测",
            )

        # 线性回归 (手册 12.1)
        x = np.arange(len(monthly)).reshape(-1, 1)
        y = monthly.values
        slope, intercept, r_value, _, _ = scipy_stats.linregress(
            x.ravel(), y
        )

        # 拟合值与残差
        fitted = slope * x.ravel() + intercept
        residuals = y - fitted
        sigma_resid = float(np.std(residuals))

        # 下期预测
        next_x = len(monthly)
        next_forecast = round(float(slope * next_x + intercept), 2)

        # 预测区间 (手册 12.2: 80% ≈ ±1.28σ, 95% ≈ ±1.96σ)
        interval_80 = round(1.28 * sigma_resid, 2)
        interval_95 = round(1.96 * sigma_resid, 2)

        metrics = {
            "trend_slope": round(float(slope), 4),
            "trend_intercept": round(float(intercept), 2),
            "trend_r2": round(float(r_value ** 2), 4),
            "sigma_residual": round(sigma_resid, 2),
            "next_forecast": next_forecast,
            "forecast_interval_80": (round(next_forecast - interval_80, 2), round(next_forecast + interval_80, 2)),
            "forecast_interval_95": (round(next_forecast - interval_95, 2), round(next_forecast + interval_95, 2)),
            "fitted_values": {str(monthly.index[i].date()): round(float(v), 2) for i, v in enumerate(fitted)},
        }

        direction = "增长" if slope > 0 else "下降"
        narration = (
            f"线性模型 R²={metrics['trend_r2']:.2%}，"
            f"趋势斜率为每期{metrics['trend_slope']:+.2f}（{direction}）。"
            f"下期预测值 {next_forecast:,.0f}，"
            f"95%置信区间 [{metrics['forecast_interval_95'][0]:,.0f}, {metrics['forecast_interval_95'][1]:,.0f}]。"
        )

        return SubAnalysisResult(
            task_id="simple_forecast",
            metrics=metrics,
            narration=narration,
        )

    # ------------------------------------------------------------------
    # Step 6: 综合洞察
    # ------------------------------------------------------------------

    def _synthesize_insights(self, data: CsvDataSource) -> List[Insight]:
        """
        综合所有子分析结果，生成跨维度洞察。

        策略（启发式规则，生产环境应替换为 LLM 调用）:
        1. 从描述统计提取数据规模洞察
        2. 从时序分析提取趋势洞察
        3. 从排名分析提取集中度洞察
        4. 从异常检测提取数据质量洞察

        Args:
            data: 数据源（用于获取 focus_questions）

        Returns:
            3-8 条分级洞察
        """
        insights = []
        idx = 0

        # --- 洞察 1: 数据规模 ---
        desc_result = next(
            (a for a in self.analyses if a.task_id == "desc_stats"), None
        )
        if desc_result and desc_result.status == "success":
            m = desc_result.metrics
            insights.append(Insight(
                id=f"i{idx:02d}",
                title=f"数据集包含 {m.get('row_count', '?')} 条记录",
                severity="info",
                evidence=[f"{m.get('row_count', '?')} 行 × {m.get('col_count', '?')} 列"],
                interpretation=f"数据规模适中，适合进行多维度统计分析。",
            ))
            idx += 1

        # --- 洞察 2: 趋势方向 ---
        ts_result = next(
            (a for a in self.analyses if a.task_id == "time_series"), None
        )
        if ts_result and ts_result.status == "success":
            m = ts_result.metrics
            monthly_vals = m.get("monthly_values", {})
            if monthly_vals:
                vals = list(monthly_vals.values())
                if len(vals) >= 2:
                    first, last = vals[0], vals[-1]
                    change = (last - first) / first if first != 0 else 0
                    insights.append(Insight(
                        id=f"i{idx:02d}",
                        title=f"整体趋势{'上升' if change > 0 else '下降'}，变动幅度 {abs(change):.1%}",
                        severity="info" if abs(change) < 0.3 else "warning",
                        evidence=[
                            f"首期: {first:,.0f}，末期: {last:,.0f}",
                            f"变动: {change:+.1%}",
                        ],
                        interpretation=(
                            f"数据呈{('上升' if change > 0 else '下降')}趋势，"
                            f"建议{'关注增长驱动因素' if change > 0 else '排查下降原因'}。"
                        ),
                    ))
                    idx += 1

        # --- 洞察 3: 集中度 ---
        ranking = next(
            (a for a in self.analyses if a.task_id == "top_ranking"), None
        )
        if ranking and ranking.status == "success":
            cr5 = ranking.metrics.get("cr5", 0)
            if cr5 > 0.6:
                insights.append(Insight(
                    id=f"i{idx:02d}",
                    title=f"头部集中度较高（CR5={cr5:.0%}）",
                    severity="warning",
                    evidence=[f"前5名合计占比 {cr5:.1%}"],
                    interpretation="少数核心产品/客户贡献了大部分收入，存在依赖风险。",
                ))
                idx += 1

        # --- 洞察 4: 数据质量 ---
        if self.quality:
            if self.quality.score < 70:
                insights.append(Insight(
                    id=f"i{idx:02d}",
                    title=f"数据质量偏低（{self.quality.score}分）",
                    severity="critical" if self.quality.score < 50 else "warning",
                    evidence=[
                        f"缺失率>20%的列: {sum(1 for v in self.quality.null_summary.values() if v > 0.2)}",
                        f"重复行: {self.quality.duplicate_rows}",
                    ],
                    interpretation="数据质量问题可能影响分析的准确性，建议先清洗数据后再分析。",
                ))
                idx += 1
            else:
                insights.append(Insight(
                    id=f"i{idx:02d}",
                    title=f"数据质量良好（{self.quality.score}分）",
                    severity="info",
                    evidence=[f"总体缺失率低，重复行 {self.quality.duplicate_rows} 行"],
                    interpretation="数据质量可以支撑可靠的统计分析。",
                ))
                idx += 1

        # --- 洞察 5: ABC 长尾 ---
        pareto = next(
            (a for a in self.analyses if a.task_id == "pareto_abc"), None
        )
        if pareto and pareto.status == "success":
            c_count = pareto.metrics.get("C", {}).get("count", 0)
            if c_count > 0:
                insights.append(Insight(
                    id=f"i{idx:02d}",
                    title=f"长尾产品占比偏高（C类 {c_count} 个）",
                    severity="warning" if c_count > 20 else "info",
                    evidence=[
                        f"C类产品 {c_count} 个，合计贡献仅 {pareto.metrics['C']['revenue_share']:.1%}"
                    ],
                    interpretation="建议评估长尾产品的清理或合并机会，释放管理资源。",
                ))
                idx += 1

        # 限制最多 8 条
        return insights[:8]

    # ------------------------------------------------------------------
    # 输出控制 — slim 裁剪 + 断路器
    # ------------------------------------------------------------------

    def _apply_slim_output(self, output: AnalysisOutput) -> AnalysisOutput:
        """
        应用 slim_output 裁剪：去掉完整矩阵、时序明细、top10 列表。

        仅保留聚合值，显著减小输出体积（可减少 50-75%）。
        适用于 Host Agent 对上下文大小敏感的场景。
        """
        for a in output.analyses:
            if a.status != "success":
                continue
            slim = self._slim_metrics(a.task_id, a.metrics)
            # 保留完整 metrics 中的 slim 字段
            slim_metrics = {k: a.metrics[k] for k in slim if k in a.metrics}
            # 注入裁剪标记
            slim_metrics["_slim"] = True
            a.metrics = slim_metrics

        return output

    def _check_output_size(self, output: AnalysisOutput) -> AnalysisOutput:
        """
        输出断路器：检查序列化后的大小，超过 MAX_OUTPUT_TOKENS 时自动裁剪。

        策略:
            1. 序列化输出为 JSON 字符串
            2. 估算 token 数（字符数 ÷ 4）
            3. 超过阈值时自动触发 slim_output 裁剪
            4. 裁剪后仍超限时记录警告但不阻断
        """
        try:
            size_json = len(json.dumps(output.to_dict(), default=str, ensure_ascii=False))
            est_tokens = size_json // 4
        except Exception:
            return output  # 序列化失败时跳过检查

        if est_tokens <= MAX_OUTPUT_TOKENS:
            return output

        # 超过阈值，自动触发裁剪
        logger.warning(
            f"输出 {est_tokens} token 超过上限 {MAX_OUTPUT_TOKENS}，自动触发 slim 裁剪"
        )
        output = self._apply_slim_output(output)

        # 裁剪后重新检查
        try:
            size_json2 = len(json.dumps(output.to_dict(), default=str, ensure_ascii=False))
            est_tokens2 = size_json2 // 4
        except Exception:
            return output

        if est_tokens2 > MAX_OUTPUT_TOKENS:
            logger.warning(
                f"裁剪后 {est_tokens2} token 仍超限，进一步精简 data_profile 和 data_facts"
            )
            # 极限裁剪：清空大体积字段
            output.data_profile = output.data_profile[:5]  # 仅保留前5列
            output.data_facts = ""  # 清空事实块

        return output

    # ------------------------------------------------------------------
    # Step 7 & 8: 摘要与事实块
    # ------------------------------------------------------------------

    def _build_executive_summary(self) -> str:
        """
        生成 200 字以内的执行摘要。

        汇总: 数据规模 + 关键趋势 + 最严重洞察

        Returns:
            纯文本摘要
        """
        parts = []

        # 数据规模
        if self.parse_meta:
            parts.append(
                f"数据集 {self.parse_meta.row_count} 行 × {self.parse_meta.col_count} 列"
            )

        # 关键指标
        ts = next((a for a in self.analyses if a.task_id == "time_series"), None)
        if ts and ts.status == "success":
            monthly = ts.metrics.get("monthly_values", {})
            if monthly:
                total = sum(monthly.values())
                parts.append(f"总计 {total:,.0f}")

        # 最严重洞察
        criticals = [i for i in self.insights if i.severity == "critical"]
        warnings = [i for i in self.insights if i.severity == "warning"]
        priority = criticals + warnings
        if priority:
            parts.append(f"关注: {priority[0].title}")

        # 数据质量
        if self.quality:
            parts.append(f"数据质量 {self.quality.score}/100")

        return "。".join(parts) + "。"

    def _build_data_facts(self) -> str:
        """
        生成 Markdown 格式的数据事实块。

        供 Host Agent 注入后续对话使用，只包含聚合值，不含原始行数据。

        Returns:
            Markdown 字符串
        """
        lines = ["## 数据概览", ""]

        # 基本信息
        if self.parse_meta:
            lines.append(f"- 总行数: {self.parse_meta.row_count}")
            lines.append(f"- 总列数: {self.parse_meta.col_count}")
            if self.parse_meta.delimiter:
                lines.append(f"- CSV 分隔符: `{self.parse_meta.delimiter}`")
            if self.parse_meta.encoding:
                lines.append(f"- 编码: {self.parse_meta.encoding}")

        # 质量
        if self.quality:
            lines.append(f"- 数据质量评分: {self.quality.score}/100")
            lines.append(f"- 重复行: {self.quality.duplicate_rows}")

        lines.append("")

        # 关键指标表格
        ts = next((a for a in self.analyses if a.task_id == "time_series"), None)
        if ts and ts.status == "success":
            monthly = ts.metrics.get("monthly_values", {})
            if monthly and len(monthly) <= 12:
                lines.append("## 月度数据")
                lines.append("")
                lines.append("| 月份 | 金额 | 环比 |")
                lines.append("|------|------|------|")
                mom = ts.metrics.get("mom_growth", {})
                prev_val = None
                for period, val in list(monthly.items()):
                    mom_val = mom.get(period)
                    mom_str = f"{mom_val:+.1%}" if mom_val is not None else "-"
                    lines.append(f"| {period} | {val:,.0f} | {mom_str} |")
                lines.append("")

        # 区域分布
        region = next((a for a in self.analyses if a.task_id == "region_analysis"), None)
        if region and region.status == "success":
            regions = region.metrics.get("regions", {})
            if regions and len(regions) <= 10:
                lines.append("## 区域分布")
                lines.append("")
                lines.append("| 区域 | 合计 | 占比 |")
                lines.append("|------|------|------|")
                for r_name, r_data in sorted(
                    regions.items(), key=lambda x: x[1]["share"], reverse=True
                ):
                    lines.append(
                        f"| {r_name} | {r_data['total']:,.0f} | {r_data['share']:.1%} |"
                    )
                lines.append("")

        return "\n".join(lines)


# ============================================================================
# 团队级分析器 — 集成鉴权 + 用量追踪（企业级中间件）
# ============================================================================

class TeamCsvAnalyzer:
    """
    团队级 CSV 分析器 —— 在 CsvAnalyzer 外层加入鉴权和用量追踪。

    这是面向企业团队使用的推荐入口。
    团队成员通过 API Key 调用，自动完成权限校验、速率限制和用量记录。

    使用方式:
        from scripts.auth import AuthManager
        from scripts.tracker import UsageTracker
        from scripts.csv_analyzer import TeamCsvAnalyzer, CsvDataSource, AnalysisConfig

        auth = AuthManager("config/team.yaml")
        tracker = UsageTracker("logs/usage.jsonl")

        analyzer = TeamCsvAnalyzer(auth=auth, tracker=tracker, model="claude-sonnet-4-6")

        # 团队成员调用
        result = analyzer.run_for_user(
            api_key="sk-xxx",
            data=CsvDataSource(file_path="/data/sales.csv"),
            depth="standard",
        )
    """

    # 延迟导入以避免循环依赖（运行时才需要）
    AuthError = None
    RateLimitError = None
    QuotaExceededError = None

    def __init__(
        self,
        auth: "AuthManager",
        tracker: "UsageTracker",
        model: str = "claude-sonnet-4-6",
    ):
        """
        Args:
            auth:   鉴权管理器实例
            tracker: 用量追踪器实例
            model:   默认使用的 LLM 模型（用于成本核算）
        """
        # 延迟导入
        from auth import AuthError as _AuthError
        TeamCsvAnalyzer.AuthError = _AuthError

        self._auth = auth
        self._tracker = tracker
        self._model = model

    def run_for_user(
        self,
        api_key: str,
        data: CsvDataSource,
        depth: str = "standard",
        focus_questions: Optional[List[str]] = None,
        column_hints: Optional[Dict[str, str]] = None,
        encoding: Optional[str] = None,
        delimiter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        团队成员入口 —— 完整的鉴权→分析→追踪流程。

        流程:
        1. 验证 API Key → 获取用户上下文
        2. 校验角色权限（是否有权使用该深度）
        3. 检查速率限制 + 日配额
        4. 执行分析
        5. 记录用量
        6. 返回结果

        Args:
            api_key:        用户 API Key
            data:           数据源
            depth:          分析深度
            focus_questions: 业务问题
            column_hints:   手动列语义
            encoding:       编码
            delimiter:      分隔符

        Returns:
            分析结果 dict + _team_meta（用量信息）

        Raises:
            AuthError:          鉴权失败
            RateLimitError:     速率超限
            QuotaExceededError: 配额超限
        """
        # 1. 鉴权
        user = self._auth.authenticate(api_key)

        # 2. 权限校验
        effective_depth = depth or user.default_depth
        self._auth.authorize(user, effective_depth)

        # 3. 速率 + 配额
        self._auth.check_rate_limit(user.tenant_id)
        self._auth.check_daily_quota(user)

        # 4. 执行分析
        config = AnalysisConfig(
            analysis_depth=effective_depth,
            focus_questions=focus_questions,
            column_hints=column_hints,
            encoding=encoding,
            delimiter=delimiter,
            output_locale=user.output_locale,
        )
        analyzer = CsvAnalyzer(config=config)
        start = time.time()
        output = analyzer.run(data)
        elapsed = time.time() - start

        # 5. 估算 token（非精确，生产环境从 LLM response 中取实际值）
        token_est = {
            "quick": (2700, 1200),
            "standard": (5600, 2800),
            "deep": (8500, 4500),
        }
        t_in, t_out = token_est.get(effective_depth, (5600, 2800))

        # 6. 记录
        self._auth.record_call(user, effective_depth, t_in + t_out, output.status)
        self._tracker.log(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            user_name=user.user_name,
            depth=effective_depth,
            model=self._model,
            token_input=t_in,
            token_output=t_out,
            status=output.status,
            elapsed_seconds=round(elapsed, 1),
        )

        # 7. 附加团队元信息
        result = output.to_dict()
        result["_team_meta"] = {
            "tenant_id": user.tenant_id,
            "user_name": user.user_name,
            "role": user.role,
            "daily_remaining": user.daily_limit - 1,  # 近似值
        }

        return result

    def get_team_usage(self, api_key: str) -> Dict[str, Any]:
        """
        查询团队用量（仅 admin 角色可用）。

        Args:
            api_key: 管理员 API Key

        Returns:
            用量报告 dict
        """
        user = self._auth.authenticate(api_key)
        if user.role != "admin":
            raise TeamCsvAnalyzer.AuthError("仅管理员可查看团队用量")

        daily = self._tracker.daily_report(user.tenant_id)
        monthly = self._tracker.monthly_report(user.tenant_id)
        budget_alert = self._tracker.budget_alert(
            user.tenant_id,
            self._auth._get_team(user.tenant_id).get("monthly_budget", 200)
            if self._auth._get_team(user.tenant_id) else 200,
        )

        return {
            "tenant_id": user.tenant_id,
            "today": {
                "calls": daily.total_calls,
                "tokens": daily.total_tokens,
                "cost": daily.total_cost_usd,
                "by_user": daily.by_user,
            },
            "this_month": {
                "calls": monthly.total_calls,
                "tokens": monthly.total_tokens,
                "cost": monthly.total_cost_usd,
                "avg_per_call": monthly.avg_cost_per_call,
            },
            "budget_alert": budget_alert,
        }

    def get_auth(self) -> "AuthManager":
        """获取鉴权管理器（供外部查询团队列表等）"""
        return self._auth

    def get_tracker(self) -> "UsageTracker":
        """获取追踪器（供外部生成自定义报告）"""
        return self._tracker


# ============================================================================
# 模块级快捷函数 — 简化常见调用场景
# ============================================================================

def analyze_csv(
    file_path: str,
    depth: Literal["quick", "standard", "deep"] = "standard",
    focus_questions: Optional[List[str]] = None,
    column_hints: Optional[Dict[str, str]] = None,
    encoding: Optional[str] = None,
    delimiter: Optional[str] = None,
) -> Dict[str, Any]:
    """
    分析指定的 CSV 文件并返回完整分析结果字典。

    这是最常用的快捷入口——传入文件路径，获得完整的结构化分析结果。
    适合 Host Agent 直接调用并消费。

    Args:
        file_path:       CSV 文件的绝对路径
        depth:           分析深度: "quick"(快速) / "standard"(标准) / "deep"(深度)
        focus_questions: 用户关注的具体问题列表，如 ["为什么Q3下滑？"]
        column_hints:    手动列语义标注，如 {"日期": "date", "金额": "revenue"}
        encoding:        CSV 文件编码，不指定则自动检测
        delimiter:       CSV 分隔符，不指定则自动检测

    Returns:
        dict: 完整的结构化分析结果，包含以下关键字段:
            - status:           "success" / "partial" / "error"
            - executive_summary: 200字执行摘要
            - data_facts:        Markdown 格式数据事实块
            - insights:          3-8条综合洞察
            - analyses:          所有子分析结果列表
            - quality:           数据质量评分与清洗记录
            - column_roles:      列名→语义角色映射

    Example:
        >>> result = analyze_csv("/data/sales_q4.csv", depth="standard")
        >>> print(result["executive_summary"])
        >>> for insight in result["insights"]:
        ...     print(f"[{insight['severity']}] {insight['title']}")
    """
    config = AnalysisConfig(
        analysis_depth=depth,
        focus_questions=focus_questions,
        column_hints=column_hints,
        encoding=encoding,
        delimiter=delimiter,
    )
    data = CsvDataSource(file_path=file_path)
    analyzer = CsvAnalyzer(config=config)
    output = analyzer.run(data)
    return output.to_dict()


def quick_summary(
    file_path: str,
    encoding: Optional[str] = None,
    delimiter: Optional[str] = None,
) -> Dict[str, str]:
    """
    快速分析 CSV 文件并仅返回摘要信息。

    适用于 Agent 需要快速判断数据内容、不需要完整分析的场景。
    仅执行描述统计 + 数据画像，比完整分析快 3-5 倍。

    Args:
        file_path: CSV 文件的绝对路径
        encoding:  CSV 文件编码，不指定则自动检测
        delimiter: CSV 分隔符，不指定则自动检测

    Returns:
        dict: 包含以下键的简化结果:
            - summary:        一句话数据摘要
            - row_count:      行数
            - col_count:      列数
            - column_names:   列名列表
            - quality_score:  数据质量评分 (0-100)
            - has_date:       是否包含日期列
            - has_numeric:    是否包含数值列
            - sample_rows:    前 3 行样本数据（dict 格式）

    Example:
        >>> summary = quick_summary("/data/unknown.csv")
        >>> print(summary["summary"])
        "5000行×8列的表格数据，含日期列和5个数值列，数据质量92分"
    """
    config = AnalysisConfig(
        analysis_depth="quick",
        encoding=encoding,
        delimiter=delimiter,
    )
    data = CsvDataSource(file_path=file_path)
    analyzer = CsvAnalyzer(config=config)
    output = analyzer.run(data)

    # 从完整输出中提取关键摘要字段
    has_date = any(r.role == "date" for r in output.column_roles.values())
    has_numeric = any(
        r.role in ("revenue", "quantity", "cost", "profit", "metric")
        for r in output.column_roles.values()
    )
    col_names = list(output.column_roles.keys()) if output.column_roles else []

    # 构建一句话摘要
    quality_str = f"数据质量{output.quality.score}分" if output.quality else "质量未知"
    summary = (
        f"{output.parse_meta.row_count}行×{output.parse_meta.col_count}列的表格数据，"
        f"{'含日期列，' if has_date else ''}"
        f"{'含' + str(sum(1 for r in output.column_roles.values() if r.role in ('revenue','quantity','cost','profit','metric'))) + '个数值列，' if has_numeric else ''}"
        f"{quality_str}"
    )

    # 前 3 行样本
    sample_rows = []
    if analyzer.raw_df is not None:
        sample_rows = analyzer.raw_df.head(3).to_dict(orient="records")

    return {
        "summary": summary,
        "row_count": output.parse_meta.row_count if output.parse_meta else 0,
        "col_count": output.parse_meta.col_count if output.parse_meta else 0,
        "column_names": col_names,
        "quality_score": output.quality.score if output.quality else 0,
        "has_date": has_date,
        "has_numeric": has_numeric,
        "sample_rows": sample_rows,
    }


# ============================================================================
# 命令行入口 — 可直接通过 python csv_analyzer.py <file_path> 使用
# ============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python csv_analyzer.py <csv_file_path> [quick|standard|deep]")
        print("示例: python csv_analyzer.py sales.csv standard")
        sys.exit(1)

    path = sys.argv[1]
    depth = sys.argv[2] if len(sys.argv) > 2 else "standard"

    if depth not in ("quick", "standard", "deep"):
        print(f"无效的分析深度 '{depth}'，使用默认值 'standard'")
        depth = "standard"

    print(f"正在分析: {path} (深度: {depth})")
    print("-" * 60)

    result = analyze_csv(path, depth=depth)

    if result["status"] == "error":
        print(f"分析失败: {result.get('executive_summary', '未知错误')}")
        sys.exit(1)

    print(f"状态: {result['status']}")
    print(f"耗时: {result['elapsed_seconds']}s")
    print()
    print("=" * 60)
    print("执行摘要")
    print("=" * 60)
    print(result["executive_summary"])
    print()
    print("=" * 60)
    print("洞察")
    print("=" * 60)
    for insight in result["insights"]:
        print(f"  [{insight['severity'].upper()}] {insight['title']}")
    print()
    print("=" * 60)
    print("数据事实")
    print("=" * 60)
    print(result["data_facts"])
