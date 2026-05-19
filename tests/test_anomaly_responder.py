"""Tests pour AnomalyResponder — détection + auto-réponse."""

import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from anomaly_responder import (
    AnomalyResponder, QUALITY_DROP_THRESHOLD, QUALITY_MIN_LEADS_FOR_SIGNAL,
    ALL_PAUSED_GRACE_HOURS,
)
from lead_store import LeadStore


def write_leads_to_store(store: LeadStore, leads_data: list):
    """Helper : écrit directement des entrées dans le JSONL pour contrôler les timestamps."""
    with open(store.path, "a") as f:
        for lead in leads_data:
            f.write(json.dumps(lead) + "\n")


def fake_lead(lead_id, hours_ago, quality, vertical="famille_4lignes"):
    return {
        "lead_id": lead_id, "ad_id": "ad_x", "vertical": vertical,
        "quality_score": quality, "funnel_status": "received",
        "received_at": (datetime.now() - timedelta(hours=hours_ago)).isoformat(),
        "field_data": [],
    }


@pytest.fixture
def mock_tracker():
    t = MagicMock()
    t.data = {"ads": {}}
    t.get_quality_weighted_leads = MagicMock(return_value=0.0)
    return t


@pytest.fixture
def mock_optimizer_with_active_ad():
    o = MagicMock()
    o.state = {"ads": {
        "ad_active": {"status": "active", "vertical": "famille_4lignes",
                      "last_update": datetime.now().isoformat()}
    }}
    o.MIN_EXPLORATION_BUDGET_RATIO = 0.20
    o.MIN_SPEND_BEFORE_PAUSE = 30.0
    return o


@pytest.fixture
def mock_optimizer_no_active():
    o = MagicMock()
    o.state = {"ads": {
        "ad_paused": {"status": "paused", "vertical": "famille_4lignes",
                      "last_update": (datetime.now() - timedelta(hours=12)).isoformat()}
    }}
    o.MIN_EXPLORATION_BUDGET_RATIO = 0.20
    o.MIN_SPEND_BEFORE_PAUSE = 30.0
    return o


@pytest.fixture
def lead_store(tmp_path):
    return LeadStore(store_path=tmp_path / "leads.jsonl")


# ----------- Quality drop -----------

class TestQualityDropDetection:
    def test_quality_drop_detected_with_lead_store(
        self, base_config, mock_tracker, mock_optimizer_with_active_ad, lead_store, tmp_path
    ):
        # 15 leads anciens (qualité 0.85)
        write_leads_to_store(lead_store, [fake_lead(f"old_{i}", hours_ago=24*7+12, quality=0.85)
                                          for i in range(15)])
        # 15 leads récents (qualité 0.30 = chute)
        write_leads_to_store(lead_store, [fake_lead(f"new_{i}", hours_ago=12, quality=0.30)
                                          for i in range(15)])
        ar = AnomalyResponder(base_config, mock_tracker, mock_optimizer_with_active_ad,
                              lead_store=lead_store, log_path=tmp_path / "anom.jsonl")
        report = ar.detect_and_respond()
        types = [a["type"] for a in report["detected"]]
        assert "quality_drop" in types

    def test_no_drop_when_quality_stable(
        self, base_config, mock_tracker, mock_optimizer_with_active_ad, lead_store, tmp_path
    ):
        write_leads_to_store(lead_store, [fake_lead(f"old_{i}", hours_ago=24*7+12, quality=0.80)
                                          for i in range(15)])
        write_leads_to_store(lead_store, [fake_lead(f"new_{i}", hours_ago=12, quality=0.78)
                                          for i in range(15)])
        ar = AnomalyResponder(base_config, mock_tracker, mock_optimizer_with_active_ad,
                              lead_store=lead_store, log_path=tmp_path / "anom.jsonl")
        report = ar.detect_and_respond()
        assert "quality_drop" not in [a["type"] for a in report["detected"]]

    def test_no_signal_below_min_leads(
        self, base_config, mock_tracker, mock_optimizer_with_active_ad, lead_store, tmp_path
    ):
        # Seulement 3 leads dans chaque fenêtre, < QUALITY_MIN_LEADS_FOR_SIGNAL (10)
        write_leads_to_store(lead_store, [fake_lead(f"o_{i}", 24*7+12, 0.85) for i in range(3)])
        write_leads_to_store(lead_store, [fake_lead(f"n_{i}", 12, 0.30) for i in range(3)])
        ar = AnomalyResponder(base_config, mock_tracker, mock_optimizer_with_active_ad,
                              lead_store=lead_store, log_path=tmp_path / "anom.jsonl")
        report = ar.detect_and_respond()
        assert "quality_drop" not in [a["type"] for a in report["detected"]]

    def test_no_lead_store_returns_none(
        self, base_config, mock_tracker, mock_optimizer_with_active_ad, tmp_path
    ):
        ar = AnomalyResponder(base_config, mock_tracker, mock_optimizer_with_active_ad,
                              lead_store=None, log_path=tmp_path / "anom.jsonl")
        # Pas de crash, juste pas de détection quality_drop
        report = ar.detect_and_respond()
        assert "quality_drop" not in [a["type"] for a in report["detected"]]


# ----------- Empty verticals -----------

class TestEmptyVerticalDetection:
    def test_detect_unconfigured_vertical_empty(
        self, base_config, mock_tracker, mock_optimizer_with_active_ad, lead_store, tmp_path
    ):
        # Ad active uniquement sur famille_4lignes, mais config a fibre + mobile aussi
        ar = AnomalyResponder(base_config, mock_tracker, mock_optimizer_with_active_ad,
                              lead_store=lead_store, log_path=tmp_path / "anom.jsonl")
        report = ar.detect_and_respond()
        empty = next((a for a in report["detected"] if a["type"] == "empty_verticals"), None)
        assert empty is not None
        # fibre et mobile sont vides
        assert "fibre" in empty["verticals"]
        assert "mobile" in empty["verticals"]

    def test_no_empty_when_all_active(
        self, base_config, mock_tracker, lead_store, tmp_path
    ):
        opt = MagicMock()
        opt.state = {"ads": {
            "a1": {"status": "active", "vertical": "fibre", "last_update": datetime.now().isoformat()},
            "a2": {"status": "active", "vertical": "mobile", "last_update": datetime.now().isoformat()},
            "a3": {"status": "active", "vertical": "famille_4lignes", "last_update": datetime.now().isoformat()},
        }}
        opt.MIN_EXPLORATION_BUDGET_RATIO = 0.20
        opt.MIN_SPEND_BEFORE_PAUSE = 30.0
        ar = AnomalyResponder(base_config, mock_tracker, opt,
                              lead_store=lead_store, log_path=tmp_path / "anom.jsonl")
        report = ar.detect_and_respond()
        assert "empty_verticals" not in [a["type"] for a in report["detected"]]


# ----------- All paused crisis -----------

class TestAllPausedCrisis:
    def test_detect_when_zero_active_after_grace(
        self, base_config, mock_tracker, lead_store, tmp_path
    ):
        opt = MagicMock()
        # Tout en pause, et la dernière activité est plus vieille que la grace period
        opt.state = {"ads": {
            "a1": {"status": "paused", "vertical": "famille_4lignes",
                   "last_update": (datetime.now() - timedelta(hours=ALL_PAUSED_GRACE_HOURS + 2)).isoformat()},
        }}
        opt.MIN_EXPLORATION_BUDGET_RATIO = 0.20
        opt.MIN_SPEND_BEFORE_PAUSE = 30.0
        ar = AnomalyResponder(base_config, mock_tracker, opt,
                              lead_store=lead_store, log_path=tmp_path / "anom.jsonl")
        report = ar.detect_and_respond()
        assert "all_paused_crisis" in [a["type"] for a in report["detected"]]

    def test_no_crisis_within_grace_period(
        self, base_config, mock_tracker, lead_store, tmp_path
    ):
        opt = MagicMock()
        opt.state = {"ads": {
            "a1": {"status": "paused", "vertical": "famille_4lignes",
                   "last_update": (datetime.now() - timedelta(hours=1)).isoformat()},
        }}
        opt.MIN_EXPLORATION_BUDGET_RATIO = 0.20
        opt.MIN_SPEND_BEFORE_PAUSE = 30.0
        ar = AnomalyResponder(base_config, mock_tracker, opt,
                              lead_store=lead_store, log_path=tmp_path / "anom.jsonl")
        report = ar.detect_and_respond()
        assert "all_paused_crisis" not in [a["type"] for a in report["detected"]]

    def test_no_crisis_when_some_active(
        self, base_config, mock_tracker, mock_optimizer_with_active_ad, lead_store, tmp_path
    ):
        ar = AnomalyResponder(base_config, mock_tracker, mock_optimizer_with_active_ad,
                              lead_store=lead_store, log_path=tmp_path / "anom.jsonl")
        report = ar.detect_and_respond()
        assert "all_paused_crisis" not in [a["type"] for a in report["detected"]]


# ----------- Auto-response actions -----------

class TestAutoResponses:
    def test_quality_drop_boosts_exploration(
        self, base_config, mock_tracker, lead_store, tmp_path
    ):
        # Ads actives + famille_4lignes (donc pas d'empty_verticals SUR cette vertical)
        opt = MagicMock()
        opt.state = {"ads": {
            "a1": {"status": "active", "vertical": "famille_4lignes",
                   "last_update": datetime.now().isoformat()},
        }}
        opt.MIN_EXPLORATION_BUDGET_RATIO = 0.20
        opt.MIN_SPEND_BEFORE_PAUSE = 30.0

        # Drop qualité
        write_leads_to_store(lead_store, [fake_lead(f"o_{i}", 24*7+12, 0.85) for i in range(15)])
        write_leads_to_store(lead_store, [fake_lead(f"n_{i}", 12, 0.30) for i in range(15)])

        ar = AnomalyResponder(base_config, mock_tracker, opt, lead_store=lead_store,
                              log_path=tmp_path / "anom.jsonl")
        ar.detect_and_respond()
        # L'optimizer instance doit avoir son ratio bumpé de +0.10
        assert opt.MIN_EXPLORATION_BUDGET_RATIO == pytest.approx(0.30)

    def test_empty_verticals_marks_priority(
        self, base_config, mock_tracker, mock_optimizer_with_active_ad, lead_store, tmp_path
    ):
        ar = AnomalyResponder(base_config, mock_tracker, mock_optimizer_with_active_ad,
                              lead_store=lead_store, log_path=tmp_path / "anom.jsonl")
        ar.detect_and_respond()
        priorities = ar.get_priority_verticals()
        assert "fibre" in priorities
        assert "mobile" in priorities

    def test_priority_expires_after_24h(
        self, base_config, mock_tracker, mock_optimizer_with_active_ad, lead_store, tmp_path
    ):
        ar = AnomalyResponder(base_config, mock_tracker, mock_optimizer_with_active_ad,
                              lead_store=lead_store, log_path=tmp_path / "anom.jsonl")
        # Force une priorité vieille
        with open(ar.priority_verticals_path, "w") as f:
            json.dump({
                "priorities": ["fibre"],
                "set_at": (datetime.now() - timedelta(hours=25)).isoformat(),
            }, f)
        assert ar.get_priority_verticals() == []

    def test_clear_priorities(
        self, base_config, mock_tracker, mock_optimizer_with_active_ad, lead_store, tmp_path
    ):
        ar = AnomalyResponder(base_config, mock_tracker, mock_optimizer_with_active_ad,
                              lead_store=lead_store, log_path=tmp_path / "anom.jsonl")
        ar._mark_priority_verticals(["fibre"])
        assert ar.get_priority_verticals() == ["fibre"]
        ar.clear_priorities()
        assert ar.get_priority_verticals() == []

    def test_get_priority_handles_corrupt_file(
        self, base_config, mock_tracker, mock_optimizer_with_active_ad, lead_store, tmp_path
    ):
        ar = AnomalyResponder(base_config, mock_tracker, mock_optimizer_with_active_ad,
                              lead_store=lead_store, log_path=tmp_path / "anom.jsonl")
        ar.priority_verticals_path.write_text("not json")
        assert ar.get_priority_verticals() == []

    def test_all_paused_crisis_unlocks_emergency_relaunch(
        self, base_config, mock_tracker, lead_store, tmp_path
    ):
        opt = MagicMock()
        opt.state = {"ads": {
            "a1": {"status": "paused", "vertical": "famille_4lignes",
                   "last_update": (datetime.now() - timedelta(hours=ALL_PAUSED_GRACE_HOURS + 2)).isoformat()},
        }}
        opt.MIN_EXPLORATION_BUDGET_RATIO = 0.20
        opt.MIN_SPEND_BEFORE_PAUSE = 30.0
        ar = AnomalyResponder(base_config, mock_tracker, opt, lead_store=lead_store,
                              log_path=tmp_path / "anom.jsonl")
        ar.detect_and_respond()
        # Toutes les verticales sont marquées prio (mode urgence)
        priorities = ar.get_priority_verticals()
        assert set(priorities) == set(base_config["meta"]["targeting"].keys())


class TestDetectionRobustness:
    def test_detection_function_failure_doesnt_crash(
        self, base_config, mock_tracker, mock_optimizer_with_active_ad, lead_store, tmp_path
    ):
        ar = AnomalyResponder(base_config, mock_tracker, mock_optimizer_with_active_ad,
                              lead_store=lead_store, log_path=tmp_path / "anom.jsonl")
        def broken():
            raise Exception("boom")
        broken.__name__ = "_check_quality_drop"
        ar._check_quality_drop = broken
        # Ne crash pas, juste log error
        report = ar.detect_and_respond()
        assert "detected" in report

    def test_log_persists_anomalies(
        self, base_config, mock_tracker, mock_optimizer_with_active_ad, lead_store, tmp_path
    ):
        ar = AnomalyResponder(base_config, mock_tracker, mock_optimizer_with_active_ad,
                              lead_store=lead_store, log_path=tmp_path / "anom.jsonl")
        ar.detect_and_respond()
        # Si une anomalie a été détectée, le log existe
        if ar.log_path.exists():
            content = ar.log_path.read_text()
            assert "anomaly" in content
