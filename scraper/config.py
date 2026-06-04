import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv


def _find_dotenv() -> Path | None:
    global_env = Path.home() / ".config" / "ebay-scraper" / ".env"
    if global_env.exists():
        return global_env
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        return cwd_env
    return None


_env_path = _find_dotenv()
if _env_path:
    load_dotenv(dotenv_path=_env_path)


@dataclass
class Settings:
    redis_url: str
    database_url: str
    proxy_url: str | None
    requests_per_second: float
    worker_concurrency: int
    max_rps_per_ip: float
    batch_size: int
    challenge_escalation_threshold: float
    challenge_cooldown_seconds: float

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
        self.worker_concurrency = int(os.getenv("WORKER_CONCURRENCY", "8"))
        self.max_rps_per_ip = float(os.getenv("MAX_RPS_PER_IP", "6"))
        self.batch_size = int(os.getenv("BATCH_SIZE", "200"))
        self.challenge_escalation_threshold = float(os.getenv("CHALLENGE_ESCALATION_THRESHOLD", "0.15"))
        self.challenge_cooldown_seconds = float(os.getenv("CHALLENGE_COOLDOWN_SECONDS", "120"))
