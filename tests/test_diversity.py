"""Tests pour DiversityEnforcer — rotation LRU des catégories d'angles."""

import pytest
from datetime import datetime, timedelta
from diversity import DiversityEnforcer, CATEGORIES, VERTICAL_CATEGORIES, CATEGORY_BRIEFS


@pytest.fixture
def enforcer(tmp_path):
    return DiversityEnforcer(state_path=tmp_path / "div.json")


class TestPickNextCategory:
    def test_first_pick_returns_valid_category(self, enforcer):
        cat = enforcer.pick_next_category("famille_4lignes")
        assert cat in VERTICAL_CATEGORIES["famille_4lignes"]

    def test_pick_respects_vertical_categories(self, enforcer):
        # famille_4lignes ne doit jamais retourner social_pressure
        for _ in range(10):
            cat = enforcer.pick_next_category("famille_4lignes")
            assert cat in VERTICAL_CATEGORIES["famille_4lignes"]
            assert cat != "social_pressure"
            enforcer.record_launch("famille_4lignes", cat)

    def test_lru_rotates_through_all_categories(self, enforcer):
        """Sur N launches d'une vertical, toutes les catégories doivent apparaître."""
        valid = VERTICAL_CATEGORIES["fibre"]
        seen = set()
        for _ in range(len(valid) * 2):
            cat = enforcer.pick_next_category("fibre")
            seen.add(cat)
            enforcer.record_launch("fibre", cat)
        assert seen == set(valid), f"toutes catégories devraient apparaître, manque {set(valid)-seen}"

    def test_unknown_vertical_uses_all_categories(self, enforcer):
        cat = enforcer.pick_next_category("inconnu")
        assert cat in CATEGORIES

    def test_old_launches_dont_count_lru(self, enforcer):
        """Un launch >30j ne doit pas peser dans le LRU."""
        # Force une entrée vieille
        enforcer.state["launches"] = [{
            "ts": (datetime.now() - timedelta(days=40)).isoformat(),
            "vertical": "fibre",
            "category": "problem",
        }]
        # problem devrait être éligible (compte = 0 dans la fenêtre 30j)
        for _ in range(4):
            cat = enforcer.pick_next_category("fibre")
            enforcer.record_launch("fibre", cat)
        counts = {c: 0 for c in VERTICAL_CATEGORIES["fibre"]}
        for l in enforcer.state["launches"][1:]:  # skip la vieille
            counts[l["category"]] += 1
        # Toutes les catégories doivent avoir count 1 (rotation parfaite)
        assert max(counts.values()) - min(counts.values()) <= 1

    def test_invalid_ts_in_launches_skipped(self, enforcer):
        enforcer.state["launches"] = [{
            "ts": "not-a-date",
            "vertical": "fibre",
            "category": "problem",
        }]
        # ne doit pas crasher
        cat = enforcer.pick_next_category("fibre")
        assert cat in VERTICAL_CATEGORIES["fibre"]


class TestRecordLaunch:
    def test_record_appends_and_persists(self, enforcer, tmp_path):
        enforcer.record_launch("fibre", "problem", ad_id="ad_1", angle="lenteur")
        # Reload from disk
        e2 = DiversityEnforcer(state_path=tmp_path / "div.json")
        assert len(e2.state["launches"]) == 1
        assert e2.state["launches"][0]["vertical"] == "fibre"
        assert e2.state["launches"][0]["ad_id"] == "ad_1"

    def test_launches_capped_at_500(self, enforcer):
        for i in range(600):
            enforcer.record_launch("fibre", "problem", ad_id=f"ad_{i}")
        assert len(enforcer.state["launches"]) == 500


class TestStateLoading:
    def test_corrupt_state_fallback(self, tmp_path):
        path = tmp_path / "div.json"
        path.write_text("not json")
        e = DiversityEnforcer(state_path=path)
        assert e.state == {"launches": []}

    def test_missing_state_fallback(self, tmp_path):
        e = DiversityEnforcer(state_path=tmp_path / "missing.json")
        assert e.state == {"launches": []}


class TestBriefs:
    def test_get_brief_for_known_category(self, enforcer):
        for cat in CATEGORIES:
            brief = enforcer.get_brief(cat)
            assert isinstance(brief, str)
            assert len(brief) > 10  # vraie phrase, pas vide

    def test_get_brief_unknown_returns_empty(self, enforcer):
        assert enforcer.get_brief("unknown") == ""


class TestVerticalCategoriesSanity:
    def test_all_verticals_have_categories(self):
        for v in ("fibre", "mobile", "tv", "securite", "famille_4lignes"):
            assert v in VERTICAL_CATEGORIES
            assert len(VERTICAL_CATEGORIES[v]) > 0

    def test_all_listed_categories_are_valid(self):
        for v, cats in VERTICAL_CATEGORIES.items():
            for c in cats:
                assert c in CATEGORIES

    def test_famille_includes_family_category(self):
        assert "family" in VERTICAL_CATEGORIES["famille_4lignes"]
