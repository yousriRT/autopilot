#!/usr/bin/env bash
# Déploiement Meta Ads Auto-Pilot sur un VPS Ubuntu (idempotent).
# À lancer depuis le dossier du repo cloné :  bash deploy/setup_vps.sh
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
echo "== Repo: $ROOT =="

echo "== 1. Paquets système =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

echo "== 2. Environnement virtuel + dépendances =="
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "== 3. Dossiers runtime =="
mkdir -p data logs

echo "== 4. Vérif .env =="
if [ ! -f .env ]; then
  echo "!! .env ABSENT. Crée-le (voir deploy/DEPLOY.md) puis relance." >&2
  exit 1
fi

echo "== 5. Healthcheck =="
.venv/bin/python src/orchestrator.py --action healthcheck
echo "== OK. Configure le cron : crontab -e  (voir deploy/crontab.txt) =="
