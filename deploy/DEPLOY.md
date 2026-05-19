# Déploiement VPS — Meta Ads Auto-Pilot

Objectif : faire tourner le cron 24/7 sur un petit serveur (état persistant →
kill-switch + bandit fonctionnels). ~20 min, pas besoin de client SSH.

## 1. Créer le serveur (DigitalOcean — le plus simple)

1. Compte sur **digitalocean.com** (souvent crédit offert aux nouveaux).
2. **Create → Droplet**
   - Région : **Toronto (TOR1)** (proche Québec)
   - Image : **Ubuntu 24.04 LTS**
   - Type : **Basic → Regular → 4 $/mois** (512 Mo–1 Go suffit)
   - Authentification : **Password** (choisis un mot de passe root, note-le)
   - Create
3. Une fois créé : **Droplet → Console** (icône terminal en haut à droite).
   Ça ouvre un terminal **dans le navigateur** — aucun logiciel à installer.

## 2. Récupérer le code

Sur GitHub : crée un repo **privé** (ex. `autopilot`), **vide** (sans README).
Génère un token de lecture : GitHub → Settings → Developer settings →
**Fine-grained tokens** → accès en lecture à ce repo → copie le `github_pat_...`.

Dans la console du Droplet (connecte-toi en `root` + ton mot de passe) :

```bash
apt-get update -y && apt-get install -y git
git clone https://<TON_TOKEN>@github.com/<toi>/autopilot.git automatisation
cd automatisation
```

## 3. Créer le fichier .env (les secrets — jamais dans git)

```bash
nano .env
```
Colle le contenu de ton `.env` local (les 3+ clés), puis `Ctrl+O`, `Entrée`,
`Ctrl+X`.

## 4. Installer + vérifier

```bash
bash deploy/setup_vps.sh
```
Doit finir par le healthcheck en **status: OK**.

## 5. Activer le cron 24/7

```bash
crontab -e        # choisir nano si demandé
```
Colle les lignes de `deploy/crontab.txt` (vérifie le chemin
`/root/automatisation`). Sauvegarde (`Ctrl+O`, `Entrée`, `Ctrl+X`).

C'est fini : le serveur optimise chaque heure et lance des créas chaque jour,
indépendamment de ton PC.

## Surveiller

```bash
tail -f logs/cron.log                       # activité en direct
.venv/bin/python src/orchestrator.py --action healthcheck
```

## Mettre à jour le code plus tard

```bash
cd ~/automatisation && git pull && . .venv/bin/activate && pip install -r requirements.txt
```
