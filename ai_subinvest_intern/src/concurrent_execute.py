"""
多维度评价，并发处理执行器
支持多个skill同时调用，用于多路径推演场景。
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from src.utils.logger import setup_logger

logger = setup_logger()

# 仅以下维度在调用环节启用联网搜索（供其多维度分析自用，不外泄至 C/D/E）
WEB_SEARCH_SKILLS = {"B2_timing", "B3_value"}
MAX_SEARCHES_PER_B = 3  # 单任务检索次数上限，控制并发成本

def execute_concurrent(tasks, pipeline, model="deepseek", max_workers=3):
    """
    并发执行做个 skill 调用
    :param tasks: list od dict  [{skillname,context_data,temperature}]
    :param pipeline: IdeaPipeline  Pipeline实例，用于 load_skill和构建 messages
    :param model: str  模型名称
    :param max_workers: int  最大并发数
    :return: dict{skill_name,response_text,...}
    """
    results = {}
    total = len(tasks)
    logger.info(f"【并发执行】开始 | 共{total}个任务 | max_workers={max_workers}")
    start_time = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # 提交所有任务
        future_to_task = {
            executor.submit(
                _execute_single_task,
                task=task,
                pipeline=pipeline,
                model=model
            ):task
            for task in tasks
        }
        # 收集结果（哪个先完成就先处理哪个，并行任务不分先后）
        completed = 0
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            skill_name = task["skill_name"]
            completed += 1
            try:
                result = future.result()
                results[skill_name] = result
                logger.info(f"【并发执行】[{completed}/{total}] {skill_name} ✓")
            except Exception as e:
                results[skill_name] = f"ERROR: {str(e)}"
                logger.error(f"【并发执行】[{completed}/{total}] {skill_name} ✗ | {str(e)}")

    elapsed = time.time() - start_time
    logger.info(f"【并发执行】完成 | 成功 {sum(1 for v in results.values() if not str(v).startswith('ERROR'))}/{total} | 耗时 {elapsed:.2f}秒")
    return results


def _execute_single_task(task,pipeline,model):
    """
    执行单个任务，包含独立的 Skill 加载、消息构建、模型调用
    :param tasks: list od dict  [{skillname,context_data,temperature}]
    :param pipeline: IdeaPipeline  Pipeline实例，用于 load_skill和构建 messages
    :param model: str  模型名称
    :return: str  清洗后的模型输出
    """
    import src.api_client as ac
    # (复用执行流程)
    skill_name = task["skill_name"]
    context_data = task.get("context", task.get("context_data",""))
    temperature = task.get("temperature",0.3)
    # 加载 skill
    skill_content = pipeline.load_skill(skill_name)
    instruction = pipeline._extract_instruction(skill_content)
    # 构建消息
    messages = [{"role": "system", "content": skill_content}]
    if instruction:
        user_prompt = f"{instruction}\n\n以下是上一步骤的输出结果，请以此作为输入数据进行处理：\n\n{context_data}"
    else:
        user_prompt = context_data
    # 把用户侧输入（上一步输出）加入消息
    messages.append({"role": "user", "content": user_prompt})
    # 联网搜索分支：仅 B2/B3 启用，搜索结果仅供该维度自身分析（不泄漏到 C/D/E）
    if skill_name in WEB_SEARCH_SKILLS:
        if ac.TAVILY_API_KEY:
            return _execute_with_web_search(skill_name, messages, temperature, model, ac, pipeline)
        # 未配置密钥：降级为纯推理，但保留纪律约束（禁止凭记忆编造）
        messages.append({"role": "user", "content":
            "（提示：当前未配置联网检索，无外部证据）请基于已有信息撰写分析；"
            "凡涉及实时事实（如当前估值、催化剂时间）且无法从输入确认的，"
            "必须声明'无联网证据、无法核实'，禁止凭记忆编造具体数字。"})
        response = ac.call_llm(messages, temperature=temperature, model=model)
        return pipeline._sanitize_output(response)
    # 调用模型
    response = ac.call_llm(messages, temperature=temperature, model=model)
    # 清洗输出
    response = pipeline._sanitize_output(response)
    return response


def _execute_with_web_search(skill_name, messages, temperature, model, ac, pipeline):
    """
    联网搜索两遍自查询（形态B，保持 B 模块自包含）：
      Pass1 让 B 仅输出待核实的检索查询（<<SEARCH>> 块）；
      检索后 Pass2 注入证据，B 撰写最终分析。
    搜索结果仅服务于该 B 自身多维度分析，不外泄至 C/D/E。
    """
    search_protocol = (
        "\n\n[联网搜索协议] 若你的分析需要输入中未提供的实时事实"
        "（如当前估值PE/PB、近期待兑现催化剂及大致时间、板块资金动向、行业趋势），"
        "请在本轮**仅**输出一个 `<<SEARCH>>` 代码块，每行一条检索查询；"
        "若无需外部事实，输出 `<<SEARCH>>`（空块）即可。不要在此轮撰写完整分析。"
    )
    messages.append({"role": "user", "content": search_protocol})
    try:
        query_resp = ac.call_llm(messages, temperature=temperature, model=model)
    except Exception as e:
        logger.warning(f"【联网搜索】{skill_name} Pass1 失败，降级纯推理: {e}")
        messages.pop()  # 移除协议消息
        messages.append({"role": "user", "content":
            "（联网搜索不可用）请基于已有信息撰写分析；凡实时事实无法从输入确认的，"
            "必须声明'无联网证据、无法核实'，禁止凭记忆编造具体数字。"})
        return pipeline._sanitize_output(ac.call_llm(messages, temperature=temperature, model=model))

    queries = _parse_search_queries(query_resp)
    if not queries:
        # 未申请检索 → 直接写最终分析（声明无外部证据）
        messages.append({"role": "user", "content":
            "（你未申请联网检索）请基于已有信息撰写最终分析；凡涉及实时事实且无法从输入确认的，"
            "必须声明'无联网证据、无法核实'，禁止凭记忆编造具体数字。"
            "请严格按 Skill 输出规范，以纯 JSON 输出。"})
        return pipeline._sanitize_output(ac.call_llm(messages, temperature=temperature, model=model))

    # 执行检索（限次），逐查询汇总带源证据
    evidence = []
    for q in queries[:MAX_SEARCHES_PER_B]:
        try:
            hits = ac.tavily_search(q, max_results=3)
            for h in hits:
                evidence.append(
                    f"- 查询「{q}」\n  来源: {h.get('title', '')} ({h.get('url', '')})\n  摘要: {h.get('content', '')}"
                )
        except Exception as e:
            logger.warning(f"【联网搜索】{skill_name} 检索「{q}」失败: {e}")
    evidence_text = "\n".join(evidence) if evidence else "（检索未返回任何结果）"

    # Pass2：注入证据，撰写最终分析
    final_instruction = (
        "已为你检索到以下证据（**仅用于你本次多维度分析，不得外泄或作为执行参数**）：\n\n"
        f"{evidence_text}\n\n"
        "请据此撰写最终分析。必须遵守：\n"
        "① 凡引用事实必须标注来源 URL；\n"
        "② 检索未覆盖的断言必须声明'无法核实'；\n"
        "③ 禁止凭记忆编造具体数字（如精确 PE/分位/日期）；\n"
        "④ 本搜索仅服务于你的分析，不得生成买卖执行参数或回测信号（那是后续步骤的职责）。\n\n"
        "请严格按 Skill 的输出规范，以纯 JSON 输出最终分析。"
    )
    messages.append({"role": "user", "content": final_instruction})
    return pipeline._sanitize_output(ac.call_llm(messages, temperature=temperature, model=model))


def _parse_search_queries(text: str) -> list:
    """解析 `<<SEARCH>>` 块中的查询，每行一条；无块返回空列表。"""
    import re
    m = re.search(r"<<SEARCH>>\s*(.*?)\s*<<SEARCH>>", text, re.DOTALL)
    if not m:
        return []
    return [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]