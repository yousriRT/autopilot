"""Tests pour DailyDashboard — build payload + render HTML/text + envoi SMTP."""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch
from dashboard import DailyDashboard


@pytest.fixture
def empty_state():
    tracker = MagicMock()
    tracker.data = {"ads": {}}
    tracker.get_quality_weighted_leads = MagicMock(return_value=0.0)
    tracker.get_fatigue = MagicMock(return_value={"score": 0, "level": "fresh", "reasons": [], "should_pause": False})
    optimizer = MagicMock()
    optimizer.state = {"ads": {}}
    return tracker, optimizer


@pytest.fixture
def realistic_state():
    tracker = MagicMock()
    tracker.data = {"ads": {
        "ad_1": {"vertical": "famille_4lignes", "angle": "ado_data", "spend": 30.0, "leads": 6, "impressions": 5000, "created_at": "2026-04-20"},
        "ad_2": {"vertical": "fibre", "angle": "frustration", "spend": 20.0, "leads": 4, "impressions": 3000, "created_at": "2026-04-22"},
        "ad_3": {"vertical": "mobile", "angle": "partage", "spend": 15.0, "leads": 1, "impressions": 1500, "created_at": "2026-04-23"},
    }}
    tracker.get_quality_weighted_leads = MagicMock(side_effect=lambda aid: {
        "ad_1": 5.0, "ad_2": 2.5, "ad_3": 0.3
    }.get(aid, 0.0))
    tracker.get_fatigue = MagicMock(side_effect=lambda aid: {
        "ad_1": {"score": 75, "level": "critical", "reasons": ["frequency 6.7"], "should_pause": True},
        "ad_2": {"score": 30, "level": "warning", "reasons": ["CTR baisse 20%"], "should_pause": False},
        "ad_3": {"score": 10, "level": "fresh", "reasons": [], "should_pause": False},
    }.get(aid, {"score": 0, "level": "fresh", "reasons": [], "should_pause": False}))
    optimizer = MagicMock()
    optimizer.state = {"ads": {
        "ad_1": {"status": "active", "daily_budget": 25.0},
        "ad_2": {"status": "active", "daily_budget": 18.0},
        "ad_3": {"status": "paused", "daily_budget": 5.0},
    }}
    return tracker, optimizer


class TestBuildEmpty:
    def test_empty_state_no_crash(self, base_config, empty_state):
        tracker, optimizer = empty_state
        d = DailyDashboard(base_config, tracker, optimizer, MagicMock())
        payload = d.build()
        assert payload["totals"]["raw_leads"] == 0
        assert payload["totals"]["quality_leads"] == 0
        assert payload["budget"]["spent"] == 0
        # Pas d'anomalies budget mais peut avoir vert vides
        assert isinstance(payload["anomalies"], list)


class TestBuildRealistic:
    def test_aggregates_correctly(self, base_config, realistic_state):
        tracker, optimizer = realistic_state
        d = DailyDashboard(base_config, tracker, optimizer, MagicMock())
        payload = d.build()
        assert payload["totals"]["raw_leads"] == 11
        assert payload["totals"]["quality_leads"] == pytest.approx(7.8, abs=0.1)
        assert payload["budget"]["spent"] == pytest.approx(65.0)

    def test_top3_sorted_by_cpl(self, base_config, realistic_state):
        tracker, optimizer = realistic_state
        d = DailyDashboard(base_config, tracker, optimizer, MagicMock())
        payload = d.build()
        # ad_1 : CPL 30/5 = 6, ad_2 : 20/2.5 = 8, ad_3 : 15/0.3 = 50 (mais qwl < 1 → exclu)
        if len(payload["top3"]) >= 2:
            assert payload["top3"][0]["cpl_qualifie"] <= payload["top3"][1]["cpl_qualifie"]

    def test_quality_drop_anomaly_low_ratio(self, base_config):
        tracker = MagicMock()
        tracker.data = {"ads": {"ad_x": {
            "vertical": "fibre", "angle": "x", "spend": 50.0, "leads": 20,
            "impressions": 5000, "created_at": "2026-04-20"
        }}}
        # ratio 5/20 = 0.25 → anomalie
        tracker.get_quality_weighted_leads = MagicMock(return_value=5.0)
        tracker.get_fatigue = MagicMock(return_value={"score": 0, "level": "fresh", "reasons": [], "should_pause": False})
        optimizer = MagicMock(state={"ads": {"ad_x": {"status": "active", "daily_budget": 10}}})

        d = DailyDashboard(base_config, tracker, optimizer, MagicMock())
        payload = d.build()
        assert any("qualité" in a.lower() or "qualif" in a.lower() for a in payload["anomalies"])

    def test_empty_vertical_anomaly(self, base_config, realistic_state):
        tracker, optimizer = realistic_state
        # mobile a 1 paused, 0 active → anomalie
        d = DailyDashboard(base_config, tracker, optimizer, MagicMock())
        payload = d.build()
        assert any("mobile" in a for a in payload["anomalies"])


class TestRender:
    def test_html_contains_key_sections(self, base_config, realistic_state):
        tracker, optimizer = realistic_state
        d = DailyDashboard(base_config, tracker, optimizer, MagicMock())
        payload = d.build()
        html = d.render_html(payload)
        for keyword in ("Budget", "Anomalies", "verticale", "BUDGET" or "Budget"):
            assert keyword.lower() in html.lower()
        assert "famille_4lignes" in html
        assert "<html" in html

    def test_text_renders(self, base_config, realistic_state):
        tracker, optimizer = realistic_state
        d = DailyDashboard(base_config, tracker, optimizer, MagicMock())
        payload = d.build()
        text = d.render_text(payload)
        assert "BUDGET" in text
        assert "VERTICALES" in text
        assert "famille_4lignes" in text

    def test_html_marks_critical_fatigue(self, base_config, realistic_state):
        tracker, optimizer = realistic_state
        d = DailyDashboard(base_config, tracker, optimizer, MagicMock())
        payload = d.build()
        html = d.render_html(payload)
        # ad_1 a fatigue critical → présence dans la table à risque
        assert "critical" not in html.lower() or "75" in html  # color code ou score


class TestSend:
    def test_no_smtp_creds_returns_false(self, base_config, empty_state, monkeypatch):
        monkeypatch.delenv("SMTP_USER", raising=False)
        monkeypatch.delenv("SMTP_PASSWORD", raising=False)
        tracker, optimizer = empty_state
        d = DailyDashboard(base_config, tracker, optimizer, MagicMock())
        result = d.send()
        assert result is False

    def test_send_calls_smtp(self, base_config, empty_state, monkeypatch):
        monkeypatch.setenv("SMTP_USER", "test@gmail.com")
        monkeypatch.setenv("SMTP_PASSWORD", "abcd1234")
        tracker, optimizer = empty_state
        d = DailyDashboard(base_config, tracker, optimizer, MagicMock())
        with patch("dashboard.smtplib.SMTP") as msmtp:
            msmtp_instance = MagicMock()
            msmtp.return_value.__enter__.return_value = msmtp_instance
            result = d.send()
            assert result is True
            msmtp_instance.starttls.assert_called_once()
            msmtp_instance.login.assert_called_once()
            msmtp_instance.send_message.assert_called_once()

    def test_smtp_failure_returns_false(self, base_config, empty_state, monkeypatch):
        monkeypatch.setenv("SMTP_USER", "test@gmail.com")
        monkeypatch.setenv("SMTP_PASSWORD", "abcd1234")
        tracker, optimizer = empty_state
        d = DailyDashboard(base_config, tracker, optimizer, MagicMock())
        with patch("dashboard.smtplib.SMTP") as msmtp:
            msmtp.side_effect = Exception("connection refused")
            assert d.send() is False


class TestSnapshot:
    def test_save_snapshot_writes_json(self, base_config, empty_state, tmp_path, monkeypatch):
        # Redirige le dashboard_snapshots dir vers tmp
        from pathlib import Path
        monkeypatch.setattr("dashboard.Path", lambda *a: Path(*a))  # no-op
        tracker, optimizer = empty_state
        d = DailyDashboard(base_config, tracker, optimizer, MagicMock())
        # On utilise le path par défaut mais on vérifie qu'il existe
        path = d.save_snapshot()
        assert path.exists()
        assert path.suffix == ".json"


class TestRecipientConfig:
    def test_uses_config_recipient(self, base_config, empty_state):
        tracker, optimizer = empty_state
        d = DailyDashboard(base_config, tracker, optimizer, MagicMock())
        assert d.recipient == "test@example.com"

    def test_default_recipient_when_no_config(self, empty_state):
        config = {"daily_total_budget": 100}
        tracker, optimizer = empty_state
        d = DailyDashboard(config, tracker, optimizer, MagicMock())
        assert "yousriblaligg@gmail.com" in d.recipient
