# Meta Ads Auto-Pilot 🚀

Le **bouton qui fait tout** : Claude génère les concepts, OpenAI (gpt-image-1) produit les images, le système publie sur Meta et trouve le meilleur budget tout seul.

> Créas **image uniquement** (single image ads). Pas de vidéo ni d'avatar IA.

## L'idée centrale

Tu fixes UNE seule chose : **ton budget quotidien max** (ex: 100€/jour).

Le système fait le reste :
- Claude crée les concepts + le copy (en s'appuyant sur ce qui a converti dans le passé)
- Validation auto contre les policies Meta (avant de gaspiller du crédit image)
- Génération de l'image publicitaire via OpenAI (gpt-image-1)
- Publication sur Meta (single image ad) avec un budget de départ minimal
- **Thompson Sampling** réalloue le budget en continu vers les ads gagnantes
- Pause auto des ads qui ne performent vraiment pas

## Pourquoi pas de "seuil de CPL fixe" ?

Parce que c'est nul. Un CPL de 20€ peut être excellent pour une créa et catastrophique pour une autre. Un seuil fixe te fait pauser des ads qui auraient explosé après 50€ de plus, et scaler des ads qui vont t'exploser à 200€.

Le **Thompson Sampling** (multi-arm bandit bayésien) :
- Modélise chaque ad comme un "bras" avec une distribution de probabilité de générer un lead par euro
- À chaque cycle, tire un échantillon de chaque distribution → alloue plus de budget aux meilleurs samples
- Fait naturellement de l'exploration (test) ET de l'exploitation (scale) sans qu'on lui dise quand
- Robuste au bruit : ne pause pas après 1 mauvais jour
- C'est la même approche que Netflix/Spotify pour leurs recos

## Architecture

```
                  ┌─────────────────────┐
   CRON (1h)  ──→ │   orchestrator.py   │
                  └──────────┬──────────┘
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
  ┌──────────┐       ┌──────────────┐      ┌─────────────┐
  │  Claude  │       │ Thompson     │      │   Meta      │
  │ creative │       │ Sampling     │      │ Marketing   │
  │ generator│       │ optimizer    │      │ API         │
  └────┬─────┘       └──────────────┘      └─────────────┘
       │
       ▼
  ┌──────────┐
  │  OpenAI  │
  │  image   │
  └──────────┘
```

## Setup

### 1. Installation

```bash
cd meta-ads-automation
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Variables d'environnement

```bash
cp .env.example .env
# Édite .env avec tes vraies clés
```

### 3. Config

Édite `config/config.json` :
- `daily_total_budget` : ton plafond quotidien (€)
- `meta.ad_account_id`, `page_id`, `pixel_id` : tes IDs Meta
- `meta.targeting` : ajuste le ciblage de l'offre `famille_bundle` (région Québec, âges, intérêts)

### 4. Comment obtenir un Meta System User Token (le bon)

⚠️ N'utilise PAS un user token classique, il expire en 60 jours et casserait ton cron.

1. business.facebook.com → Business Settings → Users → **System Users**
2. Ajouter un système user avec rôle "Admin"
3. Lui assigner ton ad account et ta page
4. Generate Token → permissions : `ads_management`, `ads_read`, `business_management`, `pages_show_list`, `pages_read_engagement`
5. **Choisir "Never expire"**

## Usage

### Test manuel

```bash
# Crée juste de nouvelles ads
python src/orchestrator.py --action launch

# Optimise juste les budgets existants
python src/orchestrator.py --action optimize

# Le bouton magique : tout d'un coup
python src/orchestrator.py --action full
```

### Cron job (le vrai mode "appuie sur un bouton")

```bash
crontab -e
```

Ajoute :

```cron
# Toutes les heures : optimise les budgets
0 * * * * cd /chemin/vers/meta-ads-automation && /chemin/venv/bin/python src/orchestrator.py --action optimize >> logs/cron.log 2>&1

# Tous les jours à 9h : lance de nouvelles créas
0 9 * * * cd /chemin/vers/meta-ads-automation && /chemin/venv/bin/python src/orchestrator.py --action launch >> logs/cron.log 2>&1
```

## Pourquoi cette séparation ?

- `optimize` (toutes les heures) : léger, juste des appels API Meta + calculs locaux. Garde les budgets réactifs.
- `launch` (1×/jour) : coûte de l'argent (OpenAI gpt-image-1 ~0.04$/image, Claude ~0.20$/concept). Pas besoin d'en générer 24×/jour.

## Anti-patterns évités

❌ Régénérer 50 ads par jour → flag du compte Meta  
❌ Scaler à +200% en une fois → reset apprentissage Meta  
❌ Pauser après 1 jour de mauvaise perf → bruit statistique  
❌ User token Meta → expire, cron casse  
❌ Hardcoder un "CPL cible" → le bandit le trouve tout seul

## Catégorie spéciale Meta (télécom)

Selon ta région, une offre télécom peut être classée comme Special Ad Category par Meta. Si oui :
- Édite `_create_campaign()` dans `meta_publisher.py` 
- Change `"special_ad_categories": "[]"` → `'["CREDIT"]'` (ou la catégorie correspondante)
- Ciblage sera automatiquement restreint par Meta (pas d'âge précis, etc.)

## Monitoring

Tous les logs vont dans `logs/orchestrator_YYYYMMDD.log`. 

Pour voir l'état du bandit en direct :
```bash
cat data/bandit_state.json | jq '.ads | to_entries[] | {id: .key, vertical: .value.vertical, leads: .value.leads, spend: .value.spend, cpl: (.value.spend / (.value.leads + 0.01))}'
```

## Roadmap si tu veux pousser plus loin

1. **Webhook Meta** pour les leads (déjà partiellement chez toi) → enrichis le bandit en quasi-temps-réel
2. **A/B test sur les hooks** : varie les 3 premières secondes uniquement, le bandit te dira lesquels marchent
3. **LLM-as-a-judge** pour scorer les créas avant publication (Claude évalue 10 brouillons, garde les 3 meilleurs)
4. **Time-of-day bidding** : le bandit peut être étendu à du Contextual Bandit (heure, jour de la semaine)
