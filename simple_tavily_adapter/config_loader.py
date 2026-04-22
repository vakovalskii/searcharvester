"""
Configuration loader for Tavily adapter
"""
import yaml
from pathlib import Path
from typing import Dict, Any

class Config:
    def __init__(self, config_path: str = "/srv/searxng-docker/config.yaml"):
        self.config_path = Path(config_path)
        self._config = self._load_config()
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from unified YAML file"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except FileNotFoundError:
            # Fallback to default config
            return {
                "adapter": {
                    "searxng_url": "http://searxng:8080",
                    "server": {"host": "0.0.0.0", "port": 8000},
                    "scraper": {
                        "timeout": 10,
                        "max_content_length": 2500,
                        "user_agent": "Mozilla/5.0 (compatible; TavilyBot/1.0)"
                    }
                }
            }
    
    @property
    def searxng_url(self) -> str:
        return self._config.get("adapter", {}).get("searxng_url", "http://searxng:8080")
    
    @property
    def server_host(self) -> str:
        return self._config.get("adapter", {}).get("server", {}).get("host", "0.0.0.0")
    
    @property
    def server_port(self) -> int:
        return self._config.get("adapter", {}).get("server", {}).get("port", 8000)
    
    @property
    def scraper_timeout(self) -> int:
        return self._config.get("adapter", {}).get("scraper", {}).get("timeout", 10)
    
    @property
    def scraper_max_length(self) -> int:
        return self._config.get("adapter", {}).get("scraper", {}).get("max_content_length", 2500)
    
    @property
    def scraper_user_agent(self) -> str:
        return self._config.get("adapter", {}).get("scraper", {}).get("user_agent", "Mozilla/5.0 (compatible; TavilyBot/1.0)")
    
    @property
    def default_max_results(self) -> int:
        return self._config.get("adapter", {}).get("search", {}).get("default_max_results", 10)
    
    @property
    def default_engines(self) -> str:
        return self._config.get("adapter", {}).get("search", {}).get("default_engines", "google,duckduckgo,brave")

# Глобальный экземпляр конфига
config = Config()
