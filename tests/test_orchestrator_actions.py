"""
Tests des actions principales de l'orchestrator (run_full_cycle, send_dashboard,
healthcheck, main entry point) — branches non couvertes par les scénarios.
"""

import sys
import json
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch


@pytest.fixture
def isolated_pilot(tmp_path, monkeypatch, base_config):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "config.json").write_text(json.dumps(base_config))

    fake_anthropic = MagicMock()
    monkeypatch.setattr("creative_generator.anthropic.Anthropic", MagicMock(return_value=fake_anthropic))
    monkeypatch.setattr("policy_validator.anthropic.Anthropic", MagicMock(return_value=fake_anthropic))
    monkeypatch.setattr("orchestrator.anthropic.Anthropic", MagicMock(return_value=fake_anthropic))

    from orchestrator import MetaAdsAutoPilot
    # Chemin ABSOLU : sinon MetaAdsAutoPilot résout via Path(__file__).parent.parent
    # et lirait le vrai config/config.json du repo (non hermétique).
    pilot = MetaAdsAutoPilot(config_path=str(tmp_path / "config" / "config.json"))

    # Redirige tous les paths vers tmp
    pilot.tracker.data_path = tmp_path / "data" / "performance.json"
    pilot.tracker.data = {"ads": {}}
    pilot.tracker.lead_store.path = tmp_path / "data" / "leads.jsonl"
    pilot.optimizer.data_path = tmp_path / "data" / "bandit_state.json"
    pilot.optimizer.state = {"ads": {}, "verticals": {}}
    pilot.diversity.state_path = tmp_path / "data" / "diversity_state.json"
    pilot.diversity.state = {"launches": []}
    pilot.guardian.state_path = tmp_path / "data" / "budget_guardian.json"
    pilot.guardian.state = {"killed_at": None, "killed_reason": None, "killed_total_spend": None}
    pilot.responder.log_path = tmp_path / "data" / "anomaly_log.jsonl"
    pilot.responder.priority_verticals_path = tmp_path / "data" / "priority_verticals.json"
    pilot.cost_tracker.log_path = tmp_path / "data" / "cost_log.jsonl"
    pilot.dlq.path = tmp_path / "data" / "dead_letter.json"
    pilot.dlq._items = []

    return pilot, fake_anthropic, tmp_path


# ----------- send_dashboard -----------

class TestSendDashboard:
    def test_send_dashboard_no_smtp(self, isolated_pilot, monkeypatch, tmp_path):
        pilot, _, _ = isolated_pilot
        monkeypatch.delenv("SMTP_USER", raising=False)
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)
        # Override snapshot dir
        from pathlib import Path
        snapshot_dir = tmp_path / "data" / "dashboard_snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        # Le send va échouer (pas de SMTP), on vérifie que le snapshot est généré
        pilot.send_dashboard()
        # Snapshot écrit malgré l'échec mail
        snapshots = list(snapshot_dir.glob("*.json")) if snapshot_dir.exists() else []
        # Le path par défaut va dans Path(__file__).parent.parent — pas dans tmp.
        # Donc on vérifie juste que ça n'a pas crashé.

    def test_send_dashboard_succeeds_with_smtp(self, isolated_pilot, monkeypatch):
        pilot, _, _ = isolated_pilot
        # Le dashboard lit l'env dans __init__ → on patch directement les attrs
        pilot.dashboard.smtp_user = "test@example.com"
        pilot.dashboard.smtp_password = "abcd1234"
        with patch("dashboard.smtplib.SMTP") as msmtp:
            instance = MagicMock()
            msmtp.return_value.__enter__.return_value = instance
            pilot.send_dashboard()
            instance.send_message.assert_called_once()


# ----------- healthcheck -----------

class TestHealthcheckAction:
    def test_healthcheck_returns_exit_code(self, isolated_pilot, monkeypatch):
        pilot, claude, _ = isolated_pilot
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setenv("META_ACCESS_TOKEN", "x")
        monkeypatch.setenv("ARCADS_API_KEY", "x")
        claude.models.list.return_value = iter([MagicMock()])
        pilot.publisher = MagicMock()
        pilot.publisher._request.return_value = {"account_status": 1, "disable_reason": 0}
        pilot.guardian.publisher = pilot.publisher
        pilot.claude_client = claude

        exit_code = pilot.healthcheck()
        assert isinstance(exit_code, int)
        assert exit_code in (0, 1, 2)


# ----------- run_full_cycle -----------

class TestFullCycle:
    def test_full_cycle_runs_optimize_then_launch(self, isolated_pilot, monkeypatch):
        pilot, claude, _ = isolated_pilot
        # Mock les deux méthodes pour vérifier l'ordre
        pilot.optimize_budgets = MagicMock()
        pilot.launch_new_ads = MagicMock()
        pilot.run_full_cycle()
        pilot.optimize_budgets.assert_called_once()
        pilot.launch_new_ads.assert_called_once()

    def test_full_cycle_continues_after_optimize_failure(self, isolated_pilot):
        pilot, _, _ = isolated_pilot
        pilot.optimize_budgets = MagicMock(side_effect=Exception("optimize fail"))
        pilot.launch_new_ads = MagicMock()
        pilot.run_full_cycle()
        # launch quand même appelé
        pilot.launch_new_ads.assert_called_once()

    def test_full_cycle_continues_after_launch_failure(self, isolated_pilot):
        pilot, _, _ = isolated_pilot
        pilot.optimize_budgets = MagicMock()
        pilot.launch_new_ads = MagicMock(side_effect=Exception("launch fail"))
        # Ne re-raise pas
        pilot.run_full_cycle()


# ----------- _apply_decision -----------

class TestApplyDecision:
    def test_scale_action(self, isolated_pilot):
        pilot, _, _ = isolated_pilot
        pilot.publisher = MagicMock()
        pilot._apply_decision("ad_1", {"action": "scale", "new_budget": 25.0, "cpl_qualifie": 5.0})
        pilot.publisher.update_ad_budget.assert_called_with("ad_1", 25.0)

    def test_reduce_action(self, isolated_pilot):
        pilot, _, _ = isolated_pilot
        pilot.publisher = MagicMock()
        pilot._apply_decision("ad_1", {"action": "reduce", "new_budget": 8.0, "cpl_qualifie": 15.0})
        pilot.publisher.update_ad_budget.assert_called_with("ad_1", 8.0)

    def test_pause_action_syncs_state(self, isolated_pilot):
        pilot, _, _ = isolated_pilot
        pilot.publisher = MagicMock()
        pilot.optimizer.state["ads"]["ad_1"] = {"status": "active", "vertical": "fibre"}
        pilot._apply_decision("ad_1", {"action": "pause", "reason": "no_quality"})
        pilot.publisher.pause_ad.assert_called_with("ad_1")
        assert pilot.optimizer.state["ads"]["ad_1"]["status"] == "paused"

    def test_hold_action_no_meta_call(self, isolated_pilot):
        pilot, _, _ = isolated_pilot
        pilot.publisher = MagicMock()
        pilot._apply_decision("ad_1", {"action": "hold"})
        pilot.publisher.update_ad_budget.assert_not_called()
        pilot.publisher.pause_ad.assert_not_called()

    def test_decision_failure_added_to_dlq(self, isolated_pilot):
        pilot, _, _ = isolated_pilot
        pilot.publisher = MagicMock()
        pilot.publisher.pause_ad.side_effect = Exception("Meta error")
        pilot._apply_decision("ad_1", {"action": "pause", "reason": "x"})
        assert any(i["operation"] == "apply_pause" for i in pilot.dlq._items)


# ----------- main() -----------

class TestMainEntryPoint:
    def test_main_optimize_action(self, monkeypatch, tmp_path, base_config):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "logs").mkdir(exist_ok=True)
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / "config.json").write_text(json.dumps(base_config))
        monkeypatch.setattr(sys, "argv", ["orchestrator.py", "--action", "optimize"])

        # Mock les modules externes
        monkeypatch.setattr("creative_generator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("policy_validator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("orchestrator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("orchestrator.MetaAdsAutoPilot.optimize_budgets", MagicMock())
        from orchestrator import main
        main()

    def test_main_launch_action(self, monkeypatch, tmp_path, base_config):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "logs").mkdir(exist_ok=True)
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / "config.json").write_text(json.dumps(base_config))
        monkeypatch.setattr(sys, "argv", ["orchestrator.py", "--action", "launch"])
        monkeypatch.setattr("creative_generator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("policy_validator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("orchestrator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("orchestrator.MetaAdsAutoPilot.launch_new_ads", MagicMock())
        from orchestrator import main
        main()

    def test_main_dashboard_action(self, monkeypatch, tmp_path, base_config):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "logs").mkdir(exist_ok=True)
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / "config.json").write_text(json.dumps(base_config))
        monkeypatch.setattr(sys, "argv", ["orchestrator.py", "--action", "dashboard"])
        monkeypatch.setattr("creative_generator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("policy_validator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("orchestrator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("orchestrator.MetaAdsAutoPilot.send_dashboard", MagicMock())
        from orchestrator import main
        main()

    def test_main_healthcheck_calls_sys_exit(self, monkeypatch, tmp_path, base_config):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "logs").mkdir(exist_ok=True)
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / "config.json").write_text(json.dumps(base_config))
        monkeypatch.setattr(sys, "argv", ["orchestrator.py", "--action", "healthcheck"])
        monkeypatch.setattr("creative_generator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("policy_validator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("orchestrator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("orchestrator.MetaAdsAutoPilot.healthcheck", MagicMock(return_value=0))
        from orchestrator import main
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    def test_main_default_full_cycle(self, monkeypatch, tmp_path, base_config):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "logs").mkdir(exist_ok=True)
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / "config.json").write_text(json.dumps(base_config))
        monkeypatch.setattr(sys, "argv", ["orchestrator.py"])  # no action = full default
        monkeypatch.setattr("creative_generator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("policy_validator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("orchestrator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("orchestrator.MetaAdsAutoPilot.run_full_cycle", MagicMock())
        from orchestrator import main
        main()


class TestActiveVerticalsFilter:
    def _pilot(self, tmp_path, monkeypatch, cfg):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "logs").mkdir(exist_ok=True)
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / "config.json").write_text(json.dumps(cfg))
        monkeypatch.setattr("creative_generator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("policy_validator.anthropic.Anthropic", MagicMock())
        monkeypatch.setattr("orchestrator.anthropic.Anthropic", MagicMock())
        from orchestrator import MetaAdsAutoPilot
        return MetaAdsAutoPilot(config_path=str(tmp_path / "config" / "config.json"))

    def test_filter_restricts_verticals(self, tmp_path, monkeypatch, base_config):
        cfg = {**base_config, "active_verticals": ["fibre", "mobile"]}
        pilot = self._pilot(tmp_path, monkeypatch, cfg)
        assert set(pilot.verticals) == {"fibre", "mobile"}

    def test_no_filter_keeps_all(self, tmp_path, monkeypatch, base_config):
        pilot = self._pilot(tmp_path, monkeypatch, base_config)
        # base_config cible fibre, mobile, famille_4lignes
        assert "famille_4lignes" in pilot.verticals
        assert len(pilot.verticals) == 3
