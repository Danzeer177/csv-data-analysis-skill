# CSV Data Analysis Skill — 使用说明

> **版本**: v1.0.0 | **发布日期**: 2026-06-21 | **许可证**: MIT

---

## 目录

1. [简介](#简介)
2. [快速开始](#快速开始)
3. [安装](#安装)
4. [在任何电脑上从零部署](#在任何电脑上从零部署)
5. [三种使用模式](#三种使用模式)
6. [配置说明](#配置说明)
7. [分析深度对照](#分析深度对照)
8. [输出结构](#输出结构)
9. [安全配置](#安全配置)
10. [常见问题](#常见问题)

---

## 简介

CSV Data Analysis Skill 是一个基于 **Pandas 精确计算 + LLM 语义解读** 的 CSV/Excel 数据分析引擎。

**核心特点**：
- 🔢 **12 种多维分析**：描述统计、时序趋势、同比对比、排名、ABC帕累托、异常检测、相关性、分布、区域、下钻、RFM、预测
- 🛡️ **数据隔离**：LLM 绝不接触原始行级数据，所有计算 100% 由 Python/Pandas 完成
- 🔗 **Agent 原生集成**：LangGraph 工作流编排 + Send API 并行扇出
- 🏢 **多租户支持**：API Key 鉴权 + 角色权限 + 速率限制 + 用量追踪
- ⚡ **Token 效率优化**：batch_narrate 合并调用、correlation 上限保护、时序降采样、输出断路器

---

## 快速开始

### 最简用法（2 行代码）

```python
from scripts.csv_analyzer import analyze_csv

result = analyze_csv("sales_2025_q4.csv", depth="standard")
print(result["executive_summary"])
```

### 命令行直接运行

```bash
python scripts/csv_analyzer.py sales_2025_q4.csv standard
```

### API 服务模式

```bash
pip install -r requirements-api.txt
python api_server.py
# 服务启动在 http://localhost:8080
# API 文档: http://localhost:8080/docs
```

---

## 安装

### 环境要求

- Python >= 3.10
- pip >= 23.0

### 核心分析器

```bash
pip install -r requirements.txt
```

包含：pandas, numpy, scipy, matplotlib, seaborn, pydantic, chardet, openpyxl, langgraph, pyyaml

### API 服务模式

```bash
pip install -r requirements-api.txt
```

额外包含：fastapi, uvicorn, python-multipart

### 开发/测试模式

```bash
pip install -r requirements-dev.txt
```

额外包含：pytest, pytest-asyncio, httpx

### 精确版本锁定（生产环境推荐）

```bash
pip install -r requirements.lock.txt
```

### pip 包安装（推荐 — 自动解决所有路径）

```bash
# 方式A: 可编辑安装（开发模式，修改代码立即生效）
pip install -e .

# 方式B: 含 API 服务依赖
pip install -e ".[api]"

# 方式C: 含全部依赖（API + 测试工具）
pip install -e ".[all]"
```

> 使用 `pip install -e .` 后，可在任意位置直接 `from csv_analyzer import CsvAnalyzer`，无需手动管理 `sys.path`。

---

## 在任何电脑上从零部署

以下步骤保证在 **Windows / macOS / Linux** 上均可运行。仅需 Python 3.10+。

### 标准部署流程

```bash
# 步骤 1: 克隆仓库
git clone https://github.com/your-org/csv-data-analysis-skill.git
cd csv-data-analysis-skill

# 步骤 2: 创建虚拟环境（推荐）
python -m venv venv
source venv/bin/activate      # Linux / macOS
# venv\Scripts\activate       # Windows

# 步骤 3: 安装（任选一种）
pip install -e .                        # pip 包模式，推荐
pip install -r requirements.txt         # 传统 requirements.txt

# 步骤 4: 验证安装
python -c "from scripts.csv_analyzer import analyze_csv; print('OK')"

# 步骤 5: 运行测试
pip install -r requirements-dev.txt
pytest tests/ -v
# 应显示: 101 passed
```

### 无 Git 部署（直接下载 ZIP）

```bash
# 下载 ZIP 并解压
unzip csv-data-analysis-skill.zip
cd csv-data-analysis-skill
# 后续步骤同标准部署的步骤 2-5
```

### 三种安装方式对比

| 方式 | 命令 | 适用场景 | 导入方式 |
|------|------|----------|----------|
| **pip 包模式** | `pip install -e .` | 开发/生产（推荐） | `from csv_analyzer import CsvAnalyzer` |
| **requirements.txt** | `pip install -r requirements.txt` | 仅用核心分析 | `from scripts.csv_analyzer import ...`（需 scripts/ 在 sys.path） |
| **完整安装** | `pip install -e ".[all]"` | 含 API 服务 + 测试 | 以上均可 |

### 跨平台兼容性

| 特性 | Windows | macOS | Linux |
|------|---------|-------|-------|
| 路径处理 | ✅ `os.path.join()` 自动适配 `\` | ✅ 自动适配 `/` | ✅ 自动适配 `/` |
| 编码检测 | ✅ UTF-8 / GBK / GB2312 | ✅ UTF-8 | ✅ UTF-8 |
| 终端输出 | ✅ `run_analysis.py` 自动判断平台 | ✅ 无特殊处理 | ✅ 无特殊处理 |
| 文件操作 | ✅ `tempfile` + `os.path.realpath()` | ✅ 同上 | ✅ 同上 |
| pip 安装 | ✅ | ✅ | ✅ |

### 依赖关系图

```
csv-data-analysis-skill
├── 核心 (requirements.txt)
│   ├── pandas>=2.0        # 数据处理引擎
│   ├── numpy>=1.24        # 数值计算
│   ├── scipy>=1.11        # 统计检验（推荐，缺失时部分功能降级）
│   ├── pydantic>=2.0      # 数据模型校验
│   ├── langgraph>=0.2     # 工作流编排
│   ├── matplotlib>=3.7    # 图表生成（可选，缺失时跳过）
│   ├── seaborn>=0.13      # 统计图表（可选）
│   ├── chardet>=5.0       # 编码检测（可选，缺失时回退尝试）
│   ├── openpyxl>=3.1      # Excel 读取
│   └── pyyaml>=6.0        # 团队鉴权配置
├── API 模式 (requirements-api.txt)
│   ├── fastapi>=0.104
│   ├── uvicorn>=0.24
│   └── python-multipart>=0.0.6
└── 开发 (requirements-dev.txt)
    ├── pytest>=7.0
    ├── pytest-asyncio>=0.21
    └── httpx>=0.25
```

---

## 三种使用模式

### 模式 A: 直接调用（Python 脚本）

```python
from scripts.csv_analyzer import CsvAnalyzer, AnalysisConfig, CsvDataSource

# 方式 1: 分析文件
config = AnalysisConfig(analysis_depth="standard")
data = CsvDataSource(file_path="/path/to/sales.csv")
analyzer = CsvAnalyzer(config=config)
output = analyzer.run(data)

# 方式 2: 分析内存字符串（Agent 间传递无需落盘）
data = CsvDataSource(content="date,revenue\n2025-01-01,1000\n2025-01-02,1500")
output = analyzer.run(data)

# 方式 3: 启用 LLM 解读
from your_llm_client import YourLLM
analyzer = CsvAnalyzer(config=config, llm=YourLLM())
output = analyzer.run(data)

# 方式 4: 精简输出（减少上下文占用）
config = AnalysisConfig(analysis_depth="standard", slim_output=True)
analyzer = CsvAnalyzer(config=config)
output = analyzer.run(data)
```

**快捷函数**：

```python
# 完整分析
from scripts.csv_analyzer import analyze_csv
result = analyze_csv("data.csv", depth="standard")

# 快速摘要
from scripts.csv_analyzer import quick_summary
summary = quick_summary("data.csv")
# → {"summary": "5000行×8列...", "quality_score": 92, "has_date": True, ...}
```

### 模式 B: LangGraph 编排（Agent 集成）

```python
from scripts.graph_builder import GraphAnalyzer

analyzer = GraphAnalyzer(interrupt_on_ambiguity=False)
result = analyzer.run(
    file_path="/path/to/sales.csv",
    depth="standard",
    focus_questions=["为什么Q3下滑？", "哪个产品毛利最高？"],
    column_hints={"下单日期": "date", "实收金额": "revenue"},
)

print(result["executive_summary"])
for insight in result["insights"]:
    print(f"[{insight['severity']}] {insight['title']}")
```

**流式输出**：

```python
for event in analyzer.stream_run(file_path="data.csv", depth="standard"):
    print(event)  # 逐节点状态更新
```

### 模式 C: API 服务

**启动服务**：

```bash
python api_server.py
# 或
uvicorn api_server:app --host 0.0.0.0 --port 8080
```

**文件上传**：

```bash
curl -X POST http://localhost:8080/analyze \
  -F "file=@sales.csv" \
  -F "depth=standard"
```

**内容直传**：

```bash
curl -X POST http://localhost:8080/analyze \
  -F "content=date,revenue
2025-01-01,1000
2025-01-02,1500" \
  -F "depth=quick"
```

**批量分析**：

```bash
curl -X POST http://localhost:8080/analyze/batch \
  -H "Content-Type: application/json" \
  -d '{"files_content": [{"content": "a,b\n1,2", "depth": "quick"}]}'
```

**带鉴权调用**：

```bash
curl -X POST http://localhost:8080/analyze \
  -H "X-API-Key: sk-your-team-key" \
  -F "file=@sales.csv"
```

**健康检查**：

```bash
curl http://localhost:8080/health
# → {"status": "healthy", "version": "2.0.0", "team_mode": false}
```

### 模式 D: LangChain Tool（Agent 集成）

```python
from agent_tool import create_csv_analysis_tool

tool = create_csv_analysis_tool()
# tool.name → "analyze_csv_data"
# 可直接注册到 LangChain Agent 的 tools 列表
```

---

## 配置说明

### AnalysisConfig 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `analysis_depth` | `"quick" \| "standard" \| "deep"` | `"standard"` | 分析深度 |
| `delimiter` | `str` | 自动检测 | CSV 分隔符 |
| `encoding` | `str` | 自动检测 | 文件编码 |
| `has_header` | `bool` | `True` | 第一行是否为表头 |
| `skip_rows` | `int` | `None` | 跳过前 N 行 |
| `comment_char` | `str` | `None` | 注释行前缀 |
| `focus_questions` | `List[str]` | `None` | 业务问题列表 |
| `date_range` | `(str, str)` | `None` | 时间范围 |
| `column_hints` | `Dict[str, str]` | `None` | 手动列语义标注 |
| `output_locale` | `"zh" \| "en"` | `"zh"` | 输出语言 |
| `excel_sheet` | `str` | `None` | Excel 工作表名 |
| `chart_dir` | `str` | `None` | 图表输出目录 |
| `slim_output` | `bool` | `False` | 裁剪输出模式 |

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:5173` | CORS 允许的域名（逗号分隔） |
| `RATE_LIMIT_RPS` | `10` | API 每秒最大请求数 |
| `RATE_WINDOW` | `1.0` | 速率窗口（秒） |

### 团队鉴权配置

```bash
cp config/team.yaml.example config/team.yaml
```

编辑 `config/team.yaml`：

```yaml
teams:
  - tenant_id: "team_default"
    name: "默认团队"
    monthly_budget: 200.0
    users:
      - user_id: "admin01"
        user_name: "管理员"
        api_key: "sk-your-admin-key"
        role: "admin"       # admin / analyst / viewer
        daily_limit: 50
```

---

## 分析深度对照

| 深度 | 分析数量 | 包含的分析 | 耗时（5K行） | 适用场景 |
|------|----------|------------|-------------|----------|
| **quick** | ≤3 | 描述统计 + 时序 + 排名 | ~15s | 快速扫描、数据探索 |
| **standard** | ≤7 | quick + ABC + 异常 + 相关性 + 分布 + 区域 | ~30s | 日常分析、报告生成 |
| **deep** | ≤10 | standard + RFM + 预测 + 同比 | ~60s | 深度分析、战略决策 |

---

## 输出结构

```json
{
  "status": "success",
  "execution_id": "a1b2c3d4e5f6",
  "elapsed_seconds": 0.5,
  "source_info": {
    "type": "csv_file",
    "file_name": "sales_2025_q4.csv",
    "file_size_mb": 0.02
  },
  "parse_meta": {
    "delimiter": ",",
    "encoding": "utf-8",
    "row_count": 159,
    "col_count": 9
  },
  "data_profile": [
    {
      "name": "revenue",
      "dtype": "float64",
      "null_rate": 0.0,
      "min_val": 100.0,
      "max_val": 5000.0,
      "mean_val": 1250.5,
      "std_val": 890.3
    }
  ],
  "quality": {
    "score": 95,
    "null_summary": {"revenue": 0.0, "date": 0.01},
    "duplicate_rows": 0,
    "cleaning_actions": []
  },
  "column_roles": {
    "revenue": {"role": "revenue", "confidence": 0.85, "reason": "..."}
  },
  "analyses": [
    {
      "task_id": "time_series",
      "status": "success",
      "metrics": {
        "period_count": 3,
        "total": 150000.0,
        "trend_direction": "上升",
        "cagr": 0.15
      },
      "narration": "共3期数据，整体呈上升趋势..."
    }
  ],
  "insights": [
    {
      "id": "i00",
      "severity": "warning",
      "title": "头部集中度较高（CR5=68%）",
      "evidence": ["前5名合计占比 68.0%"],
      "interpretation": "少数核心产品贡献大部分收入..."
    }
  ],
  "executive_summary": "数据集 159 行 × 9 列。总计 150,000。数据质量 95/100。",
  "data_facts": "## 数据概览\n- 总行数: 159\n- 总列数: 9\n- 数据质量评分: 95/100"
}
```

---

## 安全配置

### 纵深防御体系

```
第1层: 文件扩展名校验       ← .csv / .xlsx / .xls 白名单
第2层: 文件魔数校验         ← 拒绝 ELF/PE/ZIP/PDF 等二进制
第3层: 路径规范化 + 沙箱    ← os.path.realpath() + 前缀白名单
第4层: 文件大小限制         ← MAX_FILE_SIZE_MB = 100
第5层: 速率限制             ← 滑动窗口 IP 级限流
第6层: Pandas 解析兜底      ← 异常捕获 + 脱敏错误返回
```

### 安全保证

- ✅ LLM 绝不接触原始行级数据
- ✅ 不使用 `eval()` / `exec()` / `os.system()`
- ✅ 不使用 `pickle` 反序列化
- ✅ 所有文件路径经过 `_safe_path()` 沙箱校验
- ✅ 所有上传文件经过魔数校验

---

## 常见问题

### Q: 支持多大的 CSV 文件？

A: 硬上限 100,000 行、100MB。推荐在 50,000 行以内获得最佳性能。

### Q: 支持哪些文件格式？

A: CSV（.csv，自动检测编码和分隔符）、Excel（.xlsx / .xls）。

### Q: 如何启用 LLM 解读？

A: 实例化 `CsvAnalyzer` 时传入 `llm` 参数：
```python
analyzer = CsvAnalyzer(config=config, llm=your_llm_client)
```
不传 `llm` 时，narration 由 f-string 模板生成（向后兼容，无 LLM 消耗）。

### Q: 如何减少输出 Token 消耗？

A: 使用 `slim_output=True` 配置：
```python
config = AnalysisConfig(analysis_depth="standard", slim_output=True)
```
此模式将去掉完整矩阵、时序明细和 top10 列表，减少 50-75% 输出体积。

### Q: 如何部署为公共服务？

A: 启用团队鉴权模式：
```bash
cp config/team.yaml.example config/team.yaml
# 编辑 team.yaml，配置 API Key
python api_server.py  # team_mode 自动启用
```
建议配合反向代理（nginx）和 HTTPS 使用。

### Q: API 速率限制如何配置？

A: 通过环境变量：
```bash
export RATE_LIMIT_RPS=5      # 每秒 5 个请求
export RATE_WINDOW=2.0        # 2 秒滑动窗口
```

### Q: Windows 上编码有问题？

A: 自动编码检测支持 UTF-8 / GBK / GB2312。如果仍有问题，手动指定：
```python
config = AnalysisConfig(encoding="gbk")
```

### Q: 运行测试

A: 
```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```
101 项测试应在 3-5 秒内全部通过。

### Q: 如何确认在新电脑上部署成功？

A: 依次执行以下检查：
```bash
# 检查 1: 导入是否正常
python -c "from scripts.csv_analyzer import CsvAnalyzer, analyze_csv; print('import OK')"

# 检查 2: 测试是否通过
pytest tests/ -q

# 检查 3: 直接分析测试数据
python run_analysis.py tests/fixtures/sales_2025_q4.csv quick

# 检查 4: API 服务能否启动（如使用 API 模式）
python api_server.py &
curl http://localhost:8080/health
```
四条命令全部成功即表示部署完成。

### Q: 在 Linux 服务器上部署需要注意什么？

A:
- 如果使用无 GUI 的服务器，matplotlib 可能报错——代码已设置 `Agg` 后端，自动兼容
- 建议使用 `pip install -e ".[api]"` 并配合 `gunicorn` 或 `systemd` 管理 API 进程
- 启用团队鉴权模式：`cp config/team.yaml.example config/team.yaml`
- 设置环境变量限制速率：`export RATE_LIMIT_RPS=5`

### Q: macOS 上 matplotlib 报错？

A: 代码已自动设置 `matplotlib.use("Agg")` 非交互后端，无需 GUI。如仍有问题：
```bash
echo 'backend: Agg' > ~/.matplotlib/matplotlibrc
```
