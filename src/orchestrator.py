"""
Meta Ads Auto-Pilot - Orchestrateur principal
==============================================

Le bouton qui fait TOUT, vraiment :
1. Claude génère idée + script vidéo (catégorie d'angle imposée par diversity enforcer)
2. Validation policy via Claude (Sonnet 4.6 + adaptive thinking)
3. Génération vidéo Arcads
4. Validation Meta Preview API (si elle dit non, regen avec le retour Meta)
5. Publication Meta Ads
6. Auto-budget via Thompson Sampling sur leads pondérés par qualité
7. Détection fatigue créa → pause auto
8. Détection anomalies (drop qualité, verticale vide, panic) → réponse auto
9. Kill-switch budget si dépassement plafond
10. Lock fichier + retry sur tout, pour résister aux crashes/coupures

Usage:
    python orchestrator.py --action launch       # Crée nouvelles ads
    python orchestrator.py --action optimize     # Réalloue budgets
    python orchestrator.py --action full         # Les deux (cron job)
    python orchestrator.py --action dashboard    # Snapshot + email résumé
    python orchestrator.py --action healthcheck  # Diagnostic complet (exit code)
"""

import os
import sys
import json
import time
import logging
import argparse
import anthropic
from pathlib import Path
from datetime import datetime, timedelta

from creative_generator import CreativeGenerator
from meta_publisher import MetaPublisher
from budget_optimizer import ThompsonSamplingOptimizer
from policy_validator import PolicyValidator
from performance_tracker import PerformanceTracker
from lead_quality import LeadQualityScorer
from lead_store import LeadStore
from supabase_pusher import SupabasePusher
from dashboard import DailyDashboard
from safety import BudgetGuardian, ProcessLock, BudgetGuardianTriggered, check_account_health
from resilience import DeadLetterQueue
from cost_tracker import CostTracker
from diversity import DiversityEnforcer
from anomaly_responder import AnomalyResponder
from healthcheck import run_healthcheck, HEALTH_OK


LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"orchestrator_{datetime.now():%Y%m%d}.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)


# Nombre max de tentatives de regen pour une créa (policy + meta preview)
MAX_REGEN_ATTEMPTS = 2


class MetaAdsAutoPilot:
    """Le 'bouton' qui orchestre tout le pipeline."""

    def __init__(self, config_path: str = "config/config.json"):
        config_path = Path(__file__).parent.parent / config_path
        with open(config_path) as f:
            self.config = json.load(f)

        # Client Anthropic partagé
        self.claude_client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY") or self.config["anthropic"]["api_key"]
        )

        # Infrastructure (loggers, coûts, dead letter)
        self.cost_tracker = CostTracker(self.config)
        self.dlq = DeadLetterQueue()
        self.diversity = DiversityEnforcer()
        self.lead_scorer = LeadQualityScorer()
        self.lead_store = LeadStore()
        # Push automatique vers CRM Rappelle (Supabase). Activé seulement si
        # SUPABASE_URL + SUPABASE_SERVICE_KEY + SUPABASE_ORG_ID présents en env.
        self.supabase_pusher = SupabasePusher()
        if self.supabase_pusher.is_configured():
            log.info(f"SupabasePusher activé → org {self.supabase_pusher.organization_id}")
        else:
            log.warning("SupabasePusher inactif (env manquant) — leads stockés en local seulement")

        # Modules métier
        self.creative_gen = CreativeGenerator(
            self.config,
            claude_client=self.claude_client,
            cost_tracker=self.cost_tracker,
            diversity=self.diversity,
        )
        self.publisher = MetaPublisher(self.config)
        self.optimizer = ThompsonSamplingOptimizer(self.config)
        self.validator = PolicyValidator(
            self.config,
            claude_client=self.claude_client,
            cost_tracker=self.cost_tracker,
        )
        self.tracker = PerformanceTracker(
            self.config,
            lead_scorer=self.lead_scorer,
            lead_store=self.lead_store,
            supabase_pusher=self.supabase_pusher if self.supabase_pusher.is_configured() else None,
        )
        self.dashboard = DailyDashboard(
            self.config,
            tracker=self.tracker,
            optimizer=self.optimizer,
            publisher=self.publisher,
        )

        # Safety + auto-réponse anomalies
        self.guardian = BudgetGuardian(self.config, self.publisher)
        self.responder = AnomalyResponder(
            self.config,
            tracker=self.tracker,
            optimizer=self.optimizer,
            lead_store=self.lead_store,
        )

        self.verticals = list(self.config.get("meta", {}).get("targeting", {}).keys())
        # Filtre optionnel : ne traiter qu'un sous-ensemble de verticales
        # (le ciblage des autres reste en config pour activation ultérieure).
        active = self.config.get("active_verticals")
        if active:
            self.verticals = [v for v in self.verticals if v in active]

    # ----- Pre-flight safety -----

    def _preflight(self) -> bool:
        """Vérifs avant toute action. Retourne False si on doit s'abstenir."""
        # 1. Kill-switch déjà déclenché ?
        self.guardian.reset_if_new_day()
        if self.guardian.is_killed():
            log.critical("Kill-switch budget actif. Abort.")
            return False

        # 2. Compte Meta sain ?
        ok, msg = check_account_health(self.publisher)
        if not ok:
            log.critical(f"Compte Meta KO ({msg}). Abort.")
            return False
        return True

    # ----- LAUNCH -----

    def launch_new_ads(self):
        log.info("=" * 60)
        log.info("LAUNCH : Création de nouvelles ads")
        log.info("=" * 60)

        if not self._preflight():
            return

        # Plafond coût création (Anthropic + Arcads) — anti-burn
        capped, cost_totals = self.cost_tracker.is_creation_capped()
        if capped:
            log.warning(
                f"Plafond coût création atteint pour aujourd'hui : "
                f"{cost_totals['creation_total_usd']:.2f}$ / cap {self.cost_tracker.daily_cap_creation_usd:.2f}$. "
                f"Skip launch."
            )
            return

        # Verticales prioritaires (anomalie remontée → on les traite d'abord).
        # IMPORTANT : on ré-applique le filtre active_verticals. Sinon une
        # priorité stale ou un emergency_relaunch_unlock (qui marque TOUTES
        # les verticales configurées) ferait sortir le launch du périmètre
        # pilote et brûlerait du budget Creatify sur des verticales exclues.
        priority = [v for v in self.responder.get_priority_verticals()
                    if v in self.verticals]
        ordered_verticals = priority + [v for v in self.verticals if v not in priority]
        if priority:
            log.info(f"Verticales prioritaires (anomalie) : {priority}")

        for vertical in ordered_verticals:
            n_creatives = self.optimizer.how_many_to_explore(vertical)
            if n_creatives == 0:
                log.info(f"[{vertical}] Pas besoin de nouvelles ads, exploitation en cours")
                continue

            log.info(f"[{vertical}] Génération de {n_creatives} nouvelles créas")
            winning_angles = self.tracker.get_winning_angles(vertical, last_n_days=14)

            for i in range(n_creatives):
                try:
                    self._create_and_publish_one(vertical, winning_angles)
                except Exception as e:
                    log.error(f"[{vertical}] Échec créa {i}: {e}", exc_info=True)
                    self.dlq.add(
                        operation="launch_creative",
                        payload={"vertical": vertical, "winning_angles": winning_angles},
                        error=str(e),
                        vertical=vertical,
                    )

        # Clear les priorités traitées
        if priority:
            self.responder.clear_priorities()

    def _create_and_publish_one(self, vertical: str, winning_angles: list):
        """Pipeline complet pour une créa. Self-healing sur rejets."""

        # 1. Diversity enforcer pick la catégorie LRU
        category = self.diversity.pick_next_category(vertical)
        log.info(f"[{vertical}] Catégorie imposée : {category}")

        # 2. Claude génère le concept (avec contrainte catégorie)
        creative_brief = self.creative_gen.generate_concept(
            vertical=vertical,
            past_winners=winning_angles,
            category=category,
        )

        # 3. Validation Claude policy (avec self-healing si rejet)
        for attempt in range(MAX_REGEN_ATTEMPTS):
            is_valid, issues = self.validator.validate(creative_brief)
            if is_valid:
                break
            log.warning(f"[{vertical}] Concept rejeté policy ({attempt+1}/{MAX_REGEN_ATTEMPTS}) : {issues}")
            if attempt < MAX_REGEN_ATTEMPTS - 1:
                creative_brief = self.creative_gen.regenerate_safe(creative_brief, issues, source="policy")
            else:
                raise RuntimeError(f"[{vertical}] Concept rejeté après {MAX_REGEN_ATTEMPTS} tentatives")

        # 4. Génération vidéo Arcads
        log.info(f"[{vertical}] Génération vidéo ({self.creative_gen.video_provider})...")
        video_url = self.creative_gen.generate_video(creative_brief)

        # 5. Upload + create_creative pour preview Meta
        log.info(f"[{vertical}] Upload vidéo + creative pour preview...")
        video_id = self.publisher.upload_video(video_url)
        creative_id = self.publisher.create_ad_creative(
            video_id,
            primary_text=creative_brief["primary_text"],
            headline=creative_brief["headline"],
            description=creative_brief["description"],
        )

        # 6. Meta Preview API (self-healing si Meta rejette)
        preview = self.publisher.preview_creative(creative_id)
        if not preview["ok"]:
            log.warning(f"[{vertical}] Meta Preview rejet : {preview['body'][:200]}")
            # Re-génère avec le feedback Meta direct
            creative_brief = self.creative_gen.regenerate_safe(
                creative_brief,
                issues=[f"Meta a rejeté l'aperçu : {preview['body'][:300]}"],
                source="meta_preview",
            )
            # On re-génère la vidéo Arcads (la créa a changé)
            video_url = self.creative_gen.generate_video(creative_brief)
            video_id = self.publisher.upload_video(video_url)
            creative_id = self.publisher.create_ad_creative(
                video_id,
                primary_text=creative_brief["primary_text"],
                headline=creative_brief["headline"],
                description=creative_brief["description"],
            )
            preview = self.publisher.preview_creative(creative_id)
            if not preview["ok"]:
                raise RuntimeError(
                    f"[{vertical}] Meta Preview rejeté 2 fois. Skip cette créa. Dernier message: {preview['body'][:200]}"
                )

        # 7. Création campagne (cached) + adset + ad
        log.info(f"[{vertical}] Publication finale...")
        campaign_id = self.publisher._get_or_create_campaign(vertical)
        starting_budget = self.optimizer.get_initial_budget(vertical)
        adset_id = self.publisher._create_adset(
            campaign_id=campaign_id,
            vertical=vertical,
            daily_budget=starting_budget,
        )
        ad_id = self.publisher._create_ad(adset_id, creative_id, vertical)
        ad_data = {
            "campaign_id": campaign_id,
            "adset_id": adset_id,
            "ad_id": ad_id,
            "creative_id": creative_id,
            "video_id": video_id,
        }

        # 8. Enregistre tracker + bandit + diversity
        self.tracker.register_ad(
            ad_id=ad_data["ad_id"],
            vertical=vertical,
            angle=creative_brief["angle"],
            initial_budget=starting_budget,
        )
        self.optimizer.register_ad(
            ad_id=ad_data["ad_id"],
            vertical=vertical,
            initial_budget=starting_budget,
        )
        self.diversity.record_launch(
            vertical=vertical,
            category=category,
            ad_id=ad_data["ad_id"],
            angle=creative_brief["angle"],
        )

        log.info(f"[{vertical}] ✓ Ad publiée : {ad_data['ad_id']} (catégorie {category})")

    # ----- OPTIMIZE -----

    def optimize_budgets(self):
        log.info("=" * 60)
        log.info("OPTIMIZE : Réallocation budgets + safety + anomalies")
        log.info("=" * 60)

        if not self._preflight():
            return

        # 1. KILL-SWITCH BUDGET en premier
        if not self.guardian.check_and_enforce():
            log.critical("Kill-switch déclenché. Aucune autre action prise dans ce cycle.")
            return

        # 2. Pull insights + leads pour scoring qualité
        log.info("Récupération des insights Meta + leads bruts...")
        try:
            active_ads = self.publisher.get_active_ads()
        except Exception as e:
            log.error(f"Impossible de lister les ads actives : {e}")
            self.dlq.add(operation="get_active_ads", payload={}, error=str(e))
            return

        for ad in active_ads:
            try:
                insights = self.publisher.get_ad_insights(ad["id"], last_hours=24)
                leads_data = self.publisher.get_ad_leads(ad["id"], last_hours=24)
                self.tracker.update_performance(ad["id"], insights, leads_data=leads_data)
                qwl = self.tracker.get_quality_weighted_leads(ad["id"])
                self.optimizer.update_from_insights(ad["id"], insights, quality_weighted_leads=qwl)
            except Exception as e:
                log.warning(f"Skip insights {ad['id']}: {e}")
                self.dlq.add(operation="get_insights", payload={"ad_id": ad["id"]}, error=str(e))

        # 3. Détection anomalies + auto-réponse (avant les décisions de réallocation)
        anomaly_report = self.responder.detect_and_respond()
        if anomaly_report["detected"]:
            log.warning(
                f"Anomalies détectées : {[a['type'] for a in anomaly_report['detected']]}. "
                f"Actions : {[a['action'] for a in anomaly_report['actions_taken']]}"
            )

        # 4. Décisions par verticale, en injectant le fatigue provider
        for vertical in self.verticals:
            decisions = self.optimizer.decide_allocations(
                vertical,
                fatigue_provider=self.tracker.get_fatigue,
            )
            for ad_id, decision in decisions.items():
                self._apply_decision(ad_id, decision)

    def _apply_decision(self, ad_id: str, decision: dict):
        action = decision["action"]
        cpl = decision.get("cpl_qualifie", float("inf"))
        cpl_str = f"{cpl:.2f}€" if cpl != float("inf") else "n/a"

        try:
            if action == "scale":
                new_budget = decision["new_budget"]
                log.info(f"  ↑ {ad_id}: scale à {new_budget}€/j (CPL qualifié: {cpl_str})")
                self.publisher.update_ad_budget(ad_id, new_budget)
            elif action == "reduce":
                new_budget = decision["new_budget"]
                log.info(f"  ↓ {ad_id}: réduit à {new_budget}€/j (CPL qualifié: {cpl_str})")
                self.publisher.update_ad_budget(ad_id, new_budget)
            elif action == "pause":
                log.info(f"  ✗ {ad_id}: pause — {decision.get('reason', 'unknown')}")
                self.publisher.pause_ad(ad_id)
                # Sync local state
                if ad_id in self.optimizer.state.get("ads", {}):
                    self.optimizer.state["ads"][ad_id]["status"] = "paused"
                    self.optimizer._save_state()
            else:
                log.info(f"  = {ad_id}: hold (en apprentissage)")
        except Exception as e:
            log.error(f"Décision {action} sur {ad_id} a échoué : {e}")
            self.dlq.add(
                operation=f"apply_{action}",
                payload={"ad_id": ad_id, "decision": decision},
                error=str(e),
            )

    # ----- DASHBOARD -----

    def send_dashboard(self):
        log.info("=" * 60)
        log.info("DASHBOARD : Snapshot + envoi par email (si SMTP configuré)")
        log.info("=" * 60)
        payload = self.dashboard.build()
        snapshot_path = self.dashboard.save_snapshot(payload)
        log.info(f"Snapshot stocké : {snapshot_path}")
        sent = self.dashboard.send(payload)
        if not sent:
            log.info("Email non envoyé (SMTP non configuré ou erreur). Snapshot disponible localement.")

    # ----- HEALTHCHECK -----

    def healthcheck(self) -> int:
        log.info("=" * 60)
        log.info("HEALTHCHECK")
        log.info("=" * 60)
        exit_code, report = run_healthcheck(
            self.config,
            self.claude_client,
            self.publisher,
            self.optimizer,
        )
        log.info(f"Status: {report['status']}")
        for k, v in report.get("checks", {}).items():
            log.info(f"  {k}: {v}")
        for w in report.get("warnings", []):
            log.warning(f"  WARN: {w}")
        for e in report.get("errors", []):
            log.error(f"  ERR : {e}")

        # Snapshot du report pour audit
        report_path = Path(__file__).parent.parent / "data" / "healthcheck_last.json"
        report_path.parent.mkdir(exist_ok=True)
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)

        return exit_code

    # ----- FULL CYCLE -----

    def run_full_cycle(self):
        log.info("\n" + "█" * 60)
        log.info(f"FULL CYCLE — {datetime.now():%Y-%m-%d %H:%M:%S}")
        log.info("█" * 60)
        try:
            self.optimize_budgets()
        except Exception as e:
            log.error(f"Optimize failed: {e}", exc_info=True)
        try:
            self.launch_new_ads()
        except Exception as e:
            log.error(f"Launch failed: {e}", exc_info=True)
        log.info("Cycle terminé\n")


def main():
    # Charge le .env à la racine du projet (cron-friendly : pas besoin
    # d'exporter les variables dans le shell). Import paresseux ici (et pas
    # au niveau module) pour ne pas polluer/contraindre l'environnement des tests.
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent / ".env")
    except ImportError:
        log.warning("python-dotenv non installé — le .env ne sera pas chargé "
                    "(les variables doivent être dans l'environnement shell).")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--action",
        choices=["launch", "optimize", "full", "dashboard", "healthcheck",
                 "activate"],
        default="full",
        help="launch=nouvelles ads, optimize=budgets, full=les deux, "
             "dashboard=snapshot+email, healthcheck=diagnostic (exit code), "
             "activate=passe les ads AUTOPILOT en pause → ACTIVE (post-validation)"
    )
    args = parser.parse_args()

    pilot = MetaAdsAutoPilot()

    # Healthcheck ne prend pas le lock (lecture seule)
    if args.action == "healthcheck":
        sys.exit(pilot.healthcheck())

    # Lock per-action pour éviter les chevauchements (un launch et un optimize
    # peuvent tourner en parallèle, mais pas deux launches concurrents)
    with ProcessLock(name=args.action):
        if args.action == "launch":
            pilot.launch_new_ads()
        elif args.action == "optimize":
            pilot.optimize_budgets()
        elif args.action == "dashboard":
            pilot.send_dashboard()
        elif args.action == "activate":
            res = pilot.publisher.activate_autopilot()
            log.info(
                f"Activé : {res['adsets_activated']} ad set(s), "
                f"{res['ads_activated']} ad(s). Le budget commence maintenant."
            )
        else:
            pilot.run_full_cycle()


if __name__ == "__main__":
    main()
