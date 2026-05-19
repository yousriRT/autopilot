"""
Daily Dashboard
===============

Génère un résumé quotidien (HTML + texte) de l'état du système et l'envoie
par email. Un seul appel = visibilité totale sur :
- Budget consommé vs plafond
- Leads qualifiés par verticale
- État des ads en vol (fatigue, CPL qualifié, décisions du bandit)
- Anomalies détectées

Pourquoi par email plutôt qu'un site : pas d'infra à héberger, l'utilisateur
ne change rien à sa routine, et un mail mal lu = pas de catastrophe (le système
continue à tourner).

Envoi via SMTP Gmail (smtp.gmail.com:587 STARTTLS). Nécessite un Gmail App
Password — PAS le mot de passe du compte. Génère-le ici :
https://myaccount.google.com/apppasswords (le 2FA doit être actif).
"""

import os
import json
import smtplib
import logging
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class DailyDashboard:
    def __init__(self, config: dict, tracker, optimizer, publisher):
        self.config = config
        self.tracker = tracker
        self.optimizer = optimizer
        self.publisher = publisher
        self.daily_budget_cap = config.get("daily_total_budget", 100.0)

        # Email config
        dash_cfg = config.get("dashboard", {})
        self.recipient = dash_cfg.get("recipient_email", "yousriblaligg@gmail.com")
        self.smtp_user = os.getenv("SMTP_USER")
        self.smtp_password = os.getenv("SMTP_PASSWORD")
        self.smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))

    # ---------- Build payload ----------

    def build(self) -> dict:
        """Compile l'état du système. Pure data — aucun appel réseau."""
        ads = self.tracker.data.get("ads", {})
        bandit_state = self.optimizer.state.get("ads", {})

        verticals = {}
        anomalies = []
        ads_detail = []
        total_spend = 0.0
        total_quality_leads = 0.0
        total_raw_leads = 0

        for ad_id, ad in ads.items():
            vertical = ad.get("vertical", "unknown")
            spend = float(ad.get("spend", 0))
            raw_leads = int(ad.get("leads", 0))
            qwl = self.tracker.get_quality_weighted_leads(ad_id)
            cpl_q = (spend / qwl) if qwl > 0 else None

            fatigue = self.tracker.get_fatigue(ad_id)
            bandit_ad = bandit_state.get(ad_id, {})
            status = bandit_ad.get("status", "unknown")
            current_budget = float(bandit_ad.get("daily_budget", 0))

            ads_detail.append({
                "id": ad_id,
                "vertical": vertical,
                "angle": ad.get("angle", "?"),
                "status": status,
                "current_budget": current_budget,
                "spend": spend,
                "raw_leads": raw_leads,
                "quality_leads": round(qwl, 2),
                "cpl_qualifie": cpl_q,
                "fatigue_score": fatigue.get("score", 0),
                "fatigue_level": fatigue.get("level", "fresh"),
                "fatigue_reasons": fatigue.get("reasons", []),
                "impressions": int(ad.get("impressions", 0)),
            })

            v = verticals.setdefault(vertical, {
                "leads_qualifies": 0.0, "raw_leads": 0,
                "spend": 0.0, "active_count": 0, "paused_count": 0,
            })
            v["leads_qualifies"] += qwl
            v["raw_leads"] += raw_leads
            v["spend"] += spend
            if status == "active":
                v["active_count"] += 1
            else:
                v["paused_count"] += 1

            total_spend += spend
            total_quality_leads += qwl
            total_raw_leads += raw_leads

        # Anomalies
        for vname, v in verticals.items():
            if v["active_count"] == 0 and v["paused_count"] > 0:
                anomalies.append(f"⚠ Verticale **{vname}** : 0 ad active ({v['paused_count']} pausées). Le launch va devoir générer.")
            if v["raw_leads"] > 5:
                quality_ratio = v["leads_qualifies"] / v["raw_leads"]
                if quality_ratio < 0.4:
                    anomalies.append(
                        f"⚠ Verticale **{vname}** : qualité leads basse ({quality_ratio*100:.0f}% — {v['leads_qualifies']:.1f} qualif sur {v['raw_leads']} bruts). Pourriture du funnel ?"
                    )

        # Budget pacing
        now = datetime.now()
        hours_passed = now.hour + now.minute / 60
        expected_pace = (hours_passed / 24) * self.daily_budget_cap
        pacing_warning = None
        if total_spend > expected_pace * 1.3 and hours_passed >= 6:
            pacing_warning = f"⚠ Spend {total_spend:.2f}€ vs attendu à cette heure {expected_pace:.2f}€ (+{((total_spend/expected_pace - 1)*100):.0f}%) — risque de dépassement plafond {self.daily_budget_cap}€"

        # Top performers + bottom (CPL qualifié, ads avec ≥1 lead qualif)
        rated = [a for a in ads_detail if a["cpl_qualifie"] is not None and a["quality_leads"] >= 1]
        rated.sort(key=lambda a: a["cpl_qualifie"])
        top3 = rated[:3]
        bottom3 = list(reversed(rated[-3:])) if len(rated) > 3 else []

        return {
            "date": now.strftime("%A %d %B %Y"),
            "generated_at": now.isoformat(),
            "budget": {
                "cap": self.daily_budget_cap,
                "spent": round(total_spend, 2),
                "remaining": round(max(0, self.daily_budget_cap - total_spend), 2),
                "pct_used": round(100 * total_spend / self.daily_budget_cap, 1) if self.daily_budget_cap else 0,
                "pacing_warning": pacing_warning,
            },
            "totals": {
                "raw_leads": total_raw_leads,
                "quality_leads": round(total_quality_leads, 1),
                "quality_ratio": round(total_quality_leads / total_raw_leads, 2) if total_raw_leads > 0 else None,
                "global_cpl_qualifie": round(total_spend / total_quality_leads, 2) if total_quality_leads > 0 else None,
            },
            "verticals": verticals,
            "ads": ads_detail,
            "top3": top3,
            "bottom3": bottom3,
            "anomalies": anomalies,
        }

    # ---------- Render ----------

    def render_html(self, payload: dict) -> str:
        b = payload["budget"]
        t = payload["totals"]
        bar_width = min(100, b["pct_used"])
        bar_color = "#28a745" if b["pct_used"] < 80 else ("#ffc107" if b["pct_used"] < 100 else "#dc3545")

        anomalies_html = ""
        if b.get("pacing_warning"):
            anomalies_html += f'<div style="background:#fff3cd;border-left:4px solid #ffc107;padding:10px;margin:6px 0;">{b["pacing_warning"]}</div>'
        for a in payload["anomalies"]:
            anomalies_html += f'<div style="background:#f8d7da;border-left:4px solid #dc3545;padding:10px;margin:6px 0;">{a}</div>'
        if not anomalies_html:
            anomalies_html = '<div style="background:#d4edda;border-left:4px solid #28a745;padding:10px;margin:6px 0;">✓ Rien à signaler.</div>'

        # Per-vertical table
        vert_rows = ""
        for vname, v in payload["verticals"].items():
            cpl_q = (v["spend"] / v["leads_qualifies"]) if v["leads_qualifies"] > 0 else None
            cpl_str = f"{cpl_q:.2f}€" if cpl_q else "—"
            vert_rows += f"""
            <tr>
              <td><b>{vname}</b></td>
              <td>{v['active_count']} actives / {v['paused_count']} pausées</td>
              <td>{v['leads_qualifies']:.1f} qualif ({v['raw_leads']} bruts)</td>
              <td>{v['spend']:.2f}€</td>
              <td>{cpl_str}</td>
            </tr>"""

        # Top performers
        top_rows = "".join([
            f'<tr><td>{a["vertical"]}</td><td>{a["angle"]}</td><td>{a["cpl_qualifie"]:.2f}€</td><td>{a["quality_leads"]:.1f}</td></tr>'
            for a in payload["top3"]
        ]) or '<tr><td colspan="4"><i>Pas encore d\'ad avec leads qualifiés</i></td></tr>'

        # Ads at risk (fatigue level warning ou critical)
        at_risk = [a for a in payload["ads"] if a["fatigue_level"] in ("warning", "critical") and a["status"] == "active"]
        risk_rows = "".join([
            f'<tr><td>{a["vertical"]}</td><td>{a["angle"]}</td>'
            f'<td><span style="background:#{("dc3545" if a["fatigue_level"]=="critical" else "ffc107")};color:white;padding:2px 6px;border-radius:3px;">{a["fatigue_score"]}</span></td>'
            f'<td>{", ".join(a["fatigue_reasons"][:2])}</td></tr>'
            for a in at_risk[:5]
        ]) or '<tr><td colspan="4"><i>Aucune ad fatiguée — toutes fraîches.</i></td></tr>'

        global_cpl = f"{t['global_cpl_qualifie']:.2f}€" if t['global_cpl_qualifie'] else "—"
        quality_ratio = f"{int(t['quality_ratio']*100)}%" if t['quality_ratio'] else "—"

        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Meta Ads Auto-Pilot — {payload['date']}</title></head>
<body style="font-family:-apple-system,'Segoe UI',sans-serif;max-width:780px;margin:auto;padding:20px;background:#f6f8fa;color:#24292e;">

<h1 style="margin-top:0;">Meta Ads Auto-Pilot</h1>
<p style="color:#586069;margin:0;">{payload['date']}</p>

<h2 style="margin-top:30px;">Budget</h2>
<div style="background:white;border-radius:8px;padding:15px;border:1px solid #e1e4e8;">
  <div style="font-size:24px;font-weight:600;">{b['spent']:.2f}€ <span style="color:#586069;font-size:16px;font-weight:normal;">/ {b['cap']:.0f}€</span></div>
  <div style="background:#e1e4e8;border-radius:4px;height:8px;margin:10px 0;overflow:hidden;">
    <div style="background:{bar_color};width:{bar_width}%;height:100%;"></div>
  </div>
  <div style="color:#586069;font-size:13px;">{b['pct_used']:.1f}% consommé · {b['remaining']:.2f}€ restant</div>
</div>

<h2 style="margin-top:30px;">Anomalies</h2>
{anomalies_html}

<h2 style="margin-top:30px;">Performance par verticale</h2>
<table style="width:100%;border-collapse:collapse;background:white;border-radius:8px;overflow:hidden;border:1px solid #e1e4e8;">
  <thead style="background:#f6f8fa;"><tr>
    <th style="text-align:left;padding:10px;">Verticale</th>
    <th style="text-align:left;padding:10px;">Ads</th>
    <th style="text-align:left;padding:10px;">Leads</th>
    <th style="text-align:left;padding:10px;">Spend</th>
    <th style="text-align:left;padding:10px;">CPL qualifié</th>
  </tr></thead>
  <tbody>{vert_rows}</tbody>
</table>

<h2 style="margin-top:30px;">Top 3 ads (CPL qualifié le plus bas)</h2>
<table style="width:100%;border-collapse:collapse;background:white;border-radius:8px;overflow:hidden;border:1px solid #e1e4e8;">
  <thead style="background:#f6f8fa;"><tr>
    <th style="text-align:left;padding:10px;">Verticale</th>
    <th style="text-align:left;padding:10px;">Angle</th>
    <th style="text-align:left;padding:10px;">CPL qualifié</th>
    <th style="text-align:left;padding:10px;">Leads qualif</th>
  </tr></thead>
  <tbody>{top_rows}</tbody>
</table>

<h2 style="margin-top:30px;">Ads à risque (fatigue créa)</h2>
<table style="width:100%;border-collapse:collapse;background:white;border-radius:8px;overflow:hidden;border:1px solid #e1e4e8;">
  <thead style="background:#f6f8fa;"><tr>
    <th style="text-align:left;padding:10px;">Verticale</th>
    <th style="text-align:left;padding:10px;">Angle</th>
    <th style="text-align:left;padding:10px;">Score</th>
    <th style="text-align:left;padding:10px;">Raisons</th>
  </tr></thead>
  <tbody>{risk_rows}</tbody>
</table>

<h2 style="margin-top:30px;">Globaux</h2>
<table style="width:100%;border-collapse:collapse;background:white;border-radius:8px;overflow:hidden;border:1px solid #e1e4e8;">
  <tr><td style="padding:10px;border-bottom:1px solid #e1e4e8;"><b>Leads bruts (24h)</b></td><td style="padding:10px;border-bottom:1px solid #e1e4e8;">{t['raw_leads']}</td></tr>
  <tr><td style="padding:10px;border-bottom:1px solid #e1e4e8;"><b>Leads qualifiés (24h)</b></td><td style="padding:10px;border-bottom:1px solid #e1e4e8;">{t['quality_leads']}</td></tr>
  <tr><td style="padding:10px;border-bottom:1px solid #e1e4e8;"><b>Ratio qualité</b></td><td style="padding:10px;border-bottom:1px solid #e1e4e8;">{quality_ratio}</td></tr>
  <tr><td style="padding:10px;"><b>CPL qualifié global</b></td><td style="padding:10px;">{global_cpl}</td></tr>
</table>

<p style="color:#959da5;font-size:12px;margin-top:30px;text-align:center;">
  Généré automatiquement · {payload['generated_at']}
</p>

</body></html>"""

    def render_text(self, payload: dict) -> str:
        """Version texte brute — pour les clients email qui ne rendent pas le HTML."""
        b = payload["budget"]
        t = payload["totals"]
        lines = [
            f"Meta Ads Auto-Pilot — {payload['date']}",
            "=" * 50,
            "",
            f"BUDGET : {b['spent']:.2f}€ / {b['cap']:.0f}€ ({b['pct_used']:.1f}%)",
        ]
        if b.get("pacing_warning"):
            lines.append(f"  ⚠ {b['pacing_warning']}")
        lines.append("")

        if payload["anomalies"]:
            lines.append("ANOMALIES :")
            for a in payload["anomalies"]:
                lines.append(f"  - {a}")
            lines.append("")

        lines.append("VERTICALES :")
        for vname, v in payload["verticals"].items():
            cpl_q = (v["spend"] / v["leads_qualifies"]) if v["leads_qualifies"] > 0 else None
            cpl_str = f"{cpl_q:.2f}€" if cpl_q else "—"
            lines.append(
                f"  {vname:18} {v['active_count']}a/{v['paused_count']}p · "
                f"{v['leads_qualifies']:.1f} qualif ({v['raw_leads']} bruts) · "
                f"{v['spend']:.2f}€ · CPL {cpl_str}"
            )
        lines.append("")

        if payload["top3"]:
            lines.append("TOP 3 :")
            for a in payload["top3"]:
                lines.append(f"  {a['vertical']:18} {a['angle'][:25]:25} CPL {a['cpl_qualifie']:.2f}€")
            lines.append("")

        global_cpl = f"{t['global_cpl_qualifie']:.2f}€" if t['global_cpl_qualifie'] else "—"
        lines.append(f"GLOBAL : {t['raw_leads']} leads bruts → {t['quality_leads']} qualifiés · CPL global {global_cpl}")
        return "\n".join(lines)

    # ---------- Send ----------

    def send(self, payload: Optional[dict] = None) -> bool:
        """Construit + envoie. Retourne True si envoyé."""
        if not self.smtp_user or not self.smtp_password:
            log.error(
                "SMTP_USER ou SMTP_PASSWORD manquants dans l'env. "
                "Pour Gmail : génère un App Password sur https://myaccount.google.com/apppasswords "
                "(2FA requis)."
            )
            return False

        if payload is None:
            payload = self.build()

        msg = MIMEMultipart("alternative")
        msg["Subject"] = (
            f"Meta Ads Auto-Pilot — {payload['date']} · "
            f"{payload['totals']['raw_leads']} leads · "
            f"{payload['budget']['spent']:.0f}€/{payload['budget']['cap']:.0f}€"
        )
        msg["From"] = self.smtp_user
        msg["To"] = self.recipient

        msg.attach(MIMEText(self.render_text(payload), "plain", "utf-8"))
        msg.attach(MIMEText(self.render_html(payload), "html", "utf-8"))

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(self.smtp_user, self.smtp_password)
                smtp.send_message(msg)
            log.info(f"Dashboard envoyé à {self.recipient}")
            return True
        except Exception as e:
            log.error(f"Échec envoi dashboard : {e}")
            return False

    def save_snapshot(self, payload: Optional[dict] = None) -> Path:
        """Stocke le snapshot JSON pour historique/debug."""
        if payload is None:
            payload = self.build()
        snapshot_dir = Path(__file__).parent.parent / "data" / "dashboard_snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = snapshot_dir / f"dashboard_{datetime.now():%Y%m%d_%H%M}.json"
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        return path
