# CSV Data Analysis Skill

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-101%20passed-brightgreen.svg)](TEST_REPORT.md)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](https://github.com)

基于 **Pandas 精确计算 + LLM 语义解读** 的企业经营数据分析引擎，**专注销售/财务/运营场景**。12 种多维分析，101 项测试，6 层安全纵深防御。API 服务 + Agent Tool + CLI 三种部署模式。

### ⚠️ 适用领域

本 Skill 的列语义识别基于**企业经营关键词字典**（收入/成本/利润/产品/区域/客户/渠道），**最适合**以下场景：
- 📊 销售数据（销售额/销量/单价/区域/产品）
- 💰 财务数据（收入/成本/利润/毛利）
- 🏭 运营数据（客户/渠道/库存/供应链）
- 👤 客户分析（RFM 分层/复购/留存）

**不适用**于档案元数据、日志、JSON嵌套、非结构化文本等通用CSV。如列名不在字典中，将被标记为 `ignore` 并跳过深度分析。可通过 `column_hints` 参数手动标注列语义来扩展支持。

---

## 核心特性

| 特性 | 说明 |
|------|------|
| 🔢 **12 种分析** | 描述统计、时序趋势、同比对比、排名、ABC帕累托、异常检测、相关性、分布、区域、维度下钻、RFM客户、趋势预测 |
| 🛡️ **数据隔离** | LLM 绝不接触原始行级数据，计算 100% 由 Python/Pandas 完成 |
| 🔗 **Agent 原生** | LangGraph StateGraph 编排 + Send API 并行扇出 |
| 🏢 **多租户** | API Key 鉴权 + 角色权限 + 速率限制 + 用量追踪 + 预算告警 |
| ⚡ **Token 高效** | batch_narrate（-81% LLM调用）、correlation O(N²) 保护、时序降采样、输出断路器 |
| 🔒 **安全加固** | 路径沙箱、魔数校验、IP 限流、错误脱敏、try/finally 资源清理、依赖锁定 |

---

## 快速开始

```bash
# 安装
pip install -r requirements.txt

# 一行代码分析
python -c "
from scripts.csv_analyzer import analyze_csv
result = analyze_csv('tests/fixtures/sales_2025_q4.csv', depth='standard')
print(result['executive_summary'])
"

# 或启动 API 服务
pip install -r requirements-api.txt
python api_server.py
```

---

## 三种使用模式

### A. Python 直接调用

```python
from scripts.csv_analyzer import CsvAnalyzer, AnalysisConfig, CsvDataSource

config = AnalysisConfig(analysis_depth="standard")
data = CsvDataSource(file_path="data.csv")
output = CsvAnalyzer(config=config).run(data)
print(output.executive_summary)
```

### B. API 服务

```bash
curl -X POST http://localhost:8080/analyze -F "file=@data.csv" -F "depth=standard"
```

### C. LangChain Tool

```python
from agent_tool import create_csv_analysis_tool
tool = create_csv_analysis_tool()
```

> 完整使用说明见 [USER_GUIDE.md](USER_GUIDE.md)

---

## 分析能力总览

| 分析 | 函数 | 深度 | 说明 |
|------|------|------|------|
| 描述统计 | `desc_stats` | quick+ | 行数/列数/缺失率/数值摘要 |
| 时序趋势 | `time_series` | quick+ | 月度聚合/MoM/CAGR/拐点 |
| 同比对比 | `yoy_comparison` | deep | 同期对比增长率 |
| Top 排名 | `top_ranking` | quick+ | Top10/Bottom5/CRn/HHI |
| ABC 帕累托 | `pareto_abc` | standard+ | A(0-70%)/B(70-90%)/C(90-100%) |
| 异常检测 | `anomaly_detect` | standard+ | IQR 异常值/按列统计 |
| 相关性矩阵 | `correlation` | standard+ | Pearson/Spearman/强相关对/p-value |
| 分布分析 | `distribution` | standard+ | 偏度/峰度/Shapiro-Wilk |
| 区域分析 | `region_analysis` | standard+ | 按区域聚合/占比/排名 |
| 维度下钻 | `drill_down` | standard+ | 多维度交叉分析 |
| RFM 客户 | `rfm_analysis` | deep | 5分制 R/F/M 打分/客户分层 |
| 趋势预测 | `simple_forecast` | deep | 线性回归/80%/95%预测区间 |

---

## 安全声明

- **LLM 零接触行级数据** — 所有计算 100% Python/Pandas 完成
- **6 层纵深防御** — 扩展名→魔数→路径沙箱→文件大小→速率限制→异常兜底
- **无危险操作** — 无 `eval`/`exec`/`os.system`/`pickle`/原始 SQL
- **路径安全** — `os.path.realpath()` + 前缀白名单防遍历
- **速率限制** — IP 级滑动窗口 + 429 + Retry-After
- **错误脱敏** — `request_id` 追踪，生产环境不暴露内部细节

> 完整安全分析见 [安全审计漏洞修补技术方案](../安全审计漏洞修补技术方案.md)

---

## Token 效率

| 优化 | 方式 | 效果 |
|------|------|------|
| batch_narrate | N→1 次 LLM 调用 | 节省 ~81% LLM token |
| correlation 上限 | MAX_CORR_COLS=15 | 50列场景 ~9,400→~200 token |
| 时序降采样 | >24 期仅保留年度+最近12月 | 长序列体积减 ~70% |
| slim_output | 配置开关 | 按需减少 50-75% |
| 输出断路器 | MAX_OUTPUT_TOKENS=8000 | 自动触发裁剪 |

---

## 项目结构

```
csv-data-analysis-skill/
├── README.md                          # 本文件
├── LICENSE                            # MIT 许可证
├── CHANGELOG.md                       # 版本历史
├── CONTRIBUTING.md                    # 贡献指南
├── USER_GUIDE.md                      # 使用说明
├── TEST_REPORT.md                     # 测试报告
├── skill.md                           # Skill 定义
├── __init__.py                        # 包初始化
├── .gitignore
│
├── scripts/                           # 核心脚本
│   ├── csv_analyzer.py                # 主分析器 (~3,020行)
│   ├── graph_builder.py               # LangGraph 编排
│   ├── graph_state.py                 # 状态定义
│   ├── auth.py                        # 多租户鉴权
│   └── tracker.py                     # 用量追踪
│
├── tests/                             # 测试
│   ├── test_analyzer.py               # 分析器测试 (43项)
│   ├── test_auth.py                   # 鉴权测试 (16项)
│   ├── test_graph.py                  # 图编排测试 (17项)
│   ├── test_team.py                   # 集成测试 (8项)
│   ├── test_tracker.py                # 追踪测试 (14项)
│   ├── conftest.py                    # 测试夹具
│   └── fixtures/
│       └── sales_2025_q4.csv          # 测试数据
│
├── references/                        # 参考文档
│   └── 统计指标参考手册.md            # 公式与判读标准
│
├── config/
│   └── team.yaml.example              # 团队鉴权配置示例
│
├── api_server.py                      # FastAPI HTTP 服务
├── agent_tool.py                      # LangChain StructuredTool
├── run_analysis.py                    # CLI 运行器
│
├── requirements.txt                   # 核心依赖
├── requirements-api.txt               # API 服务依赖
├── requirements-dev.txt               # 开发/测试依赖
└── requirements.lock.txt              # 锁定版本
```

---

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

**101 项测试全部通过**（最后验证: 2026-06-21）。详见 [TEST_REPORT.md](TEST_REPORT.md)

---

## 依赖

| 依赖 | 版本 | 用途 |
|------|------|------|
| pandas | >= 2.0 | 数据处理核心 |
| numpy | >= 1.24 | 数值计算 |
| scipy | >= 1.11 | 统计检验 |
| pydantic | >= 2.0 | 数据模型 |
| langgraph | >= 0.2 | 工作流编排 |
| matplotlib | >= 3.7 | 图表生成（可选） |
| seaborn | >= 0.13 | 统计图表（可选） |
| chardet | >= 5.0 | 编码检测 |
| openpyxl | >= 3.1 | Excel 读取 |
| pyyaml | >= 6.0 | 配置解析 |

---

## 贡献

欢迎贡献！请参阅 [CONTRIBUTING.md](CONTRIBUTING.md)

---

## License

MIT © 2026 CSV Data Analysis Skill Team
