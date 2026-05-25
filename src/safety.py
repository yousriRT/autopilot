"""
Safety Layer
============

Filets de sécurité indispensables avant de lâcher le système en autopilot.

1. BudgetGuardian : kill-switch absolu sur la dépense Meta. Si on dépasse
   daily_budget × HARD_CAP_MULTIPLIER, on pause TOUTES les ads, point.
   C'est ceinture + bretelles vs un bug du bandit qui scale en boucle.

2. ProcessLock : empêche deux runs concurrents de se marcher dessus.
   Si un cron se fige et que le suivant démarre, état corrompu garanti
   sans lock. Lockfile expire après LOCK_TTL_MIN pour gérer les crashes.

3. AdAccountSafetyCheck : vérifie en début de cycle que le compte Meta
   n'est pas flag/disable. Si oui, on s'arrête sans rien faire (éviter
   d'aggraver).
"""

import os
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)


# Le bandit a déjà un cap doux (proportions × budget_for_vertical) mais en cas
# de bug logique on veut un cap dur sur la dépense Meta réelle.
HARD_CAP_MULTIPLIER = 1.20  # si dépense > daily_budget × 1.2, on stoppe tout

# Lock filesystem
LOCK_TTL_MIN = 30  # un lock plus vieux que 30 min = process crashé, on l'ignore


class BudgetGuardianTriggered(Exception):
    """Levée quand le kill-switch budget se déclenche. À catcher en haut de stack."""


class BudgetGuardian:
    """Vérifie la dépense Meta cumulée et déclenche le kill-switch si dépassement."""

    def __init__(self, config: dict, publisher, state_path=None):
        self.daily_cap = float(config.get("daily_total_budget", 100.0))
        self.publisher = publisher
        self.state_path = Path(state_path) if state_path else (Path(__file__).parent.parent / "data" / "budget_guardian.json")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load()

    def _load(self) -> dict:
        if self.state_path.exists():
            with open(self.state_path) as f:
                return json.load(f)
        return {"killed_at": None, "killed_reason": None, "killed_total_spend": None}

    def _save(self):
        tmp = self.state_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2, default=str)
        tmp.replace(self.state_path)  # atomic on POSIX, near-atomic on Windows

    def is_killed(self) -> bool:
        """True si le kill-switch est déclenché aujourd'hui."""
        killed_at = self.state.get("killed_at")
        if not killed_at:
            return False
        try:
            killed_dt = datetime.fromisoformat(killed_at)
        except (ValueError, TypeError):
            return False
        # Reset le kill-switch chaque nouveau jour
        return killed_dt.date() == datetime.now().date()

    def reset_if_new_day(self):
        """Le kill-switch se reset à minuit. Méthode idempotente."""
        if self.state.get("killed_at"):
            try:
                killed_dt = datetime.fromisoformat(self.state["killed_at"])
                if killed_dt.date() < datetime.now().date():
                    log.info("Nouveau jour : reset du kill-switch budget.")
                    self.state = {"killed_at": None, "killed_reason": None, "killed_total_spend": None}
                    self._save()
            except (ValueError, TypeError):
                pass

    def check_and_enforce(self) -> bool:
        """
        Calcule la dépense totale du jour ET pause toutes les ads si dépassement.
        Retourne True si tout va bien, False si kill-switch déclenché.
        """
        self.reset_if_new_day()

        if self.is_killed():
            log.warning(
                f"Kill-switch déjà déclenché aujourd'hui à "
                f"{self.state['killed_at']} — aucune action prise."
            )
            return False

        try:
            active_ads = self.publisher.get_active_ads()
        except Exception as e:
            log.error(f"BudgetGuardian: impossible de lister les ads actives ({e}). On laisse passer (fail-open).")
            return True  # ne pas bloquer le cron sur une erreur Meta API

        total_spend = 0.0
        for ad in active_ads:
            try:
                insights = self.publisher.get_ad_insights(ad["id"], last_hours=24)
                total_spend += float(insights.get("spend", 0))
            except Exception as e:
                log.warning(f"BudgetGuardian: skip insights {ad['id']}: {e}")
                continue

        # Filet supplémentaire : la dépense compte du jour (un seul appel, ne rate
        # rien). On prend le MAX — ça ne peut que RENFORCER le kill-switch, jamais
        # l'affaiblir. Optionnel : si l'appel échoue, on garde la somme par ad.
        try:
            account_spend = float(self.publisher.get_account_spend_today())
            if account_spend > total_spend:
                log.info(
                    f"BudgetGuardian: dépense compte/jour {account_spend:.2f} "
                    f"> somme par ad {total_spend:.2f} — on retient la plus haute."
                )
                total_spend = account_spend
        except Exception as e:
            log.warning(f"BudgetGuardian: dépense compte indisponible ({e}), somme par ad conservée.")

        hard_cap = self.daily_cap * HARD_CAP_MULTIPLIER
        log.info(f"BudgetGuardian: spend 24h = {total_spend:.2f}€ / cap dur = {hard_cap:.2f}€")

        if total_spend < hard_cap:
            return True

        # KILL-SWITCH
        reason = f"Spend 24h {total_spend:.2f}€ > cap dur {hard_cap:.2f}€ (= {self.daily_cap}€ × {HARD_CAP_MULTIPLIER})"
        log.critical(f"KILL-SWITCH BUDGET : {reason}. Pause de TOUTES les ads.")

        paused = 0
        failed = 0
        for ad in active_ads:
            try:
                self.publisher.pause_ad(ad["id"])
                paused += 1
            except Exception as e:
                log.error(f"  Impossible de pauser {ad['id']}: {e}")
                failed += 1

        self.state = {
            "killed_at": datetime.now().isoformat(),
            "killed_reason": reason,
            "killed_total_spend": total_spend,
            "paused_count": paused,
            "failed_pause_count": failed,
        }
        self._save()
        log.critical(f"KILL-SWITCH : {paused} ads pausées, {failed} échecs.")
        return False


class ProcessLock:
    """
    Lockfile pour empêcher deux runs concurrents.

    Usage:
        with ProcessLock(name="optimize"):
            do_stuff()
    """

    def __init__(self, name: str = "default", lock_dir=None):
        self.name = name
        self.lock_dir = Path(lock_dir) if lock_dir else (Path(__file__).parent.parent / "data" / "locks")
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.lock_dir / f"{name}.lock"

    def __enter__(self):
        if self.lock_path.exists():
            try:
                lock_age_min = (time.time() - self.lock_path.stat().st_mtime) / 60
                if lock_age_min < LOCK_TTL_MIN:
                    raise RuntimeError(
                        f"Lock '{self.name}' actif depuis {lock_age_min:.1f}min "
                        f"(< TTL {LOCK_TTL_MIN}min). Un autre cron tourne probablement. Abort."
                    )
                else:
                    log.warning(
                        f"Lock '{self.name}' stale ({lock_age_min:.1f}min > TTL {LOCK_TTL_MIN}min). "
                        f"Process crashé précédemment, je l'écrase."
                    )
            except FileNotFoundError:
                pass

        self.lock_path.write_text(f"{os.getpid()}\n{datetime.now().isoformat()}\n")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass
        return False  # ne pas suppress les exceptions


def check_account_health(publisher) -> tuple[bool, str]:
    """
    Vérifie que le compte Meta est en bonne santé avant de publier.
    Retourne (ok, message).

    Champs scrutés :
    - account_status : 1 = ACTIVE
    - disable_reason : si != 0, compte gelé
    """
    try:
        result = publisher._request(
            "GET",
            publisher.ad_account_id,
            params={"fields": "account_status,disable_reason,name"}
        )
        status = result.get("account_status")
        disable_reason = result.get("disable_reason")

        # Meta : 1 = ACTIVE, 2 = DISABLED, 3 = UNSETTLED, 7 = PENDING_RISK_REVIEW, etc.
        if status == 1 and (disable_reason in (0, None)):
            return True, "OK"
        return False, f"account_status={status}, disable_reason={disable_reason}"
    except Exception as e:
        log.warning(f"check_account_health: échec lookup ({e}). Fail-open.")
        return True, f"check failed (fail-open): {e}"
