# main.py
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from pydantic import BaseModel
from typing import List
from textwrap import dedent
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()


# 导入 jikan_search 工具
from tools.jikan_search import jikan_search  # noqa: E402


# ------ 定义 SearchAgent 输出结构（简化） ------
class ReturnItem(BaseModel):
    url: str
    title: str
    description: str
    source_name: str
    tool_used: str = "unknown"
    published_date: str = ""
    is_scrapping_required: bool = False


class SearchResults(BaseModel):
    items: List[ReturnItem]


# ------ Search Agent Prompt ------
SEARCH_AGENT_INSTRUCTIONS = dedent("""
You are a search assistant. Decide which search tool to use based on the query.

Tools:
- jikan_search → Use for anime, manga, Japanese entertainment.
If query contains anime, manga name, or Japanese titles, use jikan_search.

Return 1–3 high-quality results.
""")


# ------ MAIN EXECUTION ------
def run_test_query():
    print("\n=== Starting Search Agent Test ===\n")

    search_agent = Agent(
        model=OpenAIChat(id="gpt-4o-mini"),
        instructions=SEARCH_AGENT_INSTRUCTIONS,
        description="Anime-aware search agent",
        use_json_mode=True,
        response_model=SearchResults,
        tools=[jikan_search],  # <-- 只加载一个工具，便于测试
    )

    # 测试输入
    query = "One Piece summary"
    print("User Input:", query)

    # 运行 agent（这里可以用 debugger 单步）
    response = search_agent.run(query)

    print("\n=== Raw Response Dict ===")
    print(response.to_dict())

    print("\n=== Final Search Results ===")
    response_dict = response.to_dict()
    for item in response_dict["content"]["items"]:
        print(f"- {item.title} 👉 {item.url}")


if __name__ == "__main__":
    run_test_query()
