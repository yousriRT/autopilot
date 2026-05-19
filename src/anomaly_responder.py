"""
Anomaly Responder
=================

Détecte les anomalies de comportement du système ET réagit AUTOMATIQUEMENT
sans intervention humaine. C'est le bras "self-healing" de l'autopilot.

3 anomalies couvertes :
- Quality drop : ratio leads qualifiés / leads bruts chute brutalement
- Vertical empty : une verticale n'a plus aucune ad active
- All-paused crisis : pas une seule ad active sur tout le système (cron pause-storm)

Pour chacune, il y a une action correctrice automatique :
- Quality drop → augmente le ratio d'exploration (force plus d'ads neuves)
- Vertical empty → marque la verticale comme prioritaire pour le prochain launch
- All-paused → débloque le launch en mode urgence + reset des seuils MIN_*

Toutes les décisions sont loggées dans data/anomaly_log.jsonl pour audit.
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)


# Seuils
QUALITY_DROP_THRESHOLD = 0.20       # baisse > 20% en 24h vs 7j precedents = anomalie
QUALITY_MIN_LEADS_FOR_SIGNAL = 10   # bruit en dessous
ALL_PAUSED_GRACE_HOURS = 6          # tolérance avant de crier au feu


class AnomalyResponder:
    def __init__(
        self,
        config: dict,
        tracker,
        optimizer,
        lead_store=None,
        log_path: Optional[Path] = None,
    ):
        self.config = config
        self.tracker = tracker
        self.optimizer = optimizer
        self.lead_store = lead_store
        self.log_path = Path(log_path) if log_path else (Path(__file__).parent.parent / "data" / "anomaly_log.jsonl")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.priority_verticals_path = self.log_path.parent / "priority_verticals.json"

    def detect_and_respond(self) -> dict:
        """Méthode principale appelée à chaque cycle optimize. Retourne un report."""
        report = {"detected": [], "actions_taken": []}

        # 1. Detect & respond
        for fn in (self._check_quality_drop, self._check_empty_verticals, self._check_all_paused_crisis):
            try:
                anomaly = fn()
                if anomaly:
                    report["detected"].append(anomaly)
                    actions = self._respond(anomaly)
                    report["actions_taken"].extend(actions)
                    self._log({"anomaly": anomaly, "actions": actions, "ts": datetime.now().isoformat()})
            except Exception as e:
                log.error(f"AnomalyResponder.{fn.__name__} échec: {e}", exc_info=True)

        return report

    # ----- Detection -----

    def _check_quality_drop(self) -> Optional[dict]:
        """
        Compare quality moyenne des leads des 24 dernières heures vs ceux
        de la semaine d'avant. Source de vérité : lead_store (timestamps réels).
        """
        if self.lead_store is None:
            return None

        recent = self.lead_store.list_recent(hours=24)
        # Semaine d'avant : entre -7j et -8j
        from datetime import datetime, timedelta
        week_ago_start = datetime.now() - timedelta(hours=24 * 8)
        week_ago_end = datetime.now() - timedelta(hours=24 * 7)
        previous = []
        for lead in self.lead_store.iter_leads():
            try:
                ts = datetime.fromisoformat(lead["received_at"])
                if week_ago_start <= ts <= week_ago_end:
                    previous.append(lead)
            except (ValueError, KeyError):
                continue

        if len(recent) < QUALITY_MIN_LEADS_FOR_SIGNAL or len(previous) < QUALITY_MIN_LEADS_FOR_SIGNAL:
            return None

        recent_avg = sum(float(l.get("quality_score", 0)) for l in recent) / len(recent)
        previous_avg = sum(float(l.get("quality_score", 0)) for l in previous) / len(previous)
        if previous_avg == 0:
            return None

        drop = (previous_avg - recent_avg) / previous_avg
        if drop >= QUALITY_DROP_THRESHOLD:
            return {
                "type": "quality_drop",
                "drop_pct": round(drop * 100, 1),
                "recent_avg_quality": round(recent_avg, 3),
                "previous_avg_quality": round(previous_avg, 3),
                "recent_n": len(recent),
                "previous_n": len(previous),
            }
        return None

    def _check_empty_verticals(self) -> Optional[dict]:
        """Y a-t-il des verticales configurées sans aucune ad active ?"""
        configured = set(self.config.get("meta", {}).get("targeting", {}).keys())
        active = set()
        for ad in self.optimizer.state.get("ads", {}).values():
            if ad.get("status") == "active" and ad.get("vertical"):
                active.add(ad["vertical"])
        empty = configured - active
        if empty:
            return {
                "type": "empty_verticals",
                "verticals": sorted(empty),
            }
        return None

    def _check_all_paused_crisis(self) -> Optional[dict]:
        """0 ads actives partout = panique."""
        active_count = sum(
            1 for ad in self.optimizer.state.get("ads", {}).values()
            if ad.get("status") == "active"
        )
        if active_count > 0:
            return None

        # Combien de temps qu'on est à zéro ? Fonction du dernier "active"
        last_active_ts = None
        for ad in self.optimizer.state.get("ads", {}).values():
            ts = ad.get("last_update")
            if ts and (last_active_ts is None or ts > last_active_ts):
                last_active_ts = ts

        if last_active_ts:
            try:
                last_dt = datetime.fromisoformat(last_active_ts)
                age_h = (datetime.now() - last_dt).total_seconds() / 3600
                if age_h < ALL_PAUSED_GRACE_HOURS:
                    return None  # peut être normal en plein démarrage
            except (ValueError, TypeError):
                pass

        return {
            "type": "all_paused_crisis",
            "active_count": 0,
            "last_activity_ts": last_active_ts,
        }

    # ----- Response actions -----

    def _respond(self, anomaly: dict) -> list:
        actions = []
        atype = anomaly["type"]

        if atype == "quality_drop":
            # Augmente l'exploration : on baisse le seuil "min mature ads" pour
            # forcer plus de nouvelles ads dès le prochain launch.
            old_ratio = self.optimizer.MIN_EXPLORATION_BUDGET_RATIO
            new_ratio = min(0.50, old_ratio + 0.10)
            self.optimizer.MIN_EXPLORATION_BUDGET_RATIO = new_ratio
            actions.append({
                "action": "boost_exploration",
                "from": old_ratio,
                "to": new_ratio,
                "note": "qualité en baisse → plus d'exploration, moins d'exploitation",
            })

        elif atype == "empty_verticals":
            # Marque ces verticales comme prioritaires pour le prochain launch.
            self._mark_priority_verticals(anomaly["verticals"])
            actions.append({
                "action": "mark_priority_verticals",
                "verticals": anomaly["verticals"],
                "note": "le prochain --action launch lancera ces verticales en premier",
            })

        elif atype == "all_paused_crisis":
            # Débloque le launch : reset des seuils + marque toutes verticales prioritaires.
            self.optimizer.MIN_SPEND_BEFORE_PAUSE = max(50.0, self.optimizer.MIN_SPEND_BEFORE_PAUSE)
            configured = list(self.config.get("meta", {}).get("targeting", {}).keys())
            self._mark_priority_verticals(configured)
            actions.append({
                "action": "emergency_relaunch_unlock",
                "min_spend_before_pause": self.optimizer.MIN_SPEND_BEFORE_PAUSE,
                "note": "0 ads actives → mode urgence, prochain launch génère partout",
            })

        return actions

    # ----- Helpers -----

    def _mark_priority_verticals(self, verticals: list):
        data = {"priorities": list(verticals), "set_at": datetime.now().isoformat()}
        with open(self.priority_verticals_path, "w") as f:
            json.dump(data, f, indent=2)

    def get_priority_verticals(self) -> list:
        """L'orchestrator lit ça pour traiter ces verticales en premier dans launch."""
        if not self.priority_verticals_path.exists():
            return []
        try:
            with open(self.priority_verticals_path) as f:
                data = json.load(f)
            # On expire les priorités après 24h (sinon ça reste forever)
            try:
                set_at = datetime.fromisoformat(data.get("set_at", ""))
                if (datetime.now() - set_at) > timedelta(hours=24):
                    return []
            except (ValueError, TypeError):
                pass
            return data.get("priorities", [])
        except (json.JSONDecodeError, OSError):
            return []

    def clear_priorities(self):
        """À appeler après que le launch a traité les verticales prioritaires."""
        if self.priority_verticals_path.exists():
            self.priority_verticals_path.unlink()

    def _log(self, entry: dict):
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
