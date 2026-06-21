# Contributing to CSV Data Analysis Skill

感谢你的贡献！本项目是一个基于 Pandas + LangGraph 的 CSV/Excel 数据分析引擎。

---

## 行为准则

- 尊重所有贡献者
- 建设性的代码审查
- 保持讨论聚焦于技术问题

---

## 开发环境设置

```bash
# 1. 克隆仓库
git clone <repo-url>
cd csv-data-analysis-skill

# 2. 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# 3. 安装开发依赖
pip install -r requirements-dev.txt
```

---

## 运行测试

```bash
# 运行全部测试
pytest tests/ -v

# 运行指定模块
pytest tests/test_analyzer.py -v
pytest tests/test_auth.py -v

# 带覆盖率报告
pytest tests/ -v --cov=scripts --cov-report=html
```

**所有测试必须在提交前通过**（当前: 101 passed）。

---

## 代码风格

- 遵循 [PEP 8](https://pep8.org/)
- 类型注解: 所有公开函数必须有完整的类型注解
- 文档字符串: 使用 Google 风格的 docstring
- 行长度: 建议 ≤100 字符
- 命名: 类名 PascalCase，函数/变量 snake_case，常量 UPPER_SNAKE_CASE

```python
def _analyze_time_series(
    self, df: pd.DataFrame, date_col: str, metric_col: str
) -> SubAnalysisResult:
    """
    时序趋势分析。

    Args:
        df:         清洗后的 DataFrame
        date_col:   日期列名
        metric_col: 分析指标列名

    Returns:
        含有时序指标和解读的分析结果
    """
    ...
```

---

## 架构原则

1. **计算与解读分离**: metrics dict (Python 计算) + narration (LLM 解读)
2. **容错优先**: 单个分析失败不影响其他分析
3. **数据隔离**: LLM 绝不接触原始行级数据
4. **安全优先**: 所有外部输入必须经过校验

---

## 提交规范

- 使用描述性的提交信息
- 推荐格式: `<type>: <short description>`
  - `feat:` — 新功能
  - `fix:` — Bug 修复
  - `security:` — 安全相关
  - `perf:` — 性能优化
  - `test:` — 测试相关
  - `docs:` — 文档更新

---

## 添加新的分析函数

1. 在 `scripts/csv_analyzer.py` 中添加 `_analyze_xxx()` 方法
2. 在 `references/统计指标参考手册.md` 中添加对应章节
3. 在 `_run_analyses()` 中注册任务
4. 在 `scripts/graph_builder.py` 中注册分发器
5. 在 `tests/test_analyzer.py` 中添加测试
6. 运行全部测试确认通过

---

## 安全注意事项

- **绝不**将原始行级数据传给 LLM — 使用 `_slim_metrics()` 裁剪
- **所有**文件路径必须通过 `_safe_path()` 校验
- **所有**CSV 文件上传必须通过 `_verify_csv_magic()` 校验
- 新增的外部输入点必须考虑路径遍历、注入攻击
- 如有安全发现，请私下报告而非公开 Issue

---

## 问题反馈

- **Bug 报告**: 使用 GitHub Issues，附上复现步骤和测试数据
- **功能建议**: 在 Issue 中描述使用场景和期望行为
- **安全漏洞**: 请私下联系维护者，获得确认后再公开

---

再次感谢你的贡献！
