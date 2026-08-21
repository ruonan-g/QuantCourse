import json
import os
import time
from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIError, APIConnectionError

load_dotenv() # 自动读取项目根目录的 .env 文件

# ==================== 多模型客户端配置 ====================
# 每个模型一个 OpenAI client 实例，通过 model 参数路由

# --- DeepSeek（文本分析/代码审查） ---
deepseek_client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
)
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL_NAME", "deepseek-v4-pro")

# --- Moonshot / Kimi（代码生成/修复） ---
kimi_client = OpenAI(
    api_key=os.getenv("MOONSHOT_API_KEY"),
    base_url=os.getenv("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1")
)
KIMI_MODEL = os.getenv("MOONSHOT_MODEL_NAME", "kimi-k3")

# 模型名 → (client, model_id) 映射
MODEL_REGISTRY = {
    "deepseek": (deepseek_client, DEEPSEEK_MODEL),
    "kimi":     (kimi_client, KIMI_MODEL),
}

# 调用大模型
"""
加 Retry 逻辑：
    因为调用API时可能遇到：网络抖动、限流、服务端临时错误等问题,
    在 call_llm 外层加自动重试，遇到可恢复的错误时等待片刻再试。
"""
MAX_RETRIES = 3
RETRY_DELAY = 2  # 秒

def call_llm(messages, temperature: float=0.1, model: str="deepseek") -> str:
    """
    统一的LLM调用函数，含自动重试，支持多模型路由。

    参数：
        messages: 消息列表
        temperature: 生成随机性，越低越确定
        model: 模型选择，"deepseek"（默认）或 "kimi"

    返回：
         LLM的回复文本
    """
    if model not in MODEL_REGISTRY:
        raise ValueError(f"未知模型: {model}，可选: {list(MODEL_REGISTRY.keys())}")

    client_instance, model_id = MODEL_REGISTRY[model]

    # 模型温度限制：Kimi K3 只允许 temperature=1
    if model == "kimi":
        temperature = 1

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client_instance.chat.completions.create(
                model=model_id,
                temperature=temperature,
                messages=messages,
                timeout=120  # 防 API 静默挂起：单call最多等120s，否则抛错交重试
            )
            return response.choices[0].message.content

        except RateLimitError as e:
            last_error = e
            wait = RETRY_DELAY * (2 ** (attempt - 1))  # 指数退避
            print(
                f"[Retry] [{model}] 限流错误，第 {attempt}/{MAX_RETRIES} 次重试，等待 {wait} 秒..."
            )
            time.sleep(wait)

        except (APIConnectionError, APIError) as e:
            last_error = e
            wait = RETRY_DELAY
            print(
                f"[Retry] [{model}] API 错误，第 {attempt}/{MAX_RETRIES} 次重试，等待 {wait} 秒..."
            )
            time.sleep(wait)

    raise Exception(f"LLM 调用失败 [{model}]，已重试 {MAX_RETRIES} 次。最后错误: {last_error}")


# ==================== Tavily 联网检索 ====================

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# 回测标准参数 → 检索查询模板（{industry}/{strategy_type} 从 A 输出中提取）
_PARAM_QUERY_TEMPLATES = {
    "position.max_weight":      "回测 {strategy_type} 单票仓位上限 风控 惯例 券商",
    "risk_control.stop_loss":   "回测 止损比例 惯例 {industry} 量化策略",
    "risk_control.take_profit": "回测 止盈比例 惯例 {industry} 量化策略",
    "holding_period.duration":  "回测 持有周期 调仓频率 惯例 {strategy_type}",
    "buy_trigger.condition":    "回测 买入条件 技术指标 {strategy_type} {industry}",
    "sell_trigger.condition":   "回测 卖出条件 技术指标 {strategy_type} {industry}",
}


def _extract_context_from_idea(idea_context: str) -> dict:
    """
    从 Skill A 的输出文本中提取行业与策略类型，用于定制检索查询。
    解析失败时返回空 dict，调用方用通用查询兜底。
    """
    import re
    ctx = {"industry": "", "strategy_type": ""}
    try:
        # 尝试从 JSON 中提取 target.industry 和 idea.type
        industry_match = re.search(r'"industry"\s*:\s*"([^"]+)"', idea_context)
        type_match = re.search(r'"type"\s*:\s*"([^"]+)"', idea_context)
        if industry_match:
            ctx["industry"] = industry_match.group(1)
        if type_match:
            ctx["strategy_type"] = type_match.group(1)
    except Exception:
        pass
    return ctx


def tavily_search(query: str, max_results: int = 3) -> list:
    """
    调用 Tavily Search API 进行联网检索。

    Args:
        query: 检索查询字符串
        max_results: 最多返回结果数

    Returns:
        list[dict]: 每条含 title / url / content；失败返回空列表
    """
    if not TAVILY_API_KEY:
        print("[Tavily] 未配置 TAVILY_API_KEY，跳过检索")
        return []

    try:
        import httpx
    except ImportError:
        print("[Tavily] httpx 未安装，跳过检索")
        return []

    url = "https://api.tavily.com/search"
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
    }

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            results = []
            for r in data.get("results", []):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", "")[:500],
                })
            return results
    except Exception as e:
        print(f"[Tavily] 检索失败: {e}")
        return []


def web_experience_retrieval(idea_context: str, max_results_per_param: int = 3) -> str:
    """
    联网检索行业经验，为回测参数提供参考默认值。

    对回测必需的 6 个标准参数逐一发起针对性检索，返回格式化文本供 Skill D 使用。
    若未配置 TAVILY_API_KEY 或检索失败，返回降级提示，D 将使用静态默认值。

    Args:
        idea_context: Skill A 的输出文本（JSON），用于提取行业/策略类型
        max_results_per_param: 每个参数最多检索几条结果

    Returns:
        str: 格式化的检索结果文本
    """
    if not TAVILY_API_KEY:
        return "（未配置 TAVILY_API_KEY，本次未执行联网检索。D 将使用静态默认值。）"

    ctx = _extract_context_from_idea(idea_context)
    industry = ctx["industry"] or "A股"
    strategy_type = ctx["strategy_type"] or "主观多头"

    sections = []
    sections.append("以下是对回测标准参数的联网检索结果（行业经验参考）。"
                     "D 应优先使用这些经验值补全用户未明确的参数，并标注 source: web_experience。")

    for param, template in _PARAM_QUERY_TEMPLATES.items():
        query = template.format(industry=industry, strategy_type=strategy_type)
        results = tavily_search(query, max_results=max_results_per_param)

        param_lines = [f"\n### 参数: {param}", f"- 检索查询: {query}"]
        if not results:
            param_lines.append("- 未检索到相关结果，D 将对该参数使用静态默认值（source: assumption）")
        else:
            for i, r in enumerate(results, 1):
                param_lines.append(f"- 结果{i}: {r['title']}")
                param_lines.append(f"  链接: {r['url']}")
                param_lines.append(f"  摘要: {r['content']}")
        sections.append("\n".join(param_lines))

    return "\n".join(sections)
