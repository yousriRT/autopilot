"""
Health Check
============

Diagnostique l'état du système. Utilisable :
- Manuellement pour debug : `python src/orchestrator.py --action healthcheck`
- Par un cron de monitoring externe (UptimeRobot, healthchecks.io) qui ping
  régulièrement et alerte si le check renvoie un exit code != 0.

Vérifie :
- Variables d'env critiques
- Anthropic API joignable + clé valide
- Meta API joignable + token valide + compte actif
- Arcads API joignable
- État du kill-switch budget
- Dernier run de cron < 2h (sinon = cron mort)
- Espace disque sur data/
- État de la dead-letter queue
"""

import os
import json
import logging
import shutil
from pathlib import Path
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


HEALTH_OK = 0
HEALTH_DEGRADED = 1   # warnings non bloquants
HEALTH_CRITICAL = 2   # action requise


def run_healthcheck(config: dict, claude_client, publisher, optimizer) -> tuple[int, dict]:
    """
    Renvoie (exit_code, report).
    exit_code : 0=OK, 1=DEGRADED, 2=CRITICAL.
    """
    report = {"checks": {}, "warnings": [], "errors": []}
    status = HEALTH_OK

    # --- 1. Env vars critiques (selon le fournisseur vidéo configuré) ---
    required = ["ANTHROPIC_API_KEY", "META_ACCESS_TOKEN"]
    provider = config.get("video_provider", "arcads")
    if provider == "creatify":
        required += ["CREATIFY_API_ID", "CREATIFY_API_KEY"]
    elif provider == "heygen":
        required.append("HEYGEN_API_KEY")
        if config.get("heygen", {}).get("voice_source", "elevenlabs") == "elevenlabs":
            required.append("ELEVENLABS_API_KEY")
    else:
        required.append("ARCADS_API_KEY")
    for var in required:
        present = bool(os.getenv(var))
        report["checks"][f"env_{var}"] = "ok" if present else "missing"
        if not present:
            report["errors"].append(f"Env var {var} manquante")
            status = HEALTH_CRITICAL

    # --- 2. Anthropic ping (cheap : models.list) ---
    try:
        # On évite une vraie requête de tokens, models.list est gratuit
        list(claude_client.models.list(limit=1))
        report["checks"]["anthropic_api"] = "ok"
    except Exception as e:
        report["checks"]["anthropic_api"] = f"fail: {e}"
        report["errors"].append(f"Anthropic API: {e}")
        status = max(status, HEALTH_CRITICAL)

    # --- 3. Meta API + compte actif ---
    try:
        from safety import check_account_health
        ok, msg = check_account_health(publisher)
        report["checks"]["meta_account"] = msg
        if not ok:
            report["errors"].append(f"Meta account: {msg}")
            status = max(status, HEALTH_CRITICAL)
    except Exception as e:
        report["checks"]["meta_account"] = f"unreachable: {e}"
        report["errors"].append(f"Meta API unreachable: {e}")
        status = max(status, HEALTH_CRITICAL)

    # --- 4. Kill-switch ---
    try:
        from safety import BudgetGuardian
        guardian = BudgetGuardian(config, publisher)
        if guardian.is_killed():
            report["checks"]["budget_killswitch"] = "TRIGGERED"
            report["warnings"].append(
                f"Kill-switch budget actif depuis {guardian.state.get('killed_at')} "
                f"(raison: {guardian.state.get('killed_reason')})"
            )
            status = max(status, HEALTH_DEGRADED)
        else:
            report["checks"]["budget_killswitch"] = "ok"
    except Exception as e:
        report["checks"]["budget_killswitch"] = f"check failed: {e}"
        report["warnings"].append(f"Impossible de vérifier le kill-switch: {e}")

    # --- 5. Dernier cron run ---
    log_dir = Path(__file__).parent.parent / "logs"
    last_log = None
    if log_dir.exists():
        log_files = sorted(log_dir.glob("orchestrator_*.log"), reverse=True)
        if log_files:
            last_log = log_files[0]
            try:
                last_modified = datetime.fromtimestamp(last_log.stat().st_mtime)
                age_h = (datetime.now() - last_modified).total_seconds() / 3600
                report["checks"]["last_cron_run_h"] = round(age_h, 1)
                if age_h > 2:
                    report["warnings"].append(
                        f"Dernier run cron il y a {age_h:.1f}h — cron probablement arrêté"
                    )
                    status = max(status, HEALTH_DEGRADED)
            except OSError:
                pass

    # --- 6. Espace disque sur data/ ---
    data_dir = Path(__file__).parent.parent / "data"
    try:
        usage = shutil.disk_usage(data_dir if data_dir.exists() else data_dir.parent)
        free_mb = usage.free / 1024 / 1024
        report["checks"]["disk_free_mb"] = round(free_mb)
        if free_mb < 100:
            report["errors"].append(f"Espace disque critique: {free_mb:.0f} MB libres")
            status = max(status, HEALTH_CRITICAL)
        elif free_mb < 500:
            report["warnings"].append(f"Espace disque limité: {free_mb:.0f} MB libres")
            status = max(status, HEALTH_DEGRADED)
    except Exception:
        pass

    # --- 7. Dead Letter Queue ---
    try:
        from resilience import DeadLetterQueue
        dlq = DeadLetterQueue()
        pending = dlq.list_pending(max_age_hours=48)
        report["checks"]["dlq_pending_48h"] = len(pending)
        if len(pending) > 10:
            report["warnings"].append(
                f"DLQ : {len(pending)} opérations échouées sur 48h. Inspect data/dead_letter.json"
            )
            status = max(status, HEALTH_DEGRADED)
    except Exception:
        pass

    # --- 8. Au moins une ad active par verticale ? ---
    try:
        active_verticals = {}
        for ad in optimizer.state.get("ads", {}).values():
            if ad.get("status") == "active":
                v = ad.get("vertical", "?")
                active_verticals[v] = active_verticals.get(v, 0) + 1
        report["checks"]["active_ads_by_vertical"] = active_verticals
        configured_verticals = set(config.get("meta", {}).get("targeting", {}).keys())
        empty_verticals = configured_verticals - set(active_verticals.keys())
        if empty_verticals:
            report["warnings"].append(
                f"Verticales sans ad active : {', '.join(empty_verticals)} — "
                f"le prochain --action launch devra générer."
            )
    except Exception as e:
        report["warnings"].append(f"Stats verticales fail: {e}")

    report["status"] = ["OK", "DEGRADED", "CRITICAL"][status]
    report["timestamp"] = datetime.now().isoformat()
    return status, report
