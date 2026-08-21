"""
启动脚本：
    不同于在任务二中，直接用pipeline.py把调用逻辑固定写死，
    现在通过main.py调用，作为一个干净的入口文件。
"""

from src.api_client import call_llm, web_experience_retrieval
from src.pipeline import IdeaPipeline
from src.concurrent_execute import execute_concurrent

import pandas as pd

def get_data_latest_date(data_path="data/hfq_clean.csv"):
    """读取行情数据最新交易日（TRADINGDAY 最大值）"""
    latest = None
    for chunk in pd.read_csv(data_path, chunksize=500_000, usecols=["TRADINGDAY"]):
        d = pd.to_datetime(chunk["TRADINGDAY"], format="%Y/%m/%d", errors="coerce")
        mx = d.max()
        if latest is None or (mx is not None and mx > latest):
            latest = mx
    return latest.strftime("%Y-%m-%d") if latest is not None else ""

DATA_LATEST_DATE = get_data_latest_date()


# 初始化
pipeline = IdeaPipeline()

# 用户输入（str）
user_idea = input("请输入你的投资想法：\n")

# ======== 流程执行 ==========

# Step 1: Parser the idea.
decomposed = pipeline.execute_step("A_deidea",context_data = user_idea)

# Step 2: Concurrent Varification.
tasks = [
    {"skill_name": "B1_logic", "context": decomposed},
    {"skill_name": "B2_timing", "context": decomposed},
    {"skill_name": "B3_value", "context": decomposed},
    {"skill_name": "B4_risk", "context": decomposed},
    {"skill_name": "B5_alternative", "context": decomposed},
]
verify_results = execute_concurrent(tasks, pipeline)

# Step 3: Evaluate the logical chain.
evaluated = pipeline.execute_step("C_evaluate",context_data = verify_results)

# Step 3.5: Web experience retrieval (Architecture B: independent retrieval step)
web_exp = web_experience_retrieval(decomposed)

# Step 4: Complete the quant strategy.
# D needs: C's output + B1-B5 analysis + web_experience
verify_results_text = "\n\n".join([f"### {k}\n{v}" for k, v in verify_results.items()])
d_context = f"""## Skill C 输出（综合研判）
{evaluated}

## Skill B1-B5 输出（五维度分析）
{verify_results_text}

## 行情数据最新交易日（回测窗口锚点，禁止用今天或凭记忆）
DATA_LATEST_DATE: {DATA_LATEST_DATE}

## 联网检索到的行业经验参考
{web_exp}"""
quantified = pipeline.execute_step("D_detassume", context_data=d_context)

# Step 5: Backtest
backtest_result = pipeline.execute_step("E_backtest",context_data = quantified)

# -------- 代码运行 ----------
extracted_code = pipeline.extract_python_code(backtest_result)
exec_result = pipeline.execute_generated_code(extracted_code, timeout=600)
# 提取 stdout 作为回测结果
backtest_output = exec_result["stdout"] if exec_result["status"] == "success" else exec_result["stderr"]

# Step 6: Generate the report. (ONLY Assembly)
# Gather all the output before and then generate final report.
report_input = f"""
以下是前面所有步骤的完整产出，请按报告模板组装最终分析报告：

## Skill A 输出（想法拆解）
{decomposed}

## Skill B1-B5 输出（五维度分析）
{verify_results_text}

## Skill C 输出（综合研判）
{evaluated}

## Skill D 输出（策略量化补全 + 网络经验参数）
{quantified}

## Skill E 输出
### 回测代码
{backtest_result}

### 回测结果
{backtest_output}
"""
report = pipeline.execute_step("F_finreport",context_data = report_input)

# ========= 输出最终的分析报告 =========
print("\n"+"="*60)
print("最终分析报告")
print("=" * 60 + "\n")
print(report)

# 将报告保存为文件
with open(f"{pipeline.output_dir}/report.md","w", encoding="utf-8") as f:
    f.write(report)
print(f"\n 报告已保存至: {pipeline.output_dir}/report.md")