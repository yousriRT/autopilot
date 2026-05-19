"""Tests pour resilience : retry exponentiel + dead letter queue."""

import time
import pytest
from datetime import datetime, timedelta
from resilience import retry_with_backoff, DeadLetterQueue


class TestRetryDecorator:
    def test_succeeds_on_first_try(self):
        calls = {"n": 0}
        @retry_with_backoff(max_attempts=3, base_delay=0.001)
        def fn():
            calls["n"] += 1
            return "ok"
        assert fn() == "ok"
        assert calls["n"] == 1

    def test_succeeds_after_failures(self):
        calls = {"n": 0}
        @retry_with_backoff(max_attempts=4, base_delay=0.001)
        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("fail")
            return "ok"
        assert fn() == "ok"
        assert calls["n"] == 3

    def test_raises_after_max_attempts(self):
        calls = {"n": 0}
        @retry_with_backoff(max_attempts=3, base_delay=0.001)
        def fn():
            calls["n"] += 1
            raise ConnectionError("permanent")
        with pytest.raises(ConnectionError):
            fn()
        assert calls["n"] == 3

    def test_non_retryable_not_caught(self):
        @retry_with_backoff(max_attempts=3, base_delay=0.001)
        def fn():
            raise KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            fn()

    def test_only_specified_exceptions_retry(self):
        calls = {"n": 0}
        @retry_with_backoff(
            max_attempts=3, base_delay=0.001,
            retryable_exceptions=(ConnectionError,),
        )
        def fn():
            calls["n"] += 1
            raise ValueError("not retryable for this decorator")
        with pytest.raises(ValueError):
            fn()
        assert calls["n"] == 1

    def test_backoff_grows_exponentially(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
        calls = {"n": 0}
        @retry_with_backoff(max_attempts=4, base_delay=1.0, max_delay=100.0)
        def fn():
            calls["n"] += 1
            raise ConnectionError("fail")
        with pytest.raises(ConnectionError):
            fn()
        # 3 sleeps (entre 4 tentatives), croissants
        assert len(sleeps) == 3
        # Avec jitter, on vérifie que ça grandit globalement
        assert sleeps[2] > sleeps[0]

    def test_backoff_capped_at_max_delay(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
        @retry_with_backoff(max_attempts=10, base_delay=1.0, max_delay=2.0)
        def fn():
            raise ConnectionError("fail")
        with pytest.raises(ConnectionError):
            fn()
        # Tous les sleeps sont <= 2.0 + jitter (30%) = 2.6
        assert all(s <= 2.6 for s in sleeps)

    def test_decorator_preserves_function_name(self):
        @retry_with_backoff()
        def original_name():
            pass
        assert original_name.__name__ == "original_name"


class TestDeadLetterQueue:
    def test_add_and_persist(self, tmp_path):
        dlq = DeadLetterQueue(path=tmp_path / "dlq.json")
        dlq.add(operation="op1", payload={"k": "v"}, error="boom")
        # Reload depuis disque
        dlq2 = DeadLetterQueue(path=tmp_path / "dlq.json")
        assert len(dlq2._items) == 1
        assert dlq2._items[0]["operation"] == "op1"

    def test_add_extra_meta_fields(self, tmp_path):
        dlq = DeadLetterQueue(path=tmp_path / "dlq.json")
        dlq.add(operation="op1", payload={}, error="x", vertical="famille_4lignes", attempts=3)
        assert dlq._items[0]["vertical"] == "famille_4lignes"
        assert dlq._items[0]["attempts"] == 3

    def test_error_truncated_to_1000_chars(self, tmp_path):
        dlq = DeadLetterQueue(path=tmp_path / "dlq.json")
        dlq.add(operation="op1", payload={}, error="x" * 5000)
        assert len(dlq._items[0]["error"]) == 1000

    def test_capped_at_200_items(self, tmp_path):
        dlq = DeadLetterQueue(path=tmp_path / "dlq.json")
        for i in range(250):
            dlq.add(operation=f"op_{i}", payload={}, error="x")
        assert len(dlq._items) == 200
        # Les 50 premiers sont virés → reste op_50 à op_249
        ops = [i["operation"] for i in dlq._items]
        assert "op_249" in ops
        assert "op_0" not in ops

    def test_list_pending_filters_by_age(self, tmp_path):
        dlq = DeadLetterQueue(path=tmp_path / "dlq.json")
        # Vieux : 50h
        dlq._items.append({
            "ts": (datetime.now() - timedelta(hours=50)).isoformat(),
            "operation": "old",
        })
        # Récent
        dlq.add(operation="recent", payload={}, error="x")
        pending = dlq.list_pending(max_age_hours=48)
        ops = [p["operation"] for p in pending]
        assert "recent" in ops
        assert "old" not in ops

    def test_list_pending_handles_invalid_ts(self, tmp_path):
        dlq = DeadLetterQueue(path=tmp_path / "dlq.json")
        dlq._items.append({"ts": "garbage", "operation": "broken"})
        # Ne doit pas crasher
        pending = dlq.list_pending(max_age_hours=48)
        assert all(p.get("operation") != "broken" for p in pending)

    def test_clear_processed_removes_matching(self, tmp_path):
        dlq = DeadLetterQueue(path=tmp_path / "dlq.json")
        dlq.add(operation="resolved", payload={}, error="x")
        dlq.add(operation="still_failing", payload={}, error="y")
        dlq.clear_processed(["resolved"])
        ops = [i["operation"] for i in dlq._items]
        assert "resolved" not in ops
        assert "still_failing" in ops

    def test_corrupt_file_resets_to_empty(self, tmp_path):
        path = tmp_path / "dlq.json"
        path.write_text("not json")
        dlq = DeadLetterQueue(path=path)
        assert dlq._items == []

    def test_accepts_string_path(self, tmp_path):
        dlq = DeadLetterQueue(path=str(tmp_path / "dlq.json"))
        dlq.add(operation="op", payload={}, error="x")
        assert dlq.path.exists()
