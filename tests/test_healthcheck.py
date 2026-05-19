"""Tests pour healthcheck — diagnostic complet."""

import os
import pytest
from unittest.mock import MagicMock, patch
from healthcheck import run_healthcheck, HEALTH_OK, HEALTH_DEGRADED, HEALTH_CRITICAL


@pytest.fixture
def env_setup(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("META_ACCESS_TOKEN", "EAA-test")
    monkeypatch.setenv("ARCADS_API_KEY", "arcads-test")


@pytest.fixture
def healthy_setup(env_setup, base_config, mock_publisher):
    claude = MagicMock()
    # models.list returns iterable
    claude.models.list.return_value = iter([MagicMock()])
    mock_publisher._request.return_value = {
        "account_status": 1, "disable_reason": 0, "name": "ok account"
    }
    optimizer = MagicMock()
    optimizer.state = {"ads": {
        "a1": {"status": "active", "vertical": "fibre"},
        "a2": {"status": "active", "vertical": "mobile"},
        "a3": {"status": "active", "vertical": "famille_4lignes"},
    }}
    return claude, mock_publisher, optimizer


class TestRunHealthcheck:
    def test_all_green(self, healthy_setup, base_config):
        claude, publisher, optimizer = healthy_setup
        exit_code, report = run_healthcheck(base_config, claude, publisher, optimizer)
        # Pas critique au moins
        assert exit_code <= HEALTH_DEGRADED
        assert report["status"] in ("OK", "DEGRADED")

    def test_missing_anthropic_key_critical(self, base_config, mock_publisher, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("META_ACCESS_TOKEN", "x")
        monkeypatch.setenv("ARCADS_API_KEY", "x")
        claude = MagicMock()
        claude.models.list.return_value = iter([MagicMock()])
        mock_publisher._request.return_value = {"account_status": 1, "disable_reason": 0}
        opt = MagicMock(state={"ads": {}})
        exit_code, report = run_healthcheck(base_config, claude, mock_publisher, opt)
        assert exit_code == HEALTH_CRITICAL
        assert any("ANTHROPIC_API_KEY" in e for e in report["errors"])

    def test_anthropic_unreachable_critical(self, env_setup, base_config, mock_publisher):
        claude = MagicMock()
        claude.models.list.side_effect = Exception("API down")
        mock_publisher._request.return_value = {"account_status": 1, "disable_reason": 0}
        opt = MagicMock(state={"ads": {}})
        exit_code, report = run_healthcheck(base_config, claude, mock_publisher, opt)
        assert exit_code == HEALTH_CRITICAL
        assert any("Anthropic" in e for e in report["errors"])

    def test_meta_account_disabled_critical(self, env_setup, base_config, mock_publisher):
        claude = MagicMock()
        claude.models.list.return_value = iter([MagicMock()])
        mock_publisher._request.return_value = {"account_status": 2, "disable_reason": 5}
        opt = MagicMock(state={"ads": {}})
        exit_code, report = run_healthcheck(base_config, claude, mock_publisher, opt)
        assert exit_code == HEALTH_CRITICAL

    def test_killswitch_active_degraded(self, env_setup, base_config, mock_publisher, tmp_path):
        from datetime import datetime
        # Les imports sont locaux dans run_healthcheck → patch sur le module source
        with patch("safety.BudgetGuardian") as mbg:
            instance = MagicMock()
            instance.is_killed.return_value = True
            instance.state = {"killed_at": datetime.now().isoformat(), "killed_reason": "test"}
            mbg.return_value = instance
            claude = MagicMock()
            claude.models.list.return_value = iter([MagicMock()])
            mock_publisher._request.return_value = {"account_status": 1, "disable_reason": 0}
            opt = MagicMock(state={"ads": {}})
            exit_code, report = run_healthcheck(base_config, claude, mock_publisher, opt)
            assert exit_code >= HEALTH_DEGRADED
            assert report["checks"]["budget_killswitch"] == "TRIGGERED"

    def test_empty_verticals_warns(self, env_setup, base_config, mock_publisher):
        claude = MagicMock()
        claude.models.list.return_value = iter([MagicMock()])
        mock_publisher._request.return_value = {"account_status": 1, "disable_reason": 0}
        # Aucune ad active
        opt = MagicMock(state={"ads": {}})
        exit_code, report = run_healthcheck(base_config, claude, mock_publisher, opt)
        # Verticales vides → warning
        assert any("vide" in w.lower() or "Verticales" in w for w in report["warnings"])

    def test_report_has_timestamp(self, healthy_setup, base_config):
        claude, publisher, optimizer = healthy_setup
        _, report = run_healthcheck(base_config, claude, publisher, optimizer)
        assert "timestamp" in report
        assert "checks" in report

    def test_report_includes_status_string(self, healthy_setup, base_config):
        claude, publisher, optimizer = healthy_setup
        _, report = run_healthcheck(base_config, claude, publisher, optimizer)
        assert report["status"] in ("OK", "DEGRADED", "CRITICAL")

    def test_dlq_pending_warns_when_high(self, env_setup, base_config, mock_publisher, tmp_path):
        with patch("resilience.DeadLetterQueue") as mdlq:
            instance = MagicMock()
            instance.list_pending.return_value = [{"x": 1}] * 15
            mdlq.return_value = instance
            claude = MagicMock()
            claude.models.list.return_value = iter([MagicMock()])
            mock_publisher._request.return_value = {"account_status": 1, "disable_reason": 0}
            opt = MagicMock(state={"ads": {}})
            _, report = run_healthcheck(base_config, claude, mock_publisher, opt)
            assert report["checks"]["dlq_pending_48h"] == 15
            assert any("DLQ" in w for w in report["warnings"])
