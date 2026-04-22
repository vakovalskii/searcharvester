"""
Тест для проверки совместимости с оригинальным Tavily API
"""
from tavily_client import TavilyClient

# Тест совместимости с оригинальным API
client = TavilyClient(api_key="fake-key")  # API ключ не используется
response = client.search(
    query="цена bmw x6",
    include_raw_content=True
)

print("Response:")
print(response)
print("\nResults count:", len(response["results"]))
if response["results"]:
    print("First result URL:", response["results"][0]["url"])
    print("First result title:", response["results"][0]["title"])
