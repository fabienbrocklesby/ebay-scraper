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
    count = enqueue_items(
        mock_queue, mock_redis, ["111", "222", "333"],
        niche="test", store_url="https://www.ebay.com/str/s"
    )
    assert mock_queue.enqueue.call_count == 3
    assert count == 3


def test_enqueue_items_skips_already_queued():
    mock_redis = make_mock_redis(already_queued={"111"})
    mock_queue = MagicMock()
    count = enqueue_items(
        mock_queue, mock_redis, ["111", "222"],
        niche="test", store_url="https://www.ebay.com/str/s"
    )
    assert mock_queue.enqueue.call_count == 1
    assert count == 1


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
