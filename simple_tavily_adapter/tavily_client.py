"""
Tavily-compatible client for SearXNG
Provides same interface as tavily-python package but uses SearXNG backend
"""
import asyncio
import time
import uuid
from typing import Any

import aiohttp
from bs4 import BeautifulSoup
from pydantic import BaseModel

from config_loader import config


class TavilyResult(BaseModel):
    url: str
    title: str
    content: str
    score: float
    raw_content: str | None = None


class TavilyResponse(BaseModel):
    query: str
    follow_up_questions: list[str] | None = None
    answer: str | None = None
    images: list[str] = []
    results: list[TavilyResult]
    response_time: float
    request_id: str


class TavilyClient:
    def __init__(self, api_key: str = "", searxng_url: str | None = None):
        self.api_key = api_key  # Не используется, но сохраняем для совместимости
        self.searxng_url = (searxng_url or config.searxng_url).rstrip('/')
    
    async def _fetch_raw_content(self, session: aiohttp.ClientSession, url: str) -> str | None:
        """Скрапит страницу и возвращает первые 2500 символов текста"""
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=config.scraper_timeout),
                headers={'User-Agent': config.scraper_user_agent}
            ) as response:
                if response.status != 200:
                    return None
                
                html = await response.text()
                soup = BeautifulSoup(html, 'html.parser')
                
                # Удаляем ненужное
                for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                    tag.decompose()
                
                # Берем текст
                text = soup.get_text(separator=' ', strip=True)
                
                # Обрезаем до настроенного размера
                if len(text) > config.scraper_max_length:
                    text = text[:config.scraper_max_length] + "..."
                
                return text
        except Exception:
            return None
        
    def search(
        self,
        query: str,
        max_results: int = 10,
        include_raw_content: bool = False,
    ) -> dict[str, Any]:
        """
        Search using SearXNG with Tavily-compatible interface
        """
        return asyncio.run(self._async_search(
            query=query,
            max_results=max_results,
            include_raw_content=include_raw_content,
        ))
    
    async def _async_search(
        self,
        query: str,
        max_results: int = 10,
        include_raw_content: bool = False,
    ) -> dict[str, Any]:
        start_time = time.time()
        request_id = str(uuid.uuid4())
        
        # Формируем запрос к SearXNG
        searxng_params = {
            "q": query,
            "format": "json",
            "categories": "general",
            "engines": "google,duckduckgo,brave",  # Убрали Bing
            "pageno": 1,
            "language": "auto",
            "safesearch": 1,
        }
        
# Убрали обработку доменов - не нужно для упрощенного API
        
        # Добавляем заголовки для обхода блокировки SearXNG
        headers = {
            'X-Forwarded-For': '127.0.0.1',
            'X-Real-IP': '127.0.0.1',
            'User-Agent': 'Mozilla/5.0 (compatible; TavilyBot/1.0)',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    f"{self.searxng_url}/search",
                    data=searxng_params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    searxng_data = await response.json()
            except Exception as e:
                # Возвращаем пустой результат в случае ошибки
                return TavilyResponse(
                    query=query,
                    results=[],
                    response_time=time.time() - start_time,
                    request_id=request_id,
                ).model_dump()
        
        # Конвертируем результаты SearXNG в формат Tavily
        results = []
        searxng_results = searxng_data.get("results", [])
        
        # Если нужен raw_content, скрапим страницы
        raw_contents = {}
        if include_raw_content and searxng_results:
            urls_to_scrape = [r["url"] for r in searxng_results[:max_results] if r.get("url")]
            
            async with aiohttp.ClientSession() as scrape_session:
                tasks = [self._fetch_raw_content(scrape_session, url) for url in urls_to_scrape]
                page_contents = await asyncio.gather(*tasks, return_exceptions=True)
                
                for url, content in zip(urls_to_scrape, page_contents):
                    if isinstance(content, str) and content:
                        raw_contents[url] = content
        
        for i, result in enumerate(searxng_results[:max_results]):
            if not result.get("url"):
                continue
                
            raw_content = None
            if include_raw_content:
                raw_content = raw_contents.get(result["url"])
                
            tavily_result = TavilyResult(
                url=result["url"],
                title=result.get("title", ""),
                content=result.get("content", ""),
                score=0.9 - (i * 0.05),  # Простая имитация скора
                raw_content=raw_content
            )
            results.append(tavily_result)
        
        response_time = time.time() - start_time
        
        return TavilyResponse(
            query=query,
            follow_up_questions=None,
            answer=None,
            images=[],
            results=results,
            response_time=response_time,
            request_id=request_id,
        ).model_dump()
