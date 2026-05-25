"""
Resilience Layer
================

Retry exponentiel + jitter pour les appels réseau (Meta API, Arcads, Anthropic
SDK gère déjà ses retries en interne — on ne décore pas).

Dead Letter Queue : les opérations qui échouent N fois après retry sont stockées
dans data/dead_letter.json pour inspection / re-process manuel. Le cron suivant
peut tenter de les rejouer.
"""

import json
import time
import random
import logging
import functools
from pathlib import Path
from datetime import datetime, timedelta
from typing import Callable, Optional

log = logging.getLogger(__name__)


DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 1.0
DEFAULT_MAX_DELAY = 30.0


def retry_with_backoff(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    retryable_exceptions: tuple = (Exception,),
    non_retryable_exceptions: tuple = (KeyboardInterrupt, SystemExit),
):
    """
    Décorateur. Retry l'appel avec backoff exponentiel + jitter.
    Ne retry PAS les exceptions dans non_retryable_exceptions.

    Usage:
        @retry_with_backoff(max_attempts=4)
        def call_meta(): ...
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except non_retryable_exceptions:
                    raise
                except retryable_exceptions as e:
                    last_exc = e
                    if attempt == max_attempts:
                        log.error(
                            f"{fn.__name__} a échoué après {max_attempts} tentatives : {e}"
                        )
                        raise
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    delay += random.uniform(0, delay * 0.3)  # jitter ±30%
                    log.warning(
                        f"{fn.__name__} échec tentative {attempt}/{max_attempts} ({e}). "
                        f"Retry dans {delay:.1f}s."
                    )
                    time.sleep(delay)
            if last_exc:
                raise last_exc
        return wrapper
    return decorator


class DeadLetterQueue:
    """
    Stocke les opérations échouées pour inspection / replay manuel.

    Format d'entrée :
        {
            "ts": "2026-04-25T10:00:00",
            "operation": "publish_ad",
            "payload": {...},  # tout ce qui faut pour rejouer
            "error": "...",
            "vertical": "famille_bundle",  # optionnel pour filtrer
            "attempts": 3,
        }
    """

    def __init__(self, path=None):
        self.path = Path(path) if path else (Path(__file__).parent.parent / "data" / "dead_letter.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._items = self._load()

    def _load(self) -> list:
        if self.path.exists():
            try:
                with open(self.path) as f:
                    return json.load(f)
            except json.JSONDecodeError:
                log.warning(f"Dead letter queue corrompue ({self.path}), reset.")
                return []
        return []

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._items, f, indent=2, default=str)
        tmp.replace(self.path)

    def add(self, operation: str, payload: dict, error: str, **meta):
        entry = {
            "ts": datetime.now().isoformat(),
            "operation": operation,
            "payload": payload,
            "error": str(error)[:1000],  # plafond pour éviter les giga JSON
            **meta,
        }
        self._items.append(entry)
        # Plafond mémoire : on garde les 200 derniers seulement
        self._items = self._items[-200:]
        self._save()
        log.error(f"DLQ : {operation} ajouté ({error})")

    def list_pending(self, max_age_hours: int = 48) -> list:
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        out = []
        for item in self._items:
            try:
                if datetime.fromisoformat(item["ts"]) >= cutoff:
                    out.append(item)
            except (ValueError, TypeError, KeyError):
                continue
        return out

    def clear_processed(self, operations: list):
        """Retire les entrées correspondant à des opérations résolues."""
        self._items = [i for i in self._items if i.get("operation") not in operations]
        self._save()
