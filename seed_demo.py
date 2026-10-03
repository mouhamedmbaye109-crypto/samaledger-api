"""Jeu de données de démonstration (compte de test + 12 mois d'activité).
Usage : python seed_demo.py [http://localhost:8000]   (le serveur doit tourner)"""
import random, sys
from datetime import date, timedelta
import httpx

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
EMAIL, PASSWORD = "demo@samaledger.sn", "Demo-SamaLedger-2026"   # COMPTE DE TEST LOCAL : ne jamais utiliser en production
c = httpx.Client(base_url=URL, timeout=60)
r = c.post("/auth/register", json={"company_name": "Société Démo SARL", "email": EMAIL, "password": PASSWORD})
if r.status_code == 409:
    sys.exit(f"Le compte {EMAIL} existe déjà : connectez-vous avec, ou supprimez la base pour repartir de zéro.")
r.raise_for_status()
H = {"Authorization": "Bearer " + r.json()["token"]}
rnd, today = random.Random(42), date.today()
PAYS = [("SN", .44), ("CI", .16), ("ML", .10), ("GN", .07), ("MR", .05), ("FR", .08), ("MA", .04), ("NG", .04), ("AE", .02)]
LIGNES = [("Eau minérale", .50, .62), ("Boissons gazeuses", .30, .70), ("Jus", .20, .76)]   # part des ventes, coût / ventes
stmt = []
for i in range(12):
    y, m = divmod(today.year * 12 + today.month - 1 - (11 - i), 12); m += 1
    start, tag = date(y, m, 1), f"{y}{m:02d}"
    last = min(today, date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    total = 45e6 * (1 + .03 * i) * (.9 + .2 * rnd.random())
    def day(): return min(start + timedelta(days=rnd.randint(0, 26)), last)
    for k, (line, share, cost) in enumerate(LIGNES):
        sold = 0
        for code, w in PAYS:
            amt = round(total * share * w * (.85 + .3 * rnd.random()), -3)
            d, num = day(), f"F-{tag}-{code}{k}"
            c.post("/invoices", headers=H, json={"third_party": f"Distributeur {code}", "number": num, "kind": "vente", "amount": int(amt),
                   "issue_date": d.isoformat(), "due_date": (d + timedelta(days=30)).isoformat(), "country": code, "product_line": line}).raise_for_status()
            sold += amt
            if i < 11 or code == "SN":       # les ventes anciennes (et 3 du mois en cours) sont encaissées
                stmt.append((min(d + timedelta(days=10), today), f"VIR WAVE client {num}", int(amt)))
        buy, d, num = round(sold * cost * (.95 + .1 * rnd.random()), -3), day(), f"A-{tag}-{k}"
        c.post("/invoices", headers=H, json={"third_party": f"Fournisseur {line}", "number": num, "kind": "achat", "amount": int(buy),
               "issue_date": d.isoformat(), "due_date": (d + timedelta(days=30)).isoformat(), "product_line": line}).raise_for_status()
        if i < 11:
            stmt.append((min(d + timedelta(days=15), today), f"PAIEMENT FOURN {num}", -int(buy)))
    stmt += [(last, f"PAIE SALAIRES {tag}", -9_000_000), (last, f"FRAIS BANCAIRES TENUE COMPTE {tag}", -3500), (last, f"RETRAIT DAB PLATEAU {tag}", -400_000)]
stmt += [(today, "VIREMENT INCONNU XZ", 60000), (today, "VIR ORANGE MONEY boutique", 25000), (today, "VIR ORANGE MONEY boutique", 25000)]
csv = "\n".join(f"{d.isoformat()};{lib};{a}" for d, lib, a in stmt)
imp = c.post("/bank/import", headers=H, files={"file": ("releve.csv", csv.encode(), "text/csv")}); imp.raise_for_status()
val = c.post("/bank/validate-safe", headers=H).json()
for dep, b, s in [("Production", 500_000_000, 420_000_000), ("Marketing", 120_000_000, 95_000_000)]:
    c.put("/budgets", headers=H, json={"department": dep, "year": today.year, "budget": b, "spent": s}).raise_for_status()
print("Import :", imp.json(), "| validées :", val)
print(f"\nCompte de test -> e-mail : {EMAIL}   mot de passe : {PASSWORD}")
