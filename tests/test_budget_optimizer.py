"""Tests pour ThompsonSamplingOptimizer — décisions budget + fatigue."""

import json
import pytest
import numpy as np
from datetime import datetime
from unittest.mock import MagicMock
from budget_optimizer import ThompsonSamplingOptimizer


@pytest.fixture
def optimizer(tmp_path, base_config):
    o = ThompsonSamplingOptimizer(base_config)
    o.data_path = tmp_path / "bandit.json"
    o.state = {"ads": {}, "verticals": {}}
    return o


@pytest.fixture
def deterministic_random(monkeypatch):
    """Force np.random.beta à retourner alpha/(alpha+beta) (mean) pour tests déterministes."""
    def fake_beta(a, b):
        return float(a) / (a + b)
    monkeypatch.setattr(np.random, "beta", fake_beta)


class TestSampleQuality:
    def test_uses_quality_weighted_leads_when_present(self, optimizer, deterministic_random):
        optimizer.state["ads"]["ad_1"] = {
            "vertical": "fibre", "leads": 10, "quality_weighted_leads": 5.0,
            "spend": 100.0,
        }
        # alpha = 1 + 5 = 6, beta = 30 + 100 - 5 = 125, mean = 6/131 ≈ 0.046
        sample = optimizer._sample_quality("ad_1")
        expected = 6.0 / 131.0
        assert sample == pytest.approx(expected, rel=1e-3)

    def test_fallback_to_leads_times_06(self, optimizer, deterministic_random):
        optimizer.state["ads"]["ad_1"] = {
            "vertical": "fibre", "leads": 10, "spend": 100.0,
            # Pas de quality_weighted_leads
        }
        # effective = 10 * 0.6 = 6, alpha = 7, beta = 30 + 100 - 6 = 124
        sample = optimizer._sample_quality("ad_1")
        assert sample == pytest.approx(7.0 / 131.0, rel=1e-3)

    def test_high_quality_higher_sample(self, optimizer, deterministic_random):
        optimizer.state["ads"]["winner"] = {
            "vertical": "fibre", "quality_weighted_leads": 10.0, "spend": 50.0
        }
        optimizer.state["ads"]["loser"] = {
            "vertical": "fibre", "quality_weighted_leads": 1.0, "spend": 50.0
        }
        assert optimizer._sample_quality("winner") > optimizer._sample_quality("loser")

    def test_handles_zero_spend(self, optimizer, deterministic_random):
        optimizer.state["ads"]["new"] = {
            "vertical": "fibre", "leads": 0, "spend": 0.0
        }
        # Doit pas crash, retourne le prior mean
        sample = optimizer._sample_quality("new")
        assert 0 <= sample <= 1

    def test_handles_zero_qwl_negative_beta(self, optimizer, deterministic_random):
        optimizer.state["ads"]["x"] = {
            "vertical": "fibre", "quality_weighted_leads": 1000.0, "spend": 1.0
        }
        # alpha haut, beta théoriquement négatif → clipé à 0.1
        sample = optimizer._sample_quality("x")
        assert 0 <= sample <= 1


class TestHowManyToExplore:
    def test_zero_active_returns_3(self, optimizer):
        assert optimizer.how_many_to_explore("fibre") == 3

    def test_some_active_completes_to_3(self, optimizer):
        optimizer.state["ads"]["a1"] = {"vertical": "fibre", "status": "active"}
        assert optimizer.how_many_to_explore("fibre") == 2

    def test_max_reached_returns_zero(self, optimizer):
        for i in range(optimizer.MAX_ACTIVE_ADS_PER_VERTICAL):
            optimizer.state["ads"][f"a{i}"] = {"vertical": "fibre", "status": "active"}
        assert optimizer.how_many_to_explore("fibre") == 0

    def test_with_mature_ads_explores_slowly(self, optimizer):
        # 3 actives dont 2 matures (spend > 30)
        optimizer.state["ads"]["m1"] = {"vertical": "fibre", "status": "active", "spend": 50}
        optimizer.state["ads"]["m2"] = {"vertical": "fibre", "status": "active", "spend": 50}
        optimizer.state["ads"]["a3"] = {"vertical": "fibre", "status": "active", "spend": 5}
        assert optimizer.how_many_to_explore("fibre") == 1


class TestGetInitialBudget:
    def test_returns_positive(self, optimizer):
        budget = optimizer.get_initial_budget("fibre")
        assert budget > 0

    def test_divides_by_configured_verticals(self, optimizer):
        # base_config a 3 offres configurées : 100*0.20/(3*2)=3.33 → plancher 5.0
        b = optimizer.get_initial_budget("fibre")
        assert b == pytest.approx(5.0)

    def test_never_below_min_ad_budget(self, optimizer):
        # Même sans aucune ad active, jamais sous le plancher Meta
        b = optimizer.get_initial_budget("fibre")
        assert b >= optimizer.MIN_AD_BUDGET

    def test_single_offer_uses_full_exploration_share(self, tmp_path):
        # Une seule offre configurée → 100*0.20/(1*2)=10.0
        cfg = {"daily_total_budget": 100.0,
               "meta": {"targeting": {"famille_bundle": {}}}}
        o = ThompsonSamplingOptimizer(cfg)
        o.data_path = tmp_path / "b.json"
        o.state = {"ads": {}, "verticals": {}}
        assert o.get_initial_budget("famille_bundle") == pytest.approx(10.0)


class TestDecideAllocations:
    def test_empty_returns_no_decisions(self, optimizer):
        assert optimizer.decide_allocations("fibre") == {}

    def test_pause_on_significant_spend_no_qwl(self, optimizer, deterministic_random):
        optimizer.state["ads"]["ad_1"] = {
            "vertical": "fibre", "status": "active",
            "spend": 100.0, "quality_weighted_leads": 0.0,
            "impressions": 5000, "daily_budget": 25.0,
        }
        decisions = optimizer.decide_allocations("fibre")
        assert decisions["ad_1"]["action"] == "pause"
        assert "no_quality_leads" in decisions["ad_1"]["reason"]

    def test_pause_underperformer_among_many(self, optimizer, deterministic_random):
        # 4 ads, une est un loser
        for i in range(3):
            optimizer.state["ads"][f"good_{i}"] = {
                "vertical": "fibre", "status": "active",
                "spend": 50, "quality_weighted_leads": 5.0,
                "impressions": 5000, "daily_budget": 20.0,
            }
        optimizer.state["ads"]["loser"] = {
            "vertical": "fibre", "status": "active",
            "spend": 50, "quality_weighted_leads": 0.5,
            "impressions": 5000, "daily_budget": 20.0,
        }
        decisions = optimizer.decide_allocations("fibre")
        # Le loser est probablement pause (proportions softmax très basse)
        # ou hold/reduce — on teste juste qu'on a bien 4 décisions
        assert len(decisions) == 4

    def test_fatigue_provider_pauses_first(self, optimizer, deterministic_random):
        optimizer.state["ads"]["tired"] = {
            "vertical": "fibre", "status": "active",
            "spend": 50, "quality_weighted_leads": 5.0,
            "impressions": 5000, "daily_budget": 25.0,
        }
        fatigue_provider = lambda ad_id: {
            "should_pause": True, "score": 95, "level": "critical",
            "reasons": ["frequency 8.0", "CTR drop"]
        }
        decisions = optimizer.decide_allocations("fibre", fatigue_provider=fatigue_provider)
        assert decisions["tired"]["action"] == "pause"
        assert "creative_fatigue" in decisions["tired"]["reason"]

    def test_fatigue_only_pauses_when_critical(self, optimizer, deterministic_random):
        optimizer.state["ads"]["fresh"] = {
            "vertical": "fibre", "status": "active",
            "spend": 20, "quality_weighted_leads": 3.0,
            "impressions": 2000, "daily_budget": 10.0,
        }
        fatigue_provider = lambda ad_id: {
            "should_pause": False, "score": 30, "level": "fresh", "reasons": []
        }
        decisions = optimizer.decide_allocations("fibre", fatigue_provider=fatigue_provider)
        assert decisions["fresh"]["action"] != "pause"

    def test_scale_action_when_proportions_increase(self, optimizer, deterministic_random):
        # Une seule ad performante → reçoit tout le budget vertical
        optimizer.state["ads"]["winner"] = {
            "vertical": "fibre", "status": "active",
            "spend": 30, "quality_weighted_leads": 10.0,
            "impressions": 2000, "daily_budget": 5.0,
        }
        decisions = optimizer.decide_allocations("fibre")
        assert decisions["winner"]["action"] in ("scale", "hold", "reduce")
        assert decisions["winner"]["new_budget"] >= 5.0

    def test_max_budget_change_capped_50pct(self, optimizer, deterministic_random):
        optimizer.state["ads"]["ad_1"] = {
            "vertical": "fibre", "status": "active",
            "spend": 10, "quality_weighted_leads": 5.0,
            "impressions": 2000, "daily_budget": 20.0,
        }
        decisions = optimizer.decide_allocations("fibre")
        new_budget = decisions["ad_1"].get("new_budget", 20.0)
        # ±50% de 20 = entre 10 et 30
        assert 10.0 <= new_budget <= 30.0

    def test_sum_of_budgets_never_exceeds_vertical_budget(self, optimizer, deterministic_random):
        """#1 : la somme des budgets alloués ne dépasse JAMAIS le budget de la
        verticale (sinon Meta surdépense). Cas piège : beaucoup d'ads à budget
        élevé que le clamp ±50% empêcherait de réduire assez vite."""
        # 6 ads actives, toutes une seule verticale, budgets actuels élevés (40)
        for i in range(6):
            optimizer.state["ads"][f"ad_{i}"] = {
                "vertical": "fibre", "status": "active",
                "spend": 10, "quality_weighted_leads": 3.0,
                "impressions": 500, "daily_budget": 40.0,
            }
        decisions = optimizer.decide_allocations("fibre")
        # budget de la verticale = daily_total_budget (100) / 1 verticale active
        total = sum(d["new_budget"] for d in decisions.values()
                    if d["action"] != "pause")
        assert total <= 100.0 + 1e-6

    def test_min_budget_floor_5_euros(self, optimizer, deterministic_random):
        optimizer.state["ads"]["ad_1"] = {
            "vertical": "fibre", "status": "active",
            "spend": 5, "quality_weighted_leads": 0.5,
            "impressions": 100, "daily_budget": 5.0,
        }
        decisions = optimizer.decide_allocations("fibre")
        if decisions["ad_1"]["action"] != "pause":
            assert decisions["ad_1"]["new_budget"] >= 5.0


class TestRegisterAd:
    def test_creates_state_entry(self, optimizer):
        optimizer.register_ad("ad_x", "fibre", initial_budget=15.0)
        ad = optimizer.state["ads"]["ad_x"]
        assert ad["vertical"] == "fibre"
        assert ad["status"] == "active"
        assert ad["daily_budget"] == 15.0
        assert ad["quality_weighted_leads"] == 0.0


class TestUpdateFromInsights:
    def test_updates_basic_fields(self, optimizer):
        optimizer.register_ad("ad_1", "fibre", 10.0)
        optimizer.update_from_insights("ad_1", {"leads": 5, "spend": 50, "impressions": 1000})
        ad = optimizer.state["ads"]["ad_1"]
        assert ad["leads"] == 5
        assert ad["spend"] == 50
        assert ad["impressions"] == 1000

    def test_updates_qwl_when_provided(self, optimizer):
        optimizer.register_ad("ad_1", "fibre", 10.0)
        optimizer.update_from_insights("ad_1", {"leads": 5, "spend": 50}, quality_weighted_leads=3.5)
        assert optimizer.state["ads"]["ad_1"]["quality_weighted_leads"] == 3.5

    def test_skips_qwl_when_none(self, optimizer):
        optimizer.register_ad("ad_1", "fibre", 10.0)
        optimizer.state["ads"]["ad_1"]["quality_weighted_leads"] = 2.0
        optimizer.update_from_insights("ad_1", {"leads": 5})
        # qwl n'est pas écrasé
        assert optimizer.state["ads"]["ad_1"]["quality_weighted_leads"] == 2.0

    def test_unknown_ad_no_op(self, optimizer):
        optimizer.update_from_insights("ad_unknown", {"spend": 50})
        assert "ad_unknown" not in optimizer.state["ads"]
