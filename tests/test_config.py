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
