"""Moteur de classification comptable : règles explicables (SYSCOHADA)."""
import hashlib, re
from datetime import date
from decimal import Decimal

# (motif, sens : +1 entrée / -1 sortie, débit, crédit, libellé, justification)
RULES = [
    (r"FRAIS|COMMISSION|AGIOS", -1, "631", "521", "Frais bancaires", "le libellé contient des frais bancaires (compte 631)."),
    (r"RETRAIT|\bDAB\b", -1, "571", "521", "Retrait d'espèces", "un retrait DAB alimente la caisse (compte 571)."),
    (r"SALAIRE|PAIE\b", -1, "661", "521", "Salaires", "le libellé évoque la paie (compte 661)."),
    (r"WAVE|ORANGE|BOUTIQUE|CLIENT", 1, "521", "411", "Encaissement client",
     "l'entrée provient d'un canal de vente connu, rapprochée d'un client (compte 411)."),
    (r"FOURN|SENELEC|SONATEL|\bSDE\b|ACHAT", -1, "401", "521", "Paiement fournisseur",
     "le bénéficiaire correspond à un fournisseur (compte 401)."),
]


def classify(label: str, amount: int, has_invoice: bool = False) -> dict:
    for pat, sign, d, c, name, why in RULES:
        if re.search(pat, label, re.I) and (sign > 0) == (amount > 0):
            return dict(debit=d, credit=c, rule=name, recognized=True,
                        confidence="élevée" if (has_invoice or amount < 0) else "moyenne",
                        explanation=f"J'ai classé cette opération comme « {name} » car {why}")
    if has_invoice:
        d, c = ("521", "411") if amount > 0 else ("401", "521")
        return dict(debit=d, credit=c, rule="Règlement de facture", recognized=True, confidence="élevée",
                    explanation="Le montant correspond exactement à une facture ouverte.")
    d, c = ("521", "471") if amount > 0 else ("471", "521")
    return dict(debit=d, credit=c, rule=None, recognized=False, confidence="faible",
                explanation="Tiers non reconnu : écriture en compte d'attente (471), à valider par un comptable.")


def tx_hash(d: date, label: str, amount: int) -> str:
    return hashlib.sha256(f"{d.isoformat()}|{label.strip().upper()}|{amount}".encode()).hexdigest()


def parse_amount(s: str) -> int:
    m = int(Decimal(s.replace(" ", "").replace("\u202f", "").replace(",", ".")))
    if m == 0:
        raise ValueError("montant nul")
    return m
