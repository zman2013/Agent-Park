"""Prompt templates for wiki search (phase 1 selection + phase 2 verification)."""
from __future__ import annotations


def phase1_selection(
    user_prompt: str,
    index_content: str,
    page_details_str: str,
    max_pages: int,
) -> str:
    """Phase 1: select candidate pages from the wiki index.

    Conservative selection — instructs LLM to prefer precision over recall.
    Only pages whose summary explicitly mentions the user's specific objects
    (function names, pass names, structs) should be selected.
    """
    prompt = (
        f"你是 wiki 知识检索引擎。请严格按以下两步执行：\n\n"
        f"【第一步】判断：wiki 中是否存在专门解释用户任务所涉及的具体对象（函数、pass、结构体、行为）的页面？\n"
        f"判断方法：逐一阅读页面摘要，检查摘要中是否明确提及了用户问题中的具体名称或行为。\n"
        f"如果摘要中没有明确提及，即使页面涉及相同的硬件模块（如 GSDMA）或领域，也不算相关。\n"
        f"原则：宁可漏选，不要错选。只选你有高置信度确定相关的页面，不确定的不选。\n\n"
        f"【第二步】如果第一步答案是『有』，输出这些页面的文件名 JSON 数组（最多 {max_pages} 个）；"
        f"如果第一步答案是『没有』，输出空数组 []。\n\n"
        f"用户任务：{user_prompt}\n\n"
        f"Wiki 索引：\n{index_content}\n\n"
    )
    if page_details_str:
        prompt += f"已有页面详情：\n{page_details_str}\n\n"
    prompt += "只输出最终的 JSON 数组（如 [\"pages/xxx.md\"] 或 []），不要输出推理过程。"
    return prompt


def phase2_verification(user_prompt: str, page_desc: str) -> str:
    """Phase 2: verify a single candidate page against the user's question.

    Uses page title/summary/overview (not full text) to avoid keyword-bias
    from rich page content leading to false positives.
    判断标准是"有无专项文档"而非"是否有帮助"，从而过滤掉仅领域相关但主题不符的页面。
    """
    return (
        f"用户问题：{user_prompt}\n\n"
        f"以下是一篇 wiki 页面的描述：\n\n{page_desc}\n"
        f"请判断：这篇页面是否有对用户问题中提到的具体对象（函数名、pass 名、结构体）的专项文档？\n"
        f"- YES：页面专门记录了用户问题中某个具体对象的定义、参数、行为或设计\n"
        f"- NO：页面只是涉及相同模块/领域，但没有针对用户问题中具体对象的专项说明\n"
        f"只输出 YES 或 NO，不要其他内容。"
    )
