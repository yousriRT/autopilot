"""Tests pour PerformanceTracker — historique perf + scoring + lead store."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from performance_tracker import PerformanceTracker
from lead_quality import LeadQualityScorer
from lead_store import LeadStore


@pytest.fixture
def tracker(tmp_path, base_config, monkeypatch):
    """Tracker isolé avec data_path redirigé vers tmp_path."""
    monkeypatch.setattr(
        "performance_tracker.Path",
        lambda *args: tmp_path  # short-circuit, on utilise un autre path direct
    )
    # Plus simple : on instancie puis on remplace data_path
    monkeypatch.undo()
    t = PerformanceTracker(
        base_config,
        lead_scorer=LeadQualityScorer(),
        lead_store=LeadStore(store_path=tmp_path / "leads.jsonl"),
    )
    t.data_path = tmp_path / "perf.json"
    t.data = {"ads": {}}
    return t


def fake_meta_lead(lead_id, email="jane@gmail.com"):
    return {
        "id": lead_id,
        "created_time": "2026-04-25T10:00:00",
        "form_id": "form_x",
        "field_data": [
            {"name": "email", "values": [email]},
            {"name": "phone_number", "values": ["+15145551234"]},
            {"name": "full_name", "values": ["Jane Tremblay"]},
            {"name": "postal_code", "values": ["H2X 1Y4"]},
        ],
    }


class TestRegisterAd:
    def test_creates_entry(self, tracker):
        tracker.register_ad("ad_1", "fibre", angle="frustration", initial_budget=10.0)
        assert "ad_1" in tracker.data["ads"]
        ad = tracker.data["ads"]["ad_1"]
        assert ad["vertical"] == "fibre"
        assert ad["angle"] == "frustration"
        assert ad["leads"] == 0
        assert ad["quality_weighted_leads"] == 0.0
        assert ad["scored_lead_ids"] == []


class TestUpdatePerformance:
    def test_updates_insights(self, tracker):
        tracker.register_ad("ad_1", "fibre", "x", 10.0)
        tracker.update_performance("ad_1", {"spend": 50.0, "impressions": 1000, "leads": 5, "reach": 700})
        ad = tracker.data["ads"]["ad_1"]
        assert ad["spend"] == 50.0
        assert ad["impressions"] == 1000
        assert ad["leads"] == 5
        assert ad["reach"] == 700

    def test_history_appended(self, tracker):
        tracker.register_ad("ad_1", "fibre", "x", 10.0)
        tracker.update_performance("ad_1", {"spend": 10})
        tracker.update_performance("ad_1", {"spend": 20})
        assert len(tracker.data["ads"]["ad_1"]["history"]) == 2

    def test_history_capped_at_100(self, tracker):
        tracker.register_ad("ad_1", "fibre", "x", 10.0)
        for _ in range(150):
            tracker.update_performance("ad_1", {"spend": 1})
        assert len(tracker.data["ads"]["ad_1"]["history"]) == 100

    def test_unknown_ad_no_op(self, tracker):
        tracker.update_performance("ad_unknown", {"spend": 50})
        assert "ad_unknown" not in tracker.data["ads"]

    def test_with_leads_data_scores_and_persists(self, tracker, tmp_path):
        tracker.register_ad("ad_1", "fibre", "x", 10.0)
        leads = [fake_meta_lead("L1"), fake_meta_lead("L2", email="bob@gmail.com")]
        tracker.update_performance("ad_1", {"spend": 20, "leads": 2}, leads_data=leads)
        ad = tracker.data["ads"]["ad_1"]
        assert ad["quality_weighted_leads"] > 0
        assert len(ad["scored_lead_ids"]) == 2
        # Les leads sont aussi dans le store
        assert tracker.lead_store.exists("L1")
        assert tracker.lead_store.exists("L2")

    def test_idempotent_scoring_per_lead(self, tracker):
        tracker.register_ad("ad_1", "fibre", "x", 10.0)
        leads = [fake_meta_lead("L1")]
        tracker.update_performance("ad_1", {"leads": 1}, leads_data=leads)
        first_qwl = tracker.data["ads"]["ad_1"]["quality_weighted_leads"]
        # Rejouer les mêmes leads ne double-compte pas
        tracker.update_performance("ad_1", {"leads": 1}, leads_data=leads)
        assert tracker.data["ads"]["ad_1"]["quality_weighted_leads"] == first_qwl

    def test_no_double_count_beyond_500_leads(self, tracker):
        """#7 : au-delà de 500 leads, le dédup persistant (lead_store) empêche
        tout double comptage, même si la trace en mémoire est tronquée."""
        tracker.register_ad("ad_1", "fibre", "x", 10.0)
        leads = [fake_meta_lead(f"L_{i}") for i in range(600)]
        tracker.update_performance("ad_1", {"leads": 600}, leads_data=leads)
        qwl_after_first = tracker.data["ads"]["ad_1"]["quality_weighted_leads"]
        assert qwl_after_first > 0
        # Rejouer les 100 premiers (dont les IDs sont sortis de la trace bornée)
        # ne doit PAS regonfler le score.
        tracker.update_performance("ad_1", {"leads": 100},
                                   leads_data=leads[:100])
        assert tracker.data["ads"]["ad_1"]["quality_weighted_leads"] == qwl_after_first


class TestGetQualityWeightedLeads:
    def test_returns_scored_value(self, tracker):
        tracker.register_ad("ad_1", "fibre", "x", 10.0)
        tracker.update_performance("ad_1", {"leads": 1}, leads_data=[fake_meta_lead("L1")])
        qwl = tracker.get_quality_weighted_leads("ad_1")
        assert qwl > 0

    def test_fallback_to_leads_times_06(self, tracker):
        tracker.register_ad("ad_1", "fibre", "x", 10.0)
        tracker.update_performance("ad_1", {"leads": 10})  # pas de leads_data
        qwl = tracker.get_quality_weighted_leads("ad_1")
        assert qwl == pytest.approx(6.0)  # 10 * 0.6

    def test_unknown_ad_returns_zero(self, tracker):
        assert tracker.get_quality_weighted_leads("ad_x") == 0.0


class TestGetFatigue:
    def test_delegates_to_detector(self, tracker):
        tracker.register_ad("ad_1", "fibre", "x", 10.0)
        result = tracker.get_fatigue("ad_1")
        assert "score" in result
        assert "level" in result

    def test_unknown_ad(self, tracker):
        result = tracker.get_fatigue("ad_x")
        assert result["should_pause"] is False
        assert "unknown" in result["reasons"][0].lower()


class TestWinningAngles:
    def test_uses_quality_weighted_leads(self, tracker):
        # Ad qui a beaucoup de leads bruts mais qualité pourrie → exclue
        tracker.register_ad("ad_volume_only", "fibre", "spam_angle", 10.0)
        # 10 leads bruts mais quality_weighted_leads = 0 (pas de leads_data scorés)
        # Avec fallback, 10 * 0.6 = 6 → ça compte
        # Pour exclure : il faut qu'il y ait des scored_lead_ids mais avec quality basse
        bad_leads = [{"id": f"BL_{i}", "field_data": [{"name": "email", "values": ["x@mailinator.com"]}]}
                    for i in range(10)]
        tracker.update_performance("ad_volume_only", {"spend": 100, "leads": 10}, leads_data=bad_leads)

        # Ad bonne qualité
        tracker.register_ad("ad_good", "fibre", "good_angle", 10.0)
        good_leads = [fake_meta_lead(f"GL_{i}") for i in range(5)]
        tracker.update_performance("ad_good", {"spend": 50, "leads": 5}, leads_data=good_leads)

        winners = tracker.get_winning_angles("fibre", last_n_days=14)
        # good_angle doit ressortir (CPL plus bas après pondération qualité)
        good_in_winners = any("good_angle" in w for w in winners)
        assert good_in_winners

    def test_winning_angles_use_lifetime_spend(self, tracker):
        """#2 : le CPL des winning angles utilise la dépense CUMULÉE, pas la
        fenêtre 24h (sinon une vieille ad paraît artificiellement rentable)."""
        tracker.register_ad("ad_1", "fibre", "angle_x", 10.0)
        good_leads = [fake_meta_lead(f"GL_{i}") for i in range(10)]
        # spend récent 5 (24h) mais spend cumulé 500 → CPL réel élevé
        tracker.update_performance("ad_1", {"spend": 5, "leads": 10},
                                   leads_data=good_leads, lifetime_spend=500.0)
        qwl = tracker.get_quality_weighted_leads("ad_1")
        winners = tracker.get_winning_angles("fibre", last_n_days=14)
        # Le CPL doit refléter la dépense cumulée (500), pas la fenêtre 24h (5)
        assert winners
        assert f"{500.0 / qwl:.2f}" in winners[0]

    def test_excludes_old_ads(self, tracker):
        tracker.register_ad("old_ad", "fibre", "old_angle", 10.0)
        tracker.data["ads"]["old_ad"]["created_at"] = (
            datetime.now() - timedelta(days=30)
        ).isoformat()
        tracker.update_performance("old_ad", {"spend": 50, "leads": 10})
        winners = tracker.get_winning_angles("fibre", last_n_days=14)
        assert all("old_angle" not in w for w in winners)

    def test_excludes_under_min_leads(self, tracker):
        tracker.register_ad("ad_1", "fibre", "low_volume", 10.0)
        tracker.update_performance("ad_1", {"spend": 50, "leads": 1})  # qwl 0.6 < 3
        winners = tracker.get_winning_angles("fibre")
        assert winners == []

    def test_filters_by_vertical(self, tracker):
        tracker.register_ad("a_fibre", "fibre", "fibre_angle", 10.0)
        tracker.update_performance("a_fibre", {"spend": 30, "leads": 10})
        tracker.register_ad("a_mobile", "mobile", "mobile_angle", 10.0)
        tracker.update_performance("a_mobile", {"spend": 30, "leads": 10})
        winners = tracker.get_winning_angles("fibre")
        assert all("fibre" in w or "frustration" in w or "fibre_angle" in w for w in winners)

    def test_invalid_created_at_skipped(self, tracker):
        tracker.register_ad("a", "fibre", "x", 10.0)
        tracker.data["ads"]["a"]["created_at"] = "not-a-date"
        tracker.update_performance("a", {"spend": 30, "leads": 10})
        # ne crash pas
        winners = tracker.get_winning_angles("fibre")
        assert isinstance(winners, list)


class TestPersistence:
    def test_save_and_reload(self, tracker, tmp_path):
        tracker.register_ad("ad_1", "fibre", "x", 10.0)
        tracker.update_performance("ad_1", {"spend": 50})
        # Reload
        t2 = PerformanceTracker(
            tracker.config if hasattr(tracker, "config") else {},
            lead_store=LeadStore(store_path=tmp_path / "leads.jsonl"),
        )
        t2.data_path = tracker.data_path
        t2.data = t2._load()
        assert "ad_1" in t2.data["ads"]
        assert t2.data["ads"]["ad_1"]["spend"] == 50
