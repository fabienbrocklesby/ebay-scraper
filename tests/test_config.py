import pytest
from scraper.config import Settings

def test_loads_from_env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://test:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@test/db")
    monkeypatch.setenv("PROXY_URL", "")
    monkeypatch.setenv("REQUESTS_PER_SECOND", "1.0")
    s = Settings()
    assert s.redis_url == "redis://test:6379"
    assert s.database_url == "postgresql://u:p@test/db"
    assert s.proxy_url is None
    assert s.requests_per_second == 1.0

def test_proxy_url_none_when_empty(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://test:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@test/db")
    monkeypatch.setenv("PROXY_URL", "")
    s = Settings()
    assert s.proxy_url is None

def test_missing_redis_url_raises(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@test/db")
    with pytest.raises(ValueError, match="REDIS_URL"):
        Settings()

def test_missing_database_url_raises(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://test:6379")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings()

def test_requests_per_second_defaults_to_half(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://test:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@test/db")
    monkeypatch.delenv("REQUESTS_PER_SECOND", raising=False)
    s = Settings()
    assert s.requests_per_second == 0.5


def test_settings_concurrency_defaults(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.delenv("WORKER_CONCURRENCY", raising=False)
    monkeypatch.delenv("MAX_RPS_PER_IP", raising=False)
    monkeypatch.delenv("BATCH_SIZE", raising=False)
    from scraper.config import Settings
    s = Settings()
    assert s.worker_concurrency == 8
    assert s.max_rps_per_ip == 6.0
    assert s.batch_size == 200
    assert s.challenge_escalation_threshold == 0.15
    assert s.challenge_cooldown_seconds == 120.0


def test_settings_concurrency_overrides(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("WORKER_CONCURRENCY", "16")
    monkeypatch.setenv("MAX_RPS_PER_IP", "10")
    monkeypatch.setenv("BATCH_SIZE", "100")
    from scraper.config import Settings
    s = Settings()
    assert s.worker_concurrency == 16
    assert s.max_rps_per_ip == 10.0
    assert s.batch_size == 100
