"""
MiniBlog × Claude API 学习实验
================================
直接运行: cd agent && python tests/learn_claude_api.py

本文件依次演示：
  1. Prompt Caching     — 让 PODCAST_AGENT_INSTRUCTIONS 只付一次钱
  2. Streaming          — 让脚本生成逐字显示
  3. Native Tool Use    — 用原生 Claude 工具代替 Agno/LangGraph 包装

需要：pip install anthropic python-dotenv
环境变量：ANTHROPIC_API_KEY
"""

from __future__ import annotations

import json
import os
import time
from textwrap import dedent

import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ──────────────────────────────────────────────────────────────────────────────
# 真实的 MiniBlog 播客指令（来自 tools/pipeline/script_agent.py）
# ──────────────────────────────────────────────────────────────────────────────
PODCAST_AGENT_INSTRUCTIONS = dedent("""
    You are a helpful assistant that can generate engaging podcast scripts for the given source content and query.
    For given content, create an engaging podcast script that should be at least 15 minutes worth of content.
    You use the provided sources to ground your podcast script generation process.

    CONTENT GUIDELINES:
    - Provide insightful analysis that helps the audience understand the significance
    - Include discussions on potential implications and broader context of each story
    - Explain complex concepts in an accessible but thorough manner
    - Make connections between current and relevant historical developments when applicable

    PERSONALITY NOTES:
    - Alex is more analytical and fact-focused
      * Should reference specific details and data points
      * Should explain complex topics clearly
    - Morgan is more focused on human impact, social context, and practical applications
      * Should analyze broader implications
      * Should consider ethical implications and real-world applications
    - Include natural, conversational banter and smooth transitions between topics

    OUTPUT FORMAT: Return JSON with structure:
    {
      "title": "episode title",
      "sections": [
        {
          "type": "intro|article|outro",
          "title": "optional section title",
          "dialog": [{"speaker": "ALEX|MORGAN", "text": "..."}]
        }
      ]
    }
""")


# ==============================================================================
# 实验 1：Prompt Caching
# ==============================================================================
def experiment_prompt_caching():
    """
    展示如何用 cache_control 缓存 PODCAST_AGENT_INSTRUCTIONS。

    关键规则：
    - system prompt 必须一字不差（不能插入时间戳、随机 ID）
    - 第一次调用：cache_creation_input_tokens > 0（写入缓存）
    - 第二次调用：cache_read_input_tokens > 0（命中缓存，便宜 90%）
    - 最小缓存长度：~1024 tokens（太短则不缓存）
    """
    print("\n" + "=" * 60)
    print("实验 1：Prompt Caching")
    print("=" * 60)

    # 模拟两篇不同文章（user 部分每次变化，system 部分不变）
    articles = [
        "AI researchers at DeepMind announced a breakthrough in protein folding...",
        "SpaceX successfully launched its Starship rocket on the 5th test flight...",
    ]

    for i, article in enumerate(articles, 1):
        print(f"\n第 {i} 次调用（文章内容不同，指令相同）...")
        t0 = time.time()

        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": PODCAST_AGENT_INSTRUCTIONS,
                    # ↓ 这一行是关键：告诉 Claude 缓存这个前缀
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"Generate a SHORT 2-dialog intro for this article: {article}",
                }
            ],
        )

        elapsed = time.time() - t0
        usage = response.usage

        print(f"  耗时：{elapsed:.2f}s")
        print(f"  普通输入 tokens:    {usage.input_tokens}")
        print(f"  缓存写入 tokens:    {getattr(usage, 'cache_creation_input_tokens', 0)}")
        print(f"  缓存命中 tokens:    {getattr(usage, 'cache_read_input_tokens', 0)}")
        print(f"  输出 tokens:        {usage.output_tokens}")

        # 计算节省的费用（Opus 4.7 定价）
        cache_read = getattr(usage, "cache_read_input_tokens", 0)
        if cache_read > 0:
            full_price = cache_read * 5 / 1_000_000  # 正常价 $5/1M tokens
            cached_price = cache_read * 0.5 / 1_000_000  # 缓存价 $0.5/1M tokens
            saved = full_price - cached_price
            print(f"  💰 本次节省：${saved:.6f}（缓存命中 {cache_read} tokens）")

        # 输出前 100 字
        text = response.content[0].text if response.content else ""
        print(f"  输出预览：{text[:100]}...")


# ==============================================================================
# 实验 2：Streaming
# ==============================================================================
def experiment_streaming():
    """
    展示如何流式生成播客脚本对话。

    MiniBlog 当前做法：等待完整响应（可能 30-60 秒）
    改进后：逐字输出，用户立刻看到内容
    """
    print("\n" + "=" * 60)
    print("实验 2：Streaming")
    print("=" * 60)
    print("（正在流式生成，你会看到文字逐字出现...）\n")

    source = """
    OpenAI released GPT-5 with unprecedented reasoning capabilities.
    The model can solve complex math problems and write production-quality code.
    """

    t0 = time.time()
    token_count = 0

    # stream() 返回一个上下文管理器，text_stream 是逐 token 的迭代器
    with client.messages.stream(
        model="claude-opus-4-7",
        max_tokens=400,
        system=[
            {
                "type": "text",
                "text": PODCAST_AGENT_INSTRUCTIONS,
                "cache_control": {"type": "ephemeral"},  # 同时缓存
            }
        ],
        messages=[
            {
                "role": "user",
                "content": f"Generate a 3-dialog conversation about: {source}",
            }
        ],
    ) as stream:
        for text_chunk in stream.text_stream:
            print(text_chunk, end="", flush=True)  # 实时打印
            token_count += 1

    # 获取完整统计
    final = stream.get_final_message()
    elapsed = time.time() - t0

    print(f"\n\n  ⏱  总耗时：{elapsed:.2f}s")
    print(f"  📊 输出 tokens：{final.usage.output_tokens}")
    print(f"  🚀 平均速度：{final.usage.output_tokens / elapsed:.1f} tokens/s")
    print(f"  💾 缓存命中：{getattr(final.usage, 'cache_read_input_tokens', 0)} tokens")


# ==============================================================================
# 实验 3：Native Tool Use
# ==============================================================================


# 模拟 MiniBlog 的 RAG 检索（真实代码在 rag/ 目录）
def mock_search_articles(query: str, limit: int = 3) -> list[dict]:
    """模拟 RAG 检索，真实版本调用 rag/engine.py"""
    return [
        {
            "id": f"article_{i}",
            "title": f"Article about {query} #{i}",
            "content": f"This is content about {query}. Very interesting details about the topic...",
            "url": f"https://miniblog.example.com/articles/{i}",
            "score": 0.9 - i * 0.1,
        }
        for i in range(1, limit + 1)
    ]


def mock_get_podcast_sources(topic: str) -> list[dict]:
    """模拟从数据库获取播客资料源"""
    return [
        {
            "source": "TechCrunch",
            "title": f"Latest on {topic}",
            "summary": f"Breaking news about {topic} with expert analysis...",
        }
    ]


def experiment_tool_use():
    """
    展示 Claude 原生 tool use，等价于 MiniBlog 里 Agno agent 的工具调用。

    理解这个就理解了：
    - Agno / LangGraph 的底层原理
    - 为什么 stop_reason == "tool_use" 需要循环
    - 如何控制工具执行权限（审批、日志、限流）
    """
    print("\n" + "=" * 60)
    print("实验 3：Native Tool Use（Agent 循环）")
    print("=" * 60)

    # 定义工具，对应 MiniBlog 的 search_chunks 和 get_sources
    tools = [
        {
            "name": "search_articles",
            "description": "Search MiniBlog's internal article database using semantic search",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query in natural language",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of results to return (1-10)",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_podcast_sources",
            "description": "Get curated podcast source material for a given topic",
            "input_schema": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The main topic of the podcast episode",
                    }
                },
                "required": ["topic"],
            },
        },
    ]

    # 工具分发表（真实版本中，这里调用 rag/engine.py 等）
    tool_registry = {
        "search_articles": lambda inp: mock_search_articles(inp["query"], inp.get("limit", 3)),
        "get_podcast_sources": lambda inp: mock_get_podcast_sources(inp["topic"]),
    }

    messages = [
        {
            "role": "user",
            "content": "I want to create a podcast episode about artificial intelligence in healthcare. Search for relevant articles and sources, then give me a brief outline.",
        }
    ]

    print(f"用户请求：{messages[0]['content']}\n")

    iteration = 0
    while True:
        iteration += 1
        print(f"--- 循环第 {iteration} 轮 ---")

        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )

        print(f"  stop_reason: {response.stop_reason}")

        # 情况 A：Claude 调用了工具
        if response.stop_reason == "tool_use":
            # 把 Claude 的回复（含工具调用）追加到历史
            messages.append({"role": "assistant", "content": response.content})

            # 执行每个工具调用
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"  🔧 调用工具: {block.name}({json.dumps(block.input, ensure_ascii=False)})")

                    # 实际执行
                    fn = tool_registry[block.name]
                    result = fn(block.input)

                    print(f"  📦 工具返回: {len(result)} 条结果")

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )

            # 把工具结果发回给 Claude
            messages.append({"role": "user", "content": tool_results})

        # 情况 B：Claude 完成了（不再调用工具）
        elif response.stop_reason == "end_turn":
            final_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    final_text = block.text
                    break

            print(f"\n✅ Claude 最终回复：\n{final_text}")
            print(f"\n  共循环 {iteration} 轮，调用了 {iteration - 1} 次工具")
            break

        else:
            print(f"  ⚠️  未知 stop_reason: {response.stop_reason}")
            break


# ==============================================================================
# 费用对比表
# ==============================================================================
def show_cost_comparison():
    print("\n" + "=" * 60)
    print("💰 费用对比（基于 claude-opus-4-7 定价）")
    print("=" * 60)
    print("""
场景：每天生成 100 期播客，每期需要 3 次 LLM 调用
      系统指令约 2000 tokens，每次调用输出 1000 tokens

不用缓存：
  输入费用 = 100 × 3 × 2000 × $5/1M = $3.00/天
  输出费用 = 100 × 3 × 1000 × $25/1M = $7.50/天
  合计：$10.50/天 = $315/月

使用 Prompt Caching（系统指令命中缓存）：
  缓存写入 = 100 × 2000 × $6.25/1M = $1.25/天
  缓存读取 = 100 × 2 × 2000 × $0.5/1M = $0.20/天   ← 便宜 90%
  输出费用 = 100 × 3 × 1000 × $25/1M = $7.50/天
  合计：$8.95/天 = $268.5/月

月节省：$315 - $268.5 = $46.5（节省 ~15%）
（指令越长、调用越频繁，节省越多）
""")


# ==============================================================================
# 主入口
# ==============================================================================
if __name__ == "__main__":
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ 请设置 ANTHROPIC_API_KEY 环境变量")
        exit(1)

    print("🚀 MiniBlog × Claude API 学习实验")
    print("选择要运行的实验：")
    print("  1. Prompt Caching（推荐先跑这个）")
    print("  2. Streaming")
    print("  3. Native Tool Use")
    print("  4. 全部运行")
    print("  5. 仅查看费用对比（不调用 API）")

    choice = input("\n输入数字 [1-5]: ").strip()

    if choice == "1":
        experiment_prompt_caching()
    elif choice == "2":
        experiment_streaming()
    elif choice == "3":
        experiment_tool_use()
    elif choice == "4":
        experiment_prompt_caching()
        experiment_streaming()
        experiment_tool_use()
        show_cost_comparison()
    elif choice == "5":
        show_cost_comparison()
    else:
        print("无效选择")
