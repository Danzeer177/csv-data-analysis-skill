# CSV 数据分析 Skill

> 指标公式与判读标准: [`references/统计指标参考手册.md`](references/统计指标参考手册.md)

---

## 角色

接收 CSV（兼容 Excel）表格数据，自动完成加载→画像→清洗→多维分析→洞察→输出。无状态、无副作用。**LLM 只做解读，计算 100% 由 Pandas 完成**。

| ✅ 负责 | ❌ 不负责 |
|---------|-----------|
| CSV/Excel 读取，编码/分隔符自动检测 | UI、文件上传 |
| 列语义识别、数据清洗、质量评分(0-100) | 非结构化数据、PDF |
| 12 项多维分析(Pandas计算+LLM解读) | 数据库直连、多轮对话 |
| 洞察生成(3-8条) + 结构化输出 | 报告排版、PDF导出 |

---

## 触发条件

**立即调用**: 用户请求"分析CSV/数据"、上传.csv/.xlsx、对已有数据追问趋势/异常  
**确认后调用**: 数据含敏感个人信息、行数>100K  
**不调用**: 非表格数据(JSON嵌套/日志)、简单筛选排序、数据库直查

---

## 处理流水线 (8步)

### S1 数据加载
- 判断来源: `content`(StringIO→csv) 或 `file_path`(检测编码charset+分隔符→csv) 或 `.xlsx`(Excel兼容)
- 校验: 存在性/格式/≤100MB/≤100K行/≥2列
- 输出: `raw_df`, `source_info`, `parse_meta`

### S2 结构感知
- Pandas画像: 每列dtype/null率/唯一值/分布参数(数值:min/max/mean/std/Q1/Q3/skew; 日期:time_span; 文本:top5)
- LLM语义: 关键词匹配推断角色(date/revenue/quantity/price/cost/profit/product/region/customer/channel/metric/ignore)
- 置信度<0.6 → `ambiguity_report`，交Host Agent确认
- 输出: `data_profile`, `column_roles`

### S3 数据清洗
- 按角色差异化: date不填充标记; revenue/quantity/metric→中位数+IQR; category/region→众数; ignore→跳过
- 质量评分: `100 - 高缺失列×10 - 高异常列×5 - 重复率×50`
- **只标记不删除**
- 输出: `cleaned_df`, `quality_report`

### S4 分析编排
- 盘点可用维度→匹配分析函数; quick≤3/standard≤7/deep≤10
- 分析函数: desc_stats/time_series/yoy/top_ranking/pareto_abc/anomaly/correlation/distribution/region/drill_down/rfm/forecast
- 输出: `analysis_plan`

### S5 多维分析 (并行)
- 每个分析: Pandas计算metrics→LLM解读narration; 失败不阻塞
- 指标定义见[参考手册](references/统计指标参考手册.md)

### S6 综合洞察
- 跨维度聚合: 规模/趋势/集中度/质量/长尾/关联/客户/预测
- 分级: critical(质量<50/连续下降/流失>高价值) | warning(CR5>0.7/趋势降>10%) | info
- 数量: quick≤3/standard≤6/deep≤8

### S7 聚焦回答
- 针对focus_questions检索相关metrics→数据驱动回答+证据

### S8 输出组装
- executive_summary≤200字, data_facts(Markdown表格)

---

## 异常处理

| 场景 | 处理 |
|------|------|
| 子分析失败 | status="error"继续其他 |
| LLM超时 | 重试2次(1s→4s) |
| LLM格式异常 | 重试→降级(仅metrics) |
| 编码检测失败 | UTF-8→GBK→GB2312→Latin-1 |

---

## 输出结构

```
DataAnalysisOutput:
  status, execution_id, elapsed_seconds
  source_info: {type, file_name, file_size_mb}
  parse_meta: {delimiter, encoding, row_count, col_count}
  data_profile: [{name, dtype, null_rate, ...numeric/date/text_stats}]
  quality: {score(0-100), null_summary, outlier_summary, duplicate_rows, cleaning_actions[]}
  column_roles: {列名: {role, confidence, reason}}
  analyses: [{task_id, status, metrics, narration(200-500字), chart_path}]
  insights: [{id, title, severity(critical|warning|info), evidence[], interpretation}]
  answers: [{question, answer, evidence, confidence}]
  charts: [{chart_id, chart_type, path, title}]
  executive_summary: str(≤200字)
  data_facts: str(Markdown)
```

### 错误码

`INVALID_INPUT` | `FILE_NOT_FOUND` | `UNSUPPORTED_FORMAT` | `EMPTY_CONTENT` | `CSV_PARSE_ERROR` | `DATA_TOO_LARGE` | `ROW_LIMIT_EXCEEDED` | `TOO_FEW_COLUMNS` | `NO_ANALYZABLE_COLUMNS` | `LLM_CALL_FAILED`

---

## 环境

```
python>=3.10, pandas>=2.0, numpy>=1.24, openpyxl>=3.1, chardet>=5.0,
matplotlib>=3.7, seaborn>=0.13, scipy>=1.11, pydantic>=2.0
```

## 约束

1. 无状态 — 每次调用独立
2. 耗时: 5K行~30s, 50K行~3min
3. temperature=0 确保可复现
4. content模式比file_path快~200ms
5. 图表存本地文件，输出仅含路径
6. 指标以[参考手册](references/统计指标参考手册.md)为准
