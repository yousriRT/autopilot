"""Tests pour BudgetGuardian + ProcessLock + check_account_health."""

import os
import time
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from safety import (
    BudgetGuardian, ProcessLock, BudgetGuardianTriggered, check_account_health,
    HARD_CAP_MULTIPLIER, LOCK_TTL_MIN,
)


class TestBudgetGuardian:
    def test_initial_not_killed(self, base_config, mock_publisher, tmp_path):
        g = BudgetGuardian(base_config, mock_publisher, state_path=tmp_path / "g.json")
        assert g.is_killed() is False

    def test_under_cap_returns_true_no_kill(self, base_config, mock_publisher, tmp_path):
        # 50€ < cap 100 × 1.2 = 120€
        mock_publisher.get_active_ads.return_value = [{"id": "ad_1"}]
        mock_publisher.get_ad_insights.return_value = {"spend": 50.0}
        g = BudgetGuardian(base_config, mock_publisher, state_path=tmp_path / "g.json")
        assert g.check_and_enforce() is True
        assert g.is_killed() is False
        mock_publisher.pause_ad.assert_not_called()

    def test_over_cap_kills_and_pauses(self, base_config, mock_publisher, tmp_path):
        mock_publisher.get_active_ads.return_value = [
            {"id": "ad_1"}, {"id": "ad_2"}, {"id": "ad_3"}
        ]
        mock_publisher.get_ad_insights.return_value = {"spend": 50.0}  # 3 × 50 = 150 > 120
        g = BudgetGuardian(base_config, mock_publisher, state_path=tmp_path / "g.json")
        assert g.check_and_enforce() is False
        assert g.is_killed() is True
        # Toutes les ads pausées
        assert mock_publisher.pause_ad.call_count == 3

    def test_account_spend_strengthens_killswitch(self, base_config, mock_publisher, tmp_path):
        # #5 : somme par ad faible (10) mais dépense COMPTE du jour élevée (200)
        # → le kill-switch se déclenche quand même.
        mock_publisher.get_active_ads.return_value = [{"id": "ad_1"}]
        mock_publisher.get_ad_insights.return_value = {"spend": 10.0}
        mock_publisher.get_account_spend_today.return_value = 200.0  # > 120
        g = BudgetGuardian(base_config, mock_publisher, state_path=tmp_path / "g.json")
        assert g.check_and_enforce() is False
        assert g.is_killed() is True

    def test_account_spend_error_falls_back_to_per_ad(self, base_config, mock_publisher, tmp_path):
        mock_publisher.get_active_ads.return_value = [{"id": "ad_1"}]
        mock_publisher.get_ad_insights.return_value = {"spend": 50.0}
        mock_publisher.get_account_spend_today.side_effect = Exception("api down")
        g = BudgetGuardian(base_config, mock_publisher, state_path=tmp_path / "g.json")
        # 50 < 120 et le compte est indispo → on ne kill pas
        assert g.check_and_enforce() is True

    def test_already_killed_no_op(self, base_config, mock_publisher, tmp_path):
        g = BudgetGuardian(base_config, mock_publisher, state_path=tmp_path / "g.json")
        g.state = {"killed_at": datetime.now().isoformat(), "killed_reason": "test"}
        g._save()
        assert g.is_killed() is True
        # Re-check doit retourner False (already killed) sans rien faire
        result = g.check_and_enforce()
        assert result is False
        mock_publisher.get_active_ads.assert_not_called()

    def test_reset_on_new_day(self, base_config, mock_publisher, tmp_path):
        g = BudgetGuardian(base_config, mock_publisher, state_path=tmp_path / "g.json")
        # Kill d'hier
        g.state = {"killed_at": (datetime.now() - timedelta(days=1)).isoformat(), "killed_reason": "old"}
        g._save()
        g.reset_if_new_day()
        assert g.is_killed() is False
        assert g.state["killed_at"] is None

    def test_no_reset_same_day(self, base_config, mock_publisher, tmp_path):
        g = BudgetGuardian(base_config, mock_publisher, state_path=tmp_path / "g.json")
        # Kill il y a 1 heure (même jour)
        g.state = {"killed_at": (datetime.now() - timedelta(hours=1)).isoformat(), "killed_reason": "today"}
        g._save()
        g.reset_if_new_day()
        assert g.is_killed() is True

    def test_fail_open_on_meta_error(self, base_config, mock_publisher, tmp_path):
        """Si l'API Meta tombe, on ne bloque pas le cron — on fail-open."""
        mock_publisher.get_active_ads.side_effect = Exception("Meta down")
        g = BudgetGuardian(base_config, mock_publisher, state_path=tmp_path / "g.json")
        assert g.check_and_enforce() is True  # OK to proceed

    def test_per_ad_insights_failure_doesnt_block(self, base_config, mock_publisher, tmp_path):
        mock_publisher.get_active_ads.return_value = [{"id": "ad_1"}, {"id": "ad_2"}]
        mock_publisher.get_ad_insights.side_effect = [Exception("fail"), {"spend": 50.0}]
        g = BudgetGuardian(base_config, mock_publisher, state_path=tmp_path / "g.json")
        # Sum = 50 (l'autre est skippé), < cap → ok
        assert g.check_and_enforce() is True

    def test_corrupt_state_recovery(self, base_config, mock_publisher, tmp_path):
        path = tmp_path / "g.json"
        path.write_text('{"killed_at": "garbage"}')
        g = BudgetGuardian(base_config, mock_publisher, state_path=path)
        # ne crash pas, considère pas killé
        assert g.is_killed() is False

    def test_state_persists(self, base_config, mock_publisher, tmp_path):
        path = tmp_path / "g.json"
        mock_publisher.get_active_ads.return_value = [{"id": "ad_1"}]
        mock_publisher.get_ad_insights.return_value = {"spend": 200.0}
        g1 = BudgetGuardian(base_config, mock_publisher, state_path=path)
        g1.check_and_enforce()
        # Reload
        g2 = BudgetGuardian(base_config, mock_publisher, state_path=path)
        assert g2.is_killed() is True

    def test_accepts_string_path(self, base_config, mock_publisher, tmp_path):
        g = BudgetGuardian(base_config, mock_publisher, state_path=str(tmp_path / "g.json"))
        assert g.state_path.exists() is False or g.state == {"killed_at": None, "killed_reason": None, "killed_total_spend": None}


class TestProcessLock:
    def test_acquires_when_no_lock(self, tmp_path):
        with ProcessLock(name="test_op", lock_dir=tmp_path) as lock:
            assert lock.lock_path.exists()
        assert not lock.lock_path.exists()  # cleanup au exit

    def test_blocks_concurrent(self, tmp_path):
        # Premier lock acquis, on tente le second
        ProcessLock(name="op", lock_dir=tmp_path).__enter__()
        with pytest.raises(RuntimeError, match="Lock"):
            with ProcessLock(name="op", lock_dir=tmp_path):
                pass

    def test_takes_over_stale_lock(self, tmp_path):
        # Lock fichier vieux > LOCK_TTL_MIN
        lock_path = tmp_path / "stale.lock"
        lock_path.write_text("12345\nold")
        old_time = time.time() - (LOCK_TTL_MIN + 5) * 60
        os.utime(lock_path, (old_time, old_time))
        # Doit prendre la main
        with ProcessLock(name="stale", lock_dir=tmp_path):
            pass

    def test_cleanup_on_exception(self, tmp_path):
        try:
            with ProcessLock(name="op", lock_dir=tmp_path) as lock:
                lock_path = lock.lock_path
                raise RuntimeError("test")
        except RuntimeError:
            pass
        assert not lock_path.exists()

    def test_writes_pid_and_timestamp(self, tmp_path):
        with ProcessLock(name="op", lock_dir=tmp_path) as lock:
            content = lock.lock_path.read_text()
            assert str(os.getpid()) in content
            # Une ligne timestamp aussi
            assert len(content.strip().split("\n")) >= 1

    def test_accepts_string_lock_dir(self, tmp_path):
        with ProcessLock(name="op", lock_dir=str(tmp_path)):
            pass

    def test_named_locks_isolated(self, tmp_path):
        # Deux locks de nom différent peuvent coexister
        with ProcessLock(name="optimize", lock_dir=tmp_path):
            with ProcessLock(name="launch", lock_dir=tmp_path):
                pass


class TestCheckAccountHealth:
    def test_account_active_returns_ok(self, mock_publisher):
        mock_publisher._request.return_value = {
            "account_status": 1, "disable_reason": 0, "name": "test"
        }
        ok, msg = check_account_health(mock_publisher)
        assert ok is True
        assert msg == "OK"

    def test_account_disabled_returns_false(self, mock_publisher):
        mock_publisher._request.return_value = {
            "account_status": 2, "disable_reason": 5, "name": "disabled"
        }
        ok, msg = check_account_health(mock_publisher)
        assert ok is False
        assert "account_status=2" in msg

    def test_unreachable_fails_open(self, mock_publisher):
        """Si l'API tombe en check, on fail-open (pas de blocage cron)."""
        mock_publisher._request.side_effect = Exception("network down")
        ok, msg = check_account_health(mock_publisher)
        assert ok is True  # fail-open
        assert "fail-open" in msg

    def test_account_status_none_treated_as_unhealthy(self, mock_publisher):
        mock_publisher._request.return_value = {"account_status": None}
        ok, _ = check_account_health(mock_publisher)
        assert ok is False
