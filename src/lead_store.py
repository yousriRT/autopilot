"""
Lead Store
==========

Persistance locale de tous les leads bruts générés. Sert de CRM-substitute
en attendant l'intégration au vrai CRM. Permet :
- Audit (qui s'est inscrit, sur quelle créa, à quelle heure)
- Export CSV pour transmission au CRM
- Suivi du funnel (lead → contacté → converti) si on enregistre les statuts

Stockage : `data/leads.jsonl` append-only. Une ligne = un lead. Format Meta
Lead Ads + champs internes (score qualité, ad_id, vertical, statut funnel).
"""

import csv
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Iterator

log = logging.getLogger(__name__)


class LeadStore:
    def __init__(self, store_path=None):
        self.path = Path(store_path) if store_path else (Path(__file__).parent.parent / "data" / "leads.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(
        self,
        lead_data: dict,
        ad_id: str,
        vertical: str,
        quality_score: float,
        funnel_status: str = "received",
    ):
        """
        Ajoute un lead. Idempotent sur lead_id Meta : si déjà présent, skip.
        """
        lead_id = lead_data.get("id")
        if not lead_id:
            log.warning("LeadStore.add: lead sans id, skip")
            return False
        if self.exists(lead_id):
            return False

        entry = {
            "lead_id": lead_id,
            "ad_id": ad_id,
            "vertical": vertical,
            "quality_score": round(float(quality_score), 3),
            "funnel_status": funnel_status,  # received | contacted | converted | lost
            "received_at": datetime.now().isoformat(),
            "meta_created_time": lead_data.get("created_time"),
            "form_id": lead_data.get("form_id"),
            "field_data": lead_data.get("field_data", []),
            "status_history": [],
        }
        with open(self.path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return True

    def exists(self, lead_id: str) -> bool:
        if not self.path.exists():
            return False
        # Linear scan — ok pour des volumes < 100k. Au-delà, switcher SQLite.
        with open(self.path) as f:
            for line in f:
                try:
                    if json.loads(line).get("lead_id") == lead_id:
                        return True
                except json.JSONDecodeError:
                    continue
        return False

    def iter_leads(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with open(self.path) as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def list_recent(self, hours: int = 24, vertical: Optional[str] = None) -> list:
        cutoff = datetime.now() - timedelta(hours=hours)
        out = []
        for lead in self.iter_leads():
            try:
                ts = datetime.fromisoformat(lead["received_at"])
            except (ValueError, KeyError):
                continue
            if ts < cutoff:
                continue
            if vertical and lead.get("vertical") != vertical:
                continue
            out.append(lead)
        return out

    def update_funnel_status(self, lead_id: str, new_status: str, note: str = ""):
        """
        Met à jour le statut funnel d'un lead. Réécrit le fichier (lent mais
        rare — typiquement appelé manuellement quand le CRM remonte un statut).
        """
        if not self.path.exists():
            return False

        updated = False
        leads = list(self.iter_leads())
        for lead in leads:
            if lead.get("lead_id") == lead_id:
                lead.setdefault("status_history", []).append({
                    "ts": datetime.now().isoformat(),
                    "from": lead.get("funnel_status"),
                    "to": new_status,
                    "note": note,
                })
                lead["funnel_status"] = new_status
                updated = True
                break

        if not updated:
            return False

        tmp = self.path.with_suffix(".jsonl.tmp")
        with open(tmp, "w") as f:
            for lead in leads:
                f.write(json.dumps(lead, default=str) + "\n")
        tmp.replace(self.path)
        return True

    def export_csv(self, target_path: Path, since_hours: int = 24) -> int:
        """
        Exporte les leads récents en CSV pour transmission au CRM.
        Aplatit field_data en colonnes individuelles.
        """
        leads = self.list_recent(hours=since_hours)
        if not leads:
            return 0

        # Découvre toutes les clés field_data possibles
        all_field_names = set()
        for lead in leads:
            for f in lead.get("field_data", []):
                all_field_names.add(f.get("name", ""))
        field_cols = sorted(all_field_names - {""})

        cols = ["lead_id", "ad_id", "vertical", "quality_score", "funnel_status",
                "received_at", "meta_created_time"] + field_cols

        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=cols)
            writer.writeheader()
            for lead in leads:
                row = {k: lead.get(k, "") for k in cols if k in lead}
                # Aplatir field_data
                for fd in lead.get("field_data", []):
                    name = fd.get("name")
                    values = fd.get("values", [])
                    if name in field_cols and values:
                        row[name] = values[0]
                writer.writerow(row)
        return len(leads)

    def stats(self) -> dict:
        """Stats globales pour debug / dashboard."""
        leads = list(self.iter_leads())
        if not leads:
            return {"total": 0, "by_status": {}, "avg_quality": None, "by_vertical": {}}
        by_status = {}
        by_vertical = {}
        quality_sum = 0.0
        for l in leads:
            s = l.get("funnel_status", "?")
            by_status[s] = by_status.get(s, 0) + 1
            v = l.get("vertical", "?")
            by_vertical[v] = by_vertical.get(v, 0) + 1
            quality_sum += float(l.get("quality_score", 0))
        return {
            "total": len(leads),
            "by_status": by_status,
            "by_vertical": by_vertical,
            "avg_quality": round(quality_sum / len(leads), 3),
        }
