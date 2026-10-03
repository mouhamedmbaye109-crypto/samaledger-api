# SamaLedger API

Backend FastAPI + PostgreSQL du prototype SamaLedger : comptabilité en partie double (SYSCOHADA), import bancaire avec classement explicable, rapprochement de factures, journal, balance, tableau de bord.

## Démarrage
```bash
cp .env.example .env        # puis changez DB_PASSWORD et JWT_SECRET
docker compose up --build   # Interface : http://localhost:8000  |  API : http://localhost:8000/docs
```
Sans Docker : créez une base PostgreSQL 16, exécutez `schema.sql`, puis
`DATABASE_URL=postgresql://... JWT_SECRET=... uvicorn app.main:app`.

## Compte de test (données de démonstration)
```bash
docker compose exec -T api python seed_demo.py     # une seule fois, API démarrée (-T : nécessaire dans Git Bash)
```
Crée la société « Société Démo SARL » avec 12 mois de factures (9 pays, 3 lignes de produit), un relevé bancaire importé et validé, et des budgets.
- Adresse : http://localhost:8000
- E-mail : `demo@samaledger.sn`
- Mot de passe : `Demo-SamaLedger-2026`

Compte réservé aux essais en local : ne l'utilisez jamais en production.

## Si l'interface affiche « Failed to fetch »
Le navigateur n'arrive pas à joindre l'API. Vérifiez dans l'ordre :
1. `docker compose ps` : les services `db` et `api` doivent être « running » (sinon `docker compose logs api`).
2. http://localhost:8000/health doit afficher `{"status":"ok"}`.
3. Ouvrez l'interface via http://localhost:8000, pas en double-cliquant sur `index.html` ni depuis un lien claude.ai.
4. Si vous ouvrez le fichier directement, renseignez « Adresse du serveur » sur l'écran de connexion (`http://localhost:8000`). Pour la production, retirez `null` de `ALLOWED_ORIGINS`.

## Si l'interface affiche « Erreur interne »
Cause la plus fréquente : la base a été créée avec une ancienne version du schéma (le script `schema.sql` ne s'exécute qu'à la **première** création de la base). Repartez d'une base neuve (cela efface les données) :
```bash
docker compose down -v
docker compose up --build
docker compose exec -T api python seed_demo.py
```
Sinon, la cause exacte est affichée par `docker compose logs api`.

## Interface hébergée à part (GitHub Pages)
GitHub Pages n'héberge que des fichiers : l'API et la base doivent tourner ailleurs (Codespace, ou un hébergeur comme Render, Railway ou Fly.io). Sur l'écran de connexion, saisissez l'adresse de l'API. Côté API, autorisez le site dans `.env` puis relancez `docker compose up -d` :
```
ALLOWED_ORIGINS=https://VOTRE-COMPTE.github.io
```
(l'origine ne contient ni le nom du dépôt ni `/`). L'API doit être en `https://` pour être appelée depuis une page https.

## Interface
L'interface (`static/index.html`) est servie par l'API sur http://localhost:8000/app/ : créez une société, enregistrez vos factures (onglet Factures), puis importez un relevé (onglet Banque) et validez. Le globe (revenus par pays), l'histogramme mensuel et l'anneau de marge sont calculés à partir des factures : renseignez le pays et la ligne de produit à la saisie (`GET /reports/insights`).

## Essai rapide
```bash
API=http://localhost:8000
TOKEN=$(curl -s $API/auth/register -H 'content-type: application/json' \
  -d '{"company_name":"Ma société","email":"moi@exemple.sn","password":"un-mot-de-passe-long"}' | jq -r .token)
curl -s $API/bank/import -H "Authorization: Bearer $TOKEN" -F file=@releve.csv   # date;libellé;montant
curl -s -X POST $API/bank/validate-safe -H "Authorization: Bearer $TOKEN"
curl -s $API/reports/trial-balance -H "Authorization: Bearer $TOKEN"
```

## Ce que la base garantit (pas seulement l'application)
- Débit = crédit vérifié à la validation d'une écriture.
- Une écriture validée ne peut être ni modifiée ni supprimée : on la corrige par contrepassation (`POST /entries/{id}/reverse`).
- Journal d'audit alimenté par des triggers, en lecture seule.
- Chaque table porte `company_id` et chaque requête est filtrée par la société du jeton (isolation testée).

## Tests
`pytest tests` (moteur de classement). Le flux complet a été vérifié sur PostgreSQL 16 : inscription, facture, import, doublon, validation, balance équilibrée, contrepassation, immuabilité, audit, isolation entre sociétés.

## Pas encore fait (à prévoir avant une mise en production)
- Numérotation continue des écritures, clôtures mensuelle et annuelle, gestion des exercices.
- TVA, paie, immobilisations, stocks, OCR, fiscalité.
- Règles de classement propres à chaque société (elles sont codées en dur dans `app/engine.py`).
- Limitation des tentatives de connexion, gestion des utilisateurs, row-level security PostgreSQL, sauvegardes, HTTPS.
- Validation du plan comptable complet et des règles par un expert-comptable SYSCOHADA.
