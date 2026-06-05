from unittest.mock import MagicMock
from scraper.queue import enqueue_items, is_item_queued, mark_item_queued


def make_mock_redis(already_queued: set = None):
    r = MagicMock()
    already_queued = already_queued or set()
    r.sismember = MagicMock(side_effect=lambda key, val: val in already_queued)
    r.sadd = MagicMock()
    return r


def test_enqueue_items_enqueues_all_new():
    mock_redis = make_mock_redis()
    mock_queue = MagicMock()
    item_urls = [
        "https://www.ebay.com/itm/111",
        "https://www.ebay.com/itm/222",
        "https://www.ebay.com/itm/333",
    ]
    count = enqueue_items(
        mock_queue, mock_redis, item_urls,
        niche="test", store_url="https://www.ebay.com/str/s"
    )
    # All 3 URLs fit in a single batch (batch size >= 3), so one enqueue call.
    assert mock_queue.enqueue.call_count == 1
    assert count == 3


def test_enqueue_items_skips_already_queued():
    # item_id "111" is already in the set
    mock_redis = make_mock_redis(already_queued={"111"})
    mock_queue = MagicMock()
    item_urls = [
        "https://www.ebay.com/itm/111",
        "https://www.ebay.com/itm/222",
    ]
    count = enqueue_items(
        mock_queue, mock_redis, item_urls,
        niche="test", store_url="https://www.ebay.com/str/s"
    )
    # Only 1 new URL, fits in one batch.
    assert mock_queue.enqueue.call_count == 1
    assert count == 1


def test_enqueue_items_batches_by_size(monkeypatch):
    import scraper.queue as q
    monkeypatch.setattr(q, "_BATCH_SIZE", 2)
    queue = MagicMock()
    redis_conn = MagicMock()
    redis_conn.sismember.return_value = False
    urls = [f"https://www.ebay.com.au/itm/{i}" for i in ("111", "222", "333")]
    count = enqueue_items(queue, redis_conn, urls, "watch", "https://store")
    assert count == 3
    assert queue.enqueue.call_count == 2


def test_is_item_queued_true_when_member():
    r = make_mock_redis(already_queued={"abc"})
    assert is_item_queued(r, "abc") is True


def test_is_item_queued_false_when_not_member():
    r = make_mock_redis()
    assert is_item_queued(r, "xyz") is False


def test_mark_item_queued_calls_sadd():
    r = make_mock_redis()
    mark_item_queued(r, "999")
    r.sadd.assert_called_once_with("scraped_items", "999")


def test_queue_is_drained_true_when_empty():
    from unittest.mock import patch, MagicMock
    from scraper.queue import queue_is_drained

    mock_queue = MagicMock()
    mock_queue.count = 0

    mock_registry = MagicMock()
    mock_registry.count = 0

    with patch("scraper.queue.StartedJobRegistry", return_value=mock_registry), \
         patch("scraper.queue.DeferredJobRegistry", return_value=mock_registry), \
         patch("scraper.queue.ScheduledJobRegistry", return_value=mock_registry):
        assert queue_is_drained(mock_queue) is True


def test_queue_is_drained_false_when_pending():
    from unittest.mock import patch, MagicMock
    from scraper.queue import queue_is_drained

    mock_queue = MagicMock()
    mock_queue.count = 5

    mock_registry = MagicMock()
    mock_registry.count = 0

    with patch("scraper.queue.StartedJobRegistry", return_value=mock_registry), \
         patch("scraper.queue.DeferredJobRegistry", return_value=mock_registry), \
         patch("scraper.queue.ScheduledJobRegistry", return_value=mock_registry):
        assert queue_is_drained(mock_queue) is False


def test_queue_is_drained_false_when_running():
    from unittest.mock import patch, MagicMock
    from scraper.queue import queue_is_drained

    mock_queue = MagicMock()
    mock_queue.count = 0

    started_registry = MagicMock()
    started_registry.count = 2
    empty_registry = MagicMock()
    empty_registry.count = 0

    with patch("scraper.queue.StartedJobRegistry", return_value=started_registry), \
         patch("scraper.queue.DeferredJobRegistry", return_value=empty_registry), \
         patch("scraper.queue.ScheduledJobRegistry", return_value=empty_registry):
        assert queue_is_drained(mock_queue) is False
