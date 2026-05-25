# 📋 À FAIRE — Mise en route Meta Ads Auto-Pilot

> Le code est terminé et testé (357 tests, 97 % de couverture).
> Ce qui reste = configuration + setup de comptes. ~30 min de travail, pas de dev.
> Coche au fur et à mesure.

---

## 1. Décision à prendre EN PREMIER : comment les leads arrivent

Tout le reste en dépend. Deux options :

- [ ] **Option A — Landing page externe** : la pub envoie vers une page de ton site avec un formulaire.
      → Simple, mais le scoring qualité des leads ne fonctionne pas (le bandit compte les leads bruts).
- [ ] **Option B — Formulaire Meta natif (Instant Form)** : le lead est rempli dans Facebook directement.
      → C'est ce pour quoi le projet a été conçu (scoring qualité = la vraie valeur ajoutée).
      → Tu dois créer le formulaire à la main UNE fois sur Meta + me demander d'adapter le code.

**Mon choix : ✅ Option B — Formulaire Meta natif (Instant Form)** _(décidé le 18 mai 2026)_

### Sous-tâches Option B
- [x] Compte pub, Page, dataset/pixel créés sur Meta
- [ ] Créer le formulaire instantané (champs : Nom complet, Email, Téléphone, Code postal)
- [x] Page Politique de confidentialité en ligne → https://yousrirt.github.io/telecom/
- [x] Page de remerciement en ligne → https://yousrirt.github.io/telecom/merci.html
- [x] URL politique collée + formulaire publié → form id `1691157705401234`
- [x] Code adapté (creative = `lead_gen_form_id`, adset = Page, pixel optionnel, URL bidon supprimée) — 358 tests OK

---

## 2. Setup sur Meta / Business Manager (une seule fois)

- [ ] Avoir un **compte publicitaire** actif
- [ ] Avoir une **Page Facebook** pour l'entreprise
- [ ] Avoir un **Pixel** créé et relié au compte
- [ ] Générer un **System User Token** :
      business.facebook.com → Business Settings → Users → **System Users**
      → rôle Admin, assigner le compte pub + la Page
      → permissions : `ads_management`, `ads_read`, `business_management`,
        `pages_show_list`, `pages_read_engagement`, `leads_retrieval`
      → **choisir « Never expire »** (un user token normal expire en 60 j et casse le cron)
- [ ] Si Option B : créer le **formulaire Lead (Instant Form)** sur Meta et noter son ID
- [ ] Vérifier si le télécom est classé **Special Ad Category** dans ta zone
      (si oui : voir README, il faudra ajuster `special_ad_categories`)

---

## 3. Comptes & clés externes

- [x] Token Meta généré + validé (`meta_account: OK` au healthcheck)
- [x] `.env` créé + chargement auto (`load_dotenv` ajouté à orchestrator)
- [x] Clé **Anthropic** validée (`anthropic_api: ok` au healthcheck)
- [x] ~~Arcads / HeyGen / Creatify (vidéo UGC avatar)~~ → **abandonné** : créas **image uniquement, sans IA vidéo**
- [x] → **OpenAI gpt-image-1** retenu pour générer l'image publicitaire (single image ad)
- [ ] Clé **OpenAI** (`OPENAI_API_KEY`) + crédit sur le compte
- [ ] Régler `openai.quality` (low/medium/high) selon le rendu/coût voulu
- [ ] Test 1 image réelle → valider le rendu avant relance
- [ ] (Optionnel) App Password **Gmail** pour le dashboard quotidien par email
- [ ] (Optionnel) Clés **Supabase** si push automatique des leads vers le CRM

---

## 4. Configuration du projet (je peux le faire pour toi)

- [x] `ad_account_id` → `act_27109591352028062`
- [x] `page_id` → `1132799143252946`
- [ ] `pixel_id` — plus requis (formulaire natif = optimisation via la Page). Sera rendu optionnel dans le code.
- [ ] Remplacer l'URL de destination bidon `https://votresite.com/soumission`
      (codée en dur dans `src/meta_publisher.py:100`) par la vraie
      → me demander de la sortir dans la config + corriger le hardcode
- [ ] Créer le fichier `.env` à partir de `.env.example` et y coller les clés
- [ ] (Recommandé) Initialiser git (le dossier n'est pas versionné)
- [ ] Ajuster le ciblage par verticale dans `config/config.json` si besoin
      (régions, âges, intérêts)
- [ ] Vérifier le `daily_total_budget` (défaut : 100 €/jour)

---

## 5. Test avant lancement

- [ ] `python src/orchestrator.py --action healthcheck`
      → doit afficher **status: OK**
- [ ] `python src/orchestrator.py --action launch` (génère quelques créas — coûte un peu)
- [ ] Vérifier dans le Gestionnaire Meta que les pubs sont bien créées
- [ ] `python src/orchestrator.py --action optimize` (test réallocation budget)

---

## 6. Mise en production

- [x] Hébergement décidé : **VPS 24/7** (état persistant requis pour kill-switch/bandit)
- [x] Code versionné (git, sans secrets) + scripts de déploiement (`deploy/`)
- [ ] Créer un **repo GitHub privé** pour le code automation → me donner l'URL
- [ ] Créer le **VPS** (DigitalOcean Ubuntu, Toronto, ~4 $/mois)
- [ ] Déployer : cloner + `.env` + `bash deploy/setup_vps.sh` + cron (voir `deploy/DEPLOY.md`)
- [ ] Vérifier les logs dans `logs/orchestrator_AAAAMMJJ.log`
- [ ] (Optionnel) Activer le dashboard email quotidien

---

### 👉 Ce que Claude peut faire tout de suite pour toi
- Sortir l'URL de destination dans la config + corriger le hardcode
- Générer le `.env` à partir de `.env.example`
- Initialiser git
- Adapter le code pour le formulaire Meta natif (si Option B)

*(Dis-moi ce que tu veux que je lance.)*
