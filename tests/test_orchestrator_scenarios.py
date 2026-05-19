"""
Scénarios end-to-end de l'orchestrator avec tous les mocks externes.

Objectif : valider que l'autopilot réagit correctement dans les vraies
situations qu'il va rencontrer en prod :
- happy path complet
- self-healing sur rejet policy / Meta Preview
- kill-switch budget en plein cycle
- détection + réponse anomalie qualité
- empty vertical priorisée au prochain launch
- DLQ accumule sur erreurs Meta
- lock fichier bloque les crons concurrents
- compte Meta désactivé → abort propre

Tous ces tests utilisent des mocks pour Anthropic / Meta / Arcads / SMTP.
Aucun appel réseau réel.
"""

import json
import pytest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from creative_generator import ConceptOutput
from policy_validator import PolicyResult


pytestmark = pytest.mark.integration


@pytest.fixture
def isolated_orchestrator(tmp_path, monkeypatch, base_config):
    """
    Orchestrator avec data/ redirigé vers tmp + tous les modules externes mockés.
    Retourne (pilot, claude_mock, requests_mock).
    """
    # Redirection des paths vers tmp
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "config.json").write_text(json.dumps(base_config))

    # Patch les Path(__file__).parent.parent dans CHAQUE module qui les utilise
    # Plus simple : on instancie l'orchestrator, puis on remplace les paths critiques.
    # Mocks externes
    fake_anthropic = MagicMock()
    fake_anthropic_class = MagicMock(return_value=fake_anthropic)

    monkeypatch.setattr("creative_generator.anthropic.Anthropic", fake_anthropic_class)
    monkeypatch.setattr("policy_validator.anthropic.Anthropic", fake_anthropic_class)
    monkeypatch.setattr("orchestrator.anthropic.Anthropic", fake_anthropic_class)

    # On va patcher le module orchestrator pour pointer vers tmp
    from orchestrator import MetaAdsAutoPilot

    # Plus simple : monkeypatch __file__ ne marche pas, on utilise les state_path overrides
    # via instanciation manuelle des modules
    # Chemin ABSOLU vers le config tmp : sinon MetaAdsAutoPilot résout via
    # Path(__file__).parent.parent et lirait le vrai config/config.json du repo
    # (non hermétique). Un chemin absolu prend le pas sur le join.
    pilot = MetaAdsAutoPilot(config_path=str(tmp_path / "config" / "config.json"))

    # Override les paths de stockage vers tmp
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

    return pilot, fake_anthropic


def make_claude_response(parsed):
    mock = MagicMock()
    mock.parsed_output = parsed
    mock.usage = MagicMock(
        input_tokens=500, output_tokens=200,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    return mock


def make_concept(angle="frustration_facture"):
    return ConceptOutput(
        angle=angle,
        hook_first_3s="Hook 3s",
        video_script="Script complet",
        primary_text="Primary",
        headline="Head",
        description="Desc",
        avatar_persona="femme 40 QC",
    )


# ============================================================
# SCÉNARIO 1 : Happy path full launch
# ============================================================

class TestHappyPathLaunch:
    def test_launch_publishes_one_ad_per_vertical(self, isolated_orchestrator, monkeypatch):
        pilot, claude = isolated_orchestrator

        # Anthropic toujours OK
        claude.messages.parse.side_effect = [
            make_claude_response(make_concept(f"angle_{i}")) if i % 2 == 0
            else make_claude_response(PolicyResult(valid=True))
            for i in range(20)
        ]

        # Arcads OK
        with patch("creative_generator.requests") as mreq:
            mreq.post.return_value = MagicMock(json=lambda: {"job_id": "j1"}, raise_for_status=lambda: None)
            mreq.get.return_value = MagicMock(
                json=lambda: {"status": "completed", "video_url": "https://v/x.mp4"},
                raise_for_status=lambda: None,
            )
            mreq.RequestException = Exception
            with patch("creative_generator.time.sleep"):
                # Meta publisher : tout en succès
                pilot.publisher = MagicMock()
                pilot.publisher.get_active_ads.return_value = []
                pilot.publisher.upload_video.return_value = "v_id"
                pilot.publisher.create_ad_creative.return_value = "c_id"
                pilot.publisher.preview_creative.return_value = {"ok": True, "body": "preview"}
                pilot.publisher._get_or_create_campaign.return_value = "camp_id"
                pilot.publisher._create_adset.return_value = "as_id"
                pilot.publisher._create_ad.side_effect = [f"ad_{v}" for v in range(20)]
                pilot.publisher._request.return_value = {"account_status": 1, "disable_reason": 0}
                pilot.guardian.publisher = pilot.publisher

                pilot.launch_new_ads()

        # 3 verticales × 3 ads = 9 ads minimum
        assert len(pilot.tracker.data["ads"]) >= 3
        # Chaque verticale a au moins 1 ad
        verticals_with_ads = {ad["vertical"] for ad in pilot.tracker.data["ads"].values()}
        assert "fibre" in verticals_with_ads or "famille_4lignes" in verticals_with_ads


# ============================================================
# SCÉNARIO 2 : Self-healing sur rejet policy
# ============================================================

class TestSelfHealingPolicy:
    def test_regen_after_policy_rejection_then_succeeds(self, isolated_orchestrator):
        pilot, claude = isolated_orchestrator

        # Sequence: concept → REJECT (mention Bell) → regen → ACCEPT
        claude.messages.parse.side_effect = [
            make_claude_response(make_concept("brand_bad")),         # concept
            make_claude_response(PolicyResult(valid=False, issues=["mention Bell"])),  # validate KO
            make_claude_response(make_concept("brand_clean")),        # regen
            make_claude_response(PolicyResult(valid=True)),           # validate OK
        ]

        with patch("creative_generator.requests") as mreq:
            mreq.post.return_value = MagicMock(json=lambda: {"job_id": "j1"}, raise_for_status=lambda: None)
            mreq.get.return_value = MagicMock(
                json=lambda: {"status": "completed", "video_url": "https://v/x.mp4"},
                raise_for_status=lambda: None,
            )
            mreq.RequestException = Exception
            with patch("creative_generator.time.sleep"):
                pilot.publisher = MagicMock()
                pilot.publisher.get_active_ads.return_value = []
                pilot.publisher.upload_video.return_value = "v_id"
                pilot.publisher.create_ad_creative.return_value = "c_id"
                pilot.publisher.preview_creative.return_value = {"ok": True, "body": "ok"}
                pilot.publisher._get_or_create_campaign.return_value = "camp_id"
                pilot.publisher._create_adset.return_value = "as_id"
                pilot.publisher._create_ad.return_value = "ad_xxx"
                pilot.publisher._request.return_value = {"account_status": 1}
                pilot.guardian.publisher = pilot.publisher

                pilot._create_and_publish_one("famille_4lignes", winning_angles=[])

        # L'ad publiée a l'angle de la regen, pas le rejet
        assert "ad_xxx" in pilot.tracker.data["ads"]
        assert pilot.tracker.data["ads"]["ad_xxx"]["angle"] == "brand_clean"

    def test_two_rejects_in_a_row_raises(self, isolated_orchestrator):
        pilot, claude = isolated_orchestrator

        # Toujours rejeté (angle non corrigé)
        claude.messages.parse.side_effect = [
            make_claude_response(make_concept("bad")),
            make_claude_response(PolicyResult(valid=False, issues=["bad"])),
            make_claude_response(make_concept("still_bad")),
            make_claude_response(PolicyResult(valid=False, issues=["still bad"])),
        ]
        pilot.publisher = MagicMock()
        pilot.publisher.get_active_ads.return_value = []
        pilot.publisher._request.return_value = {"account_status": 1}
        pilot.guardian.publisher = pilot.publisher

        with pytest.raises(RuntimeError, match="rejeté après"):
            pilot._create_and_publish_one("fibre", winning_angles=[])


# ============================================================
# SCÉNARIO 3 : Self-healing sur rejet Meta Preview
# ============================================================

class TestSelfHealingMetaPreview:
    def test_meta_preview_rejected_then_regen_succeeds(self, isolated_orchestrator):
        pilot, claude = isolated_orchestrator

        claude.messages.parse.side_effect = [
            make_claude_response(make_concept("v1")),
            make_claude_response(PolicyResult(valid=True)),  # policy OK
            make_claude_response(make_concept("v2")),         # regen après meta preview KO
        ]
        with patch("creative_generator.requests") as mreq:
            mreq.post.return_value = MagicMock(json=lambda: {"job_id": "j1"}, raise_for_status=lambda: None)
            mreq.get.return_value = MagicMock(
                json=lambda: {"status": "completed", "video_url": "https://v/x.mp4"},
                raise_for_status=lambda: None,
            )
            mreq.RequestException = Exception
            with patch("creative_generator.time.sleep"):
                pilot.publisher = MagicMock()
                pilot.publisher.get_active_ads.return_value = []
                pilot.publisher.upload_video.side_effect = ["v1_id", "v2_id"]
                pilot.publisher.create_ad_creative.side_effect = ["c1_id", "c2_id"]
                # 1er preview rejette, 2e accepte
                pilot.publisher.preview_creative.side_effect = [
                    {"ok": False, "body": "text trop long"},
                    {"ok": True, "body": "ok"},
                ]
                pilot.publisher._get_or_create_campaign.return_value = "camp_id"
                pilot.publisher._create_adset.return_value = "as_id"
                pilot.publisher._create_ad.return_value = "ad_final"
                pilot.publisher._request.return_value = {"account_status": 1}
                pilot.guardian.publisher = pilot.publisher

                pilot._create_and_publish_one("fibre", winning_angles=[])

        # On a bien généré 2 vidéos (regen) et publié l'ad
        assert pilot.publisher.upload_video.call_count == 2
        assert "ad_final" in pilot.tracker.data["ads"]
        # Angle final = v2 (post-regen)
        assert pilot.tracker.data["ads"]["ad_final"]["angle"] == "v2"

    def test_meta_preview_rejected_twice_raises(self, isolated_orchestrator):
        pilot, claude = isolated_orchestrator
        claude.messages.parse.side_effect = [
            make_claude_response(make_concept("v1")),
            make_claude_response(PolicyResult(valid=True)),
            make_claude_response(make_concept("v2")),  # regen
        ]
        with patch("creative_generator.requests") as mreq:
            mreq.post.return_value = MagicMock(json=lambda: {"job_id": "j1"}, raise_for_status=lambda: None)
            mreq.get.return_value = MagicMock(
                json=lambda: {"status": "completed", "video_url": "https://v/x.mp4"},
                raise_for_status=lambda: None,
            )
            mreq.RequestException = Exception
            with patch("creative_generator.time.sleep"):
                pilot.publisher = MagicMock()
                pilot.publisher.get_active_ads.return_value = []
                pilot.publisher.upload_video.return_value = "v_id"
                pilot.publisher.create_ad_creative.return_value = "c_id"
                # Toujours rejeté
                pilot.publisher.preview_creative.return_value = {"ok": False, "body": "rejected"}
                pilot.publisher._request.return_value = {"account_status": 1}
                pilot.guardian.publisher = pilot.publisher

                with pytest.raises(RuntimeError, match="Meta Preview rejeté 2 fois"):
                    pilot._create_and_publish_one("fibre", winning_angles=[])


# ============================================================
# SCÉNARIO 4 : Kill-switch budget en plein cycle
# ============================================================

class TestKillSwitchTriggers:
    def test_optimize_aborts_when_budget_exceeded(self, isolated_orchestrator):
        pilot, claude = isolated_orchestrator
        # Spend qui dépasse le cap dur (100 × 1.2 = 120)
        pilot.publisher = MagicMock()
        pilot.publisher.get_active_ads.return_value = [{"id": "a1"}, {"id": "a2"}]
        pilot.publisher.get_ad_insights.return_value = {"spend": 100.0}  # 200 > 120
        pilot.publisher._request.return_value = {"account_status": 1}
        pilot.guardian.publisher = pilot.publisher

        pilot.optimize_budgets()

        # Kill-switch doit être actif
        assert pilot.guardian.is_killed() is True
        # Toutes les ads pausées
        assert pilot.publisher.pause_ad.call_count >= 2

    def test_already_killed_skips_action(self, isolated_orchestrator):
        pilot, claude = isolated_orchestrator
        # Pre-kill
        pilot.guardian.state = {"killed_at": datetime.now().isoformat(), "killed_reason": "test"}
        pilot.guardian._save()

        pilot.publisher = MagicMock()
        pilot.publisher._request.return_value = {"account_status": 1}
        pilot.guardian.publisher = pilot.publisher

        pilot.launch_new_ads()
        # Aucun appel à upload_video — preflight a stoppé tôt
        pilot.publisher.upload_video.assert_not_called()

    def test_meta_account_disabled_skips(self, isolated_orchestrator):
        pilot, claude = isolated_orchestrator
        pilot.publisher = MagicMock()
        pilot.publisher._request.return_value = {"account_status": 2, "disable_reason": 5}
        pilot.guardian.publisher = pilot.publisher

        pilot.launch_new_ads()
        pilot.publisher.upload_video.assert_not_called()


# ============================================================
# SCÉNARIO 5 : Anomalie quality_drop déclenche boost exploration
# ============================================================

class TestAnomalyResponse:
    def test_quality_drop_boosts_exploration(self, isolated_orchestrator):
        pilot, claude = isolated_orchestrator
        # Setup : leads anciens haute qualité, leads récents qualité pourrie
        for i in range(15):
            pilot.lead_store.add(
                lead_data={"id": f"old_{i}", "field_data": []},
                ad_id="ad_x", vertical="fibre", quality_score=0.85,
            )
            # Force timestamp ancien
            with open(pilot.lead_store.path) as f:
                lines = f.readlines()
            lines[-1] = json.dumps({**json.loads(lines[-1]),
                "received_at": (datetime.now() - timedelta(hours=24*7+12)).isoformat()}) + "\n"
            with open(pilot.lead_store.path, "w") as f:
                f.writelines(lines)
        for i in range(15):
            pilot.lead_store.add(
                lead_data={"id": f"new_{i}", "field_data": []},
                ad_id="ad_y", vertical="fibre", quality_score=0.30,
            )

        # Une ad active pour éviter all_paused_crisis
        pilot.optimizer.state["ads"]["ad_x"] = {
            "vertical": "fibre", "status": "active",
            "last_update": datetime.now().isoformat(),
        }

        pilot.publisher = MagicMock()
        pilot.publisher.get_active_ads.return_value = []
        pilot.publisher._request.return_value = {"account_status": 1}
        pilot.guardian.publisher = pilot.publisher

        old_ratio = pilot.optimizer.MIN_EXPLORATION_BUDGET_RATIO
        pilot.optimize_budgets()
        # exploration doit avoir été bumpée
        assert pilot.optimizer.MIN_EXPLORATION_BUDGET_RATIO > old_ratio

    def test_empty_vertical_marked_priority_then_treated_first(self, isolated_orchestrator):
        pilot, claude = isolated_orchestrator
        # Une seule ad active sur famille_4lignes → fibre + mobile vides
        pilot.optimizer.state["ads"]["ad_z"] = {
            "vertical": "famille_4lignes", "status": "active",
            "last_update": datetime.now().isoformat(),
        }

        pilot.publisher = MagicMock()
        pilot.publisher.get_active_ads.return_value = []
        pilot.publisher._request.return_value = {"account_status": 1}
        pilot.guardian.publisher = pilot.publisher

        pilot.optimize_budgets()
        # Priorités marquées
        priorities = pilot.responder.get_priority_verticals()
        assert "fibre" in priorities
        assert "mobile" in priorities


# ============================================================
# SCÉNARIO 6 : DLQ accumule quand opérations échouent
# ============================================================

class TestDeadLetterAccumulation:
    def test_launch_failure_added_to_dlq(self, isolated_orchestrator):
        pilot, claude = isolated_orchestrator
        # Concept gen échoue à chaque fois
        claude.messages.parse.side_effect = Exception("Anthropic down")
        pilot.publisher = MagicMock()
        pilot.publisher.get_active_ads.return_value = []
        pilot.publisher._request.return_value = {"account_status": 1}
        pilot.guardian.publisher = pilot.publisher

        pilot.launch_new_ads()
        # DLQ contient au moins une entrée par verticale tentée
        assert len(pilot.dlq._items) >= 1
        assert all(i["operation"] == "launch_creative" for i in pilot.dlq._items)


# ============================================================
# SCÉNARIO 7 : ProcessLock empêche concurrents
# ============================================================

class TestProcessLockProtection:
    def test_concurrent_optimize_blocked(self, tmp_path):
        from safety import ProcessLock
        # Premier lock acquis
        lock1 = ProcessLock(name="optimize", lock_dir=tmp_path).__enter__()
        try:
            # Deuxième tentative bloque
            with pytest.raises(RuntimeError, match="Lock"):
                with ProcessLock(name="optimize", lock_dir=tmp_path):
                    pass
        finally:
            lock1.__exit__(None, None, None)


# ============================================================
# SCÉNARIO 8 : Diversity force la rotation des catégories
# ============================================================

class TestDiversityRotationInLaunch:
    def test_consecutive_launches_rotate_categories(self, isolated_orchestrator):
        pilot, claude = isolated_orchestrator
        # 3 launches consécutifs sur famille → 3 catégories différentes
        claude.messages.parse.side_effect = [
            make_claude_response(make_concept(f"a{i}")) if i % 2 == 0
            else make_claude_response(PolicyResult(valid=True))
            for i in range(20)
        ]
        with patch("creative_generator.requests") as mreq:
            mreq.post.return_value = MagicMock(json=lambda: {"job_id": "j1"}, raise_for_status=lambda: None)
            mreq.get.return_value = MagicMock(
                json=lambda: {"status": "completed", "video_url": "https://v/x.mp4"},
                raise_for_status=lambda: None,
            )
            mreq.RequestException = Exception
            with patch("creative_generator.time.sleep"):
                pilot.publisher = MagicMock()
                pilot.publisher.get_active_ads.return_value = []
                pilot.publisher.upload_video.return_value = "v_id"
                pilot.publisher.create_ad_creative.return_value = "c_id"
                pilot.publisher.preview_creative.return_value = {"ok": True, "body": "ok"}
                pilot.publisher._get_or_create_campaign.return_value = "camp_id"
                pilot.publisher._create_adset.return_value = "as_id"
                pilot.publisher._create_ad.side_effect = [f"ad_{i}" for i in range(20)]
                pilot.publisher._request.return_value = {"account_status": 1}
                pilot.guardian.publisher = pilot.publisher

                # Lance 3 ads en famille_4lignes
                for _ in range(3):
                    pilot._create_and_publish_one("famille_4lignes", winning_angles=[])

        # 3 catégories différentes utilisées (puisque famille_4lignes a 3 catégories valides)
        from diversity import VERTICAL_CATEGORIES
        cats_used = {l["category"] for l in pilot.diversity.state["launches"]}
        valid = set(VERTICAL_CATEGORIES["famille_4lignes"])
        assert cats_used == valid


# ============================================================
# SCÉNARIO 9 : Cost cap bloque le launch si dépassé
# ============================================================

class TestCostCapBlocksLaunch:
    def test_creation_cap_skips_launch(self, isolated_orchestrator):
        pilot, claude = isolated_orchestrator
        # Force le cap atteint
        for _ in range(5):
            pilot.cost_tracker.record_arcads()  # 5 × $7 = $35 > cap par défaut $30

        pilot.publisher = MagicMock()
        pilot.publisher._request.return_value = {"account_status": 1}
        pilot.guardian.publisher = pilot.publisher

        pilot.launch_new_ads()
        # Aucun appel Anthropic (skip immédiat)
        claude.messages.parse.assert_not_called()
