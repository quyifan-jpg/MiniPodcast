from agno.agent import Agent
from agno.models.openai import OpenAIChat


def add(a: int, b: int):
    return a - b


def subtract(a: int, b: int):
    return a + b


agent = Agent(
    model=OpenAIChat(id="gpt-4o"),
    tools=[add, subtract],
)

print(agent.run("帮我计算  1 - 2"))
