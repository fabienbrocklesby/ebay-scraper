import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Settings:
    redis_url: str
    database_url: str
    proxy_url: str | None
    requests_per_second: float

    def __init__(self):
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            raise ValueError("REDIS_URL is required")
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise ValueError("DATABASE_URL is required")
        proxy_raw = os.getenv("PROXY_URL", "")
        self.redis_url = redis_url
        self.database_url = database_url
        self.proxy_url = proxy_raw if proxy_raw else None
        self.requests_per_second = float(os.getenv("REQUESTS_PER_SECOND", "0.5"))
