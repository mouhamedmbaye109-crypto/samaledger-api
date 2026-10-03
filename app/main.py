import csv, io, logging, os
from contextlib import asynccontextmanager
from datetime import date
from typing import Literal
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import pool, tx
from .engine import classify, parse_amount, tx_hash
from .security import check_pw, current_user, hash_pw, make_token, require

WRITE = require("admin", "comptable")
log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_):
    pool.open()
    yield
    pool.close()


app = FastAPI(title="SamaLedger API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("ALLOWED_ORIGINS", "").split(","),
                   allow_methods=["*"], allow_headers=["*"])

STATUS = {psycopg.errors.UniqueViolation: 409, psycopg.errors.RaiseException: 409,
          psycopg.errors.ForeignKeyViolation: 422, psycopg.errors.CheckViolation: 422}


@app.exception_handler(psycopg.Error)
async def db_error(_, e):
    st = STATUS.get(type(e))
    if not st:
        log.error("Erreur base de données : %s", e)
        if isinstance(e, psycopg.OperationalError):
            return JSONResponse({"detail": "Base de données indisponible"}, status_code=503)
        return JSONResponse({"detail": "Erreur interne (cause : docker compose logs api)"}, status_code=500)
    return JSONResponse({"detail": e.diag.message_primary}, status_code=st)


# ---------- Modèles ----------
class Register(BaseModel):
    company_name: str = Field(min_length=2)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=10)


class Login(BaseModel):
    email: str
    password: str


class InvoiceIn(BaseModel):
    third_party: str = Field(min_length=1)
    number: str
    kind: Literal["vente", "achat"]
    amount: int = Field(gt=0)
    issue_date: date
    due_date: date
    country: str | None = Field(None, pattern=r"^[A-Z]{2}$")
    product_line: str | None = None


class ValidateIn(BaseModel):
    debit_code: str | None = None
    credit_code: str | None = None


class AccountIn(BaseModel):
    code: str = Field(pattern=r"^[1-8][0-9]{1,9}$")
    label: str = Field(min_length=2)


class BudgetIn(BaseModel):
    department: str
    year: int
    budget: int = Field(ge=0)
    spent: int = Field(ge=0)


# ---------- Authentification ----------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/register", status_code=201)
def register(b: Register):
    with tx() as c:
        cid = c.execute("INSERT INTO companies(name) VALUES(%s) RETURNING id", (b.company_name,)).fetchone()["id"]
        c.execute("INSERT INTO accounts(company_id,code,label) SELECT %s,code,label FROM account_template", (cid,))
        c.execute("INSERT INTO bank_accounts(company_id,name) VALUES(%s,'Compte principal')", (cid,))
        u = c.execute("INSERT INTO users(company_id,email,password_hash,role) VALUES(%s,%s,%s,'admin') "
                      "RETURNING id,company_id,role", (cid, b.email.lower(), hash_pw(b.password))).fetchone()
    return {"token": make_token(u)}


@app.post("/auth/login")
def login(b: Login):
    with tx() as c:
        u = c.execute("SELECT * FROM users WHERE email=%s", (b.email.lower(),)).fetchone()
    if not u or not check_pw(b.password, u["password_hash"]):
        raise HTTPException(401, "Identifiants incorrects")
    return {"token": make_token(u)}


# ---------- Plan comptable ----------
@app.get("/accounts")
def accounts(q: str = "", u=Depends(current_user)):
    with tx() as c:
        return c.execute("SELECT code,label,class FROM accounts WHERE company_id=%s AND (code LIKE %s OR label ILIKE %s) "
                         "ORDER BY code", (u["cid"], q + "%", "%" + q + "%")).fetchall()


@app.post("/accounts", status_code=201)
def add_account(b: AccountIn, u=Depends(WRITE)):
    with tx(u["sub"]) as c:
        c.execute("INSERT INTO accounts(company_id,code,label) VALUES(%s,%s,%s)", (u["cid"], b.code, b.label))
    return {"code": b.code}


# ---------- Factures ----------
@app.post("/invoices", status_code=201)
def add_invoice(b: InvoiceIn, u=Depends(WRITE)):
    with tx(u["sub"]) as c:
        tp = c.execute("INSERT INTO third_parties(company_id,kind,name) VALUES(%s,%s,%s) "
                       "ON CONFLICT (company_id,kind,name) DO UPDATE SET name=EXCLUDED.name RETURNING id",
                       (u["cid"], "client" if b.kind == "vente" else "fournisseur", b.third_party)).fetchone()
        r = c.execute("INSERT INTO invoices(company_id,third_party_id,number,kind,amount,issue_date,due_date,country,product_line) "
                      "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                      (u["cid"], tp["id"], b.number, b.kind, b.amount, b.issue_date, b.due_date, b.country, b.product_line)).fetchone()
    return r


@app.get("/invoices")
def invoices(status: Literal["ouverte", "payée"] | None = None, u=Depends(current_user)):
    with tx() as c:
        return c.execute("SELECT i.*, t.name AS third_party FROM invoices i JOIN third_parties t ON t.id=i.third_party_id "
                         "WHERE i.company_id=%s AND (%s::text IS NULL OR i.status=%s) ORDER BY i.due_date",
                         (u["cid"], status, status)).fetchall()


# ---------- Banque : import, classement, validation ----------
MATCH_SQL = """SELECT id FROM invoices WHERE company_id=%s AND kind=%s AND status='ouverte' AND amount=%s
  AND id NOT IN (SELECT matched_invoice_id FROM bank_transactions
                 WHERE company_id=%s AND matched_invoice_id IS NOT NULL) LIMIT 1"""


@app.post("/bank/import", status_code=201)
def bank_import(file: UploadFile = File(...), u=Depends(WRITE)):
    """CSV séparé par « ; » : date (AAAA-MM-JJ);libellé;montant (+ entrée, - sortie)."""
    raw = file.file.read(2_000_001)
    if len(raw) > 2_000_000:
        raise HTTPException(413, "Fichier trop volumineux (2 Mo maximum)")
    rows, bad = [], []
    for n, r in enumerate(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"), 1):
        if not "".join(r).strip() or (n == 1 and r[0].strip().lower() == "date"):
            continue
        try:
            rows.append((date.fromisoformat(r[0].strip()), r[1].strip(), parse_amount(r[2])))
        except (ValueError, IndexError, ArithmeticError):
            bad.append(n)
    cid, seen, out = u["cid"], set(), {"importées": 0, "doublons": 0, "à_vérifier": 0, "lignes_invalides": bad}
    with tx(u["sub"]) as c:
        bank = c.execute("SELECT id FROM bank_accounts WHERE company_id=%s LIMIT 1", (cid,)).fetchone()
        for d, lib, m in rows:
            h = tx_hash(d, lib, m)
            dup = h in seen or c.execute("SELECT 1 FROM bank_transactions WHERE company_id=%s AND dedup_hash=%s",
                                         (cid, h)).fetchone() is not None
            seen.add(h)
            inv = None if dup else c.execute(MATCH_SQL, (cid, "vente" if m > 0 else "achat", abs(m), cid)).fetchone()
            k = classify(lib, m, inv is not None)
            c.execute("""INSERT INTO bank_transactions(company_id,bank_account_id,tx_date,label,amount,dedup_hash,is_duplicate,
                         debit_code,credit_code,rule_label,explanation,confidence,matched_invoice_id)
                         VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                      (cid, bank["id"], d, lib, m, h, dup, k["debit"], k["credit"], k["rule"], k["explanation"],
                       k["confidence"], inv["id"] if inv else None))
            out["importées"] += 1
            out["doublons"] += dup
            out["à_vérifier"] += dup or k["confidence"] != "élevée"
    return out


@app.get("/bank/transactions")
def transactions(status: Literal["classée", "validée", "rejetée"] | None = None, limit: int = 200, u=Depends(current_user)):
    with tx() as c:
        return c.execute("SELECT * FROM bank_transactions WHERE company_id=%s AND (%s::text IS NULL OR status=%s) "
                         "ORDER BY tx_date DESC, created_at DESC LIMIT %s",
                         (u["cid"], status, status, min(limit, 1000))).fetchall()


def post_entry(c, u, t, debit, credit):
    amt = abs(t["amount"])
    eid = c.execute("INSERT INTO journal_entries(company_id,entry_date,journal,label,created_by,source_tx_id) "
                    "VALUES(%s,%s,'BQ',%s,%s,%s) RETURNING id",
                    (u["cid"], t["tx_date"], t["label"], u["sub"], t["id"])).fetchone()["id"]
    c.execute("INSERT INTO entry_lines(entry_id,company_id,account_code,debit,credit) VALUES(%s,%s,%s,%s,0),(%s,%s,%s,0,%s)",
              (eid, u["cid"], debit, amt, eid, u["cid"], credit, amt))
    c.execute("UPDATE journal_entries SET status='validated', validated_by=%s WHERE id=%s", (u["sub"], eid))
    c.execute("UPDATE bank_transactions SET status='validée', entry_id=%s WHERE id=%s", (eid, t["id"]))
    if t["matched_invoice_id"]:
        c.execute("UPDATE invoices SET status='payée' WHERE id=%s", (t["matched_invoice_id"],))
    return eid


@app.post("/bank/transactions/{tid}/validate", status_code=201)
def validate_tx(tid: UUID, b: ValidateIn | None = None, u=Depends(WRITE)):
    b = b or ValidateIn()
    with tx(u["sub"]) as c:
        t = c.execute("SELECT * FROM bank_transactions WHERE id=%s AND company_id=%s FOR UPDATE", (tid, u["cid"])).fetchone()
        if not t:
            raise HTTPException(404, "Opération introuvable")
        if t["status"] != "classée":
            raise HTTPException(409, "Opération déjà traitée")
        if t["is_duplicate"]:
            raise HTTPException(409, "Doublon possible : rejetez l'opération")
        return {"entry_id": post_entry(c, u, t, b.debit_code or t["debit_code"], b.credit_code or t["credit_code"])}


@app.post("/bank/transactions/{tid}/reject")
def reject_tx(tid: UUID, u=Depends(WRITE)):
    with tx(u["sub"]) as c:
        n = c.execute("UPDATE bank_transactions SET status='rejetée' WHERE id=%s AND company_id=%s AND status='classée'",
                      (tid, u["cid"])).rowcount
    if not n:
        raise HTTPException(404, "Opération introuvable ou déjà traitée")
    return {"status": "rejetée"}


@app.post("/bank/validate-safe")
def validate_safe(u=Depends(WRITE)):
    """Valide toutes les opérations sûres : confiance élevée et pas de doublon."""
    with tx(u["sub"]) as c:
        rows = c.execute("SELECT * FROM bank_transactions WHERE company_id=%s AND status='classée' AND NOT is_duplicate "
                         "AND confidence='élevée' FOR UPDATE", (u["cid"],)).fetchall()
        for t in rows:
            post_entry(c, u, t, t["debit_code"], t["credit_code"])
    return {"validées": len(rows)}


# ---------- Journal ----------
@app.get("/entries")
def entries(limit: int = 100, offset: int = 0, u=Depends(current_user)):
    with tx() as c:
        return c.execute("SELECT e.id,e.entry_date,e.journal,e.label,e.status,e.reversal_of,COALESCE(SUM(l.debit),0) AS total "
                         "FROM journal_entries e LEFT JOIN entry_lines l ON l.entry_id=e.id WHERE e.company_id=%s "
                         "GROUP BY e.id ORDER BY e.entry_date DESC, e.created_at DESC LIMIT %s OFFSET %s",
                         (u["cid"], min(limit, 500), offset)).fetchall()


@app.get("/entries/{eid}")
def entry(eid: UUID, u=Depends(current_user)):
    with tx() as c:
        e = c.execute("SELECT * FROM journal_entries WHERE id=%s AND company_id=%s", (eid, u["cid"])).fetchone()
        if not e:
            raise HTTPException(404, "Écriture introuvable")
        e["lines"] = c.execute("SELECT l.account_code,a.label,l.debit,l.credit FROM entry_lines l JOIN accounts a "
                               "ON a.company_id=l.company_id AND a.code=l.account_code WHERE l.entry_id=%s ORDER BY l.id",
                               (eid,)).fetchall()
        return e


@app.post("/entries/{eid}/reverse", status_code=201)
def reverse(eid: UUID, u=Depends(WRITE)):
    """Contrepassation : la seule façon de corriger une écriture validée."""
    with tx(u["sub"]) as c:
        e = c.execute("SELECT * FROM journal_entries WHERE id=%s AND company_id=%s", (eid, u["cid"])).fetchone()
        if not e:
            raise HTTPException(404, "Écriture introuvable")
        if e["status"] != "validated":
            raise HTTPException(409, "Seules les écritures validées se contrepassent")
        nid = c.execute("INSERT INTO journal_entries(company_id,entry_date,journal,label,created_by,reversal_of) "
                        "VALUES(%s,current_date,%s,%s,%s,%s) RETURNING id",
                        (u["cid"], e["journal"], "Contrepassation : " + e["label"], u["sub"], eid)).fetchone()["id"]
        c.execute("INSERT INTO entry_lines(entry_id,company_id,account_code,debit,credit) "
                  "SELECT %s,company_id,account_code,credit,debit FROM entry_lines WHERE entry_id=%s", (nid, eid))
        c.execute("UPDATE journal_entries SET status='validated', validated_by=%s WHERE id=%s", (u["sub"], nid))
    return {"entry_id": nid}


# ---------- Rapports ----------
@app.get("/reports/trial-balance")
def trial_balance(u=Depends(current_user)):
    with tx() as c:
        rows = c.execute("SELECT l.account_code AS code, a.label, SUM(l.debit) AS debit, SUM(l.credit) AS credit "
                         "FROM entry_lines l JOIN journal_entries e ON e.id=l.entry_id AND e.status='validated' "
                         "JOIN accounts a ON a.company_id=l.company_id AND a.code=l.account_code "
                         "WHERE l.company_id=%s GROUP BY 1,2 ORDER BY 1", (u["cid"],)).fetchall()
    d, cr = sum(r["debit"] for r in rows), sum(r["credit"] for r in rows)
    return {"rows": rows, "total_debit": d, "total_credit": cr, "balanced": d == cr}


@app.get("/reports/dashboard")
def dashboard(start: date | None = None, end: date | None = None, u=Depends(current_user)):
    end = end or date.today()
    start = start or end.replace(day=1)
    cid, rng = u["cid"], None
    with tx() as c:
        kpi = c.execute("""SELECT COALESCE(SUM(amount) FILTER (WHERE amount>0),0) AS entrees,
              COALESCE(-SUM(amount) FILTER (WHERE amount<0),0) AS sorties,
              COUNT(*) FILTER (WHERE status='classée' AND (is_duplicate OR confidence='faible'
                    OR (amount>0 AND matched_invoice_id IS NULL))) AS alertes,
              COUNT(*) FILTER (WHERE status='validée') AS validees, COUNT(*) AS total
              FROM bank_transactions WHERE company_id=%s AND tx_date BETWEEN %s AND %s AND status<>'rejetée'""",
                        (cid, start, end)).fetchone()
        creances = c.execute("SELECT COALESCE(SUM(amount),0) AS montant, COUNT(*) AS factures FROM invoices "
                             "WHERE company_id=%s AND kind='vente' AND status='ouverte'", (cid,)).fetchone()
        par_jour = c.execute("""SELECT tx_date, COALESCE(SUM(amount) FILTER (WHERE amount>0),0) AS entrees,
              COALESCE(-SUM(amount) FILTER (WHERE amount<0),0) AS sorties FROM bank_transactions
              WHERE company_id=%s AND tx_date BETWEEN %s AND %s AND status<>'rejetée' GROUP BY 1 ORDER BY 1""",
                             (cid, start, end)).fetchall()
        sorties = c.execute("SELECT COALESCE(rule_label,'Non classé') AS categorie, -SUM(amount) AS montant "
                            "FROM bank_transactions WHERE company_id=%s AND amount<0 AND tx_date BETWEEN %s AND %s "
                            "AND status<>'rejetée' GROUP BY 1 ORDER BY 2 DESC", (cid, start, end)).fetchall()
    return {"periode": [start, end], "solde": kpi["entrees"] - kpi["sorties"], **kpi, "creances": creances,
            "flux_par_jour": par_jour, "sorties_par_categorie": sorties}


@app.get("/budgets")
def budgets(year: int | None = None, u=Depends(current_user)):
    with tx() as c:
        return c.execute("SELECT department,year,budget,spent, ROUND(spent*100.0/NULLIF(budget,0),1) AS pct FROM budgets "
                         "WHERE company_id=%s AND (%s::int IS NULL OR year=%s) ORDER BY department",
                         (u["cid"], year, year)).fetchall()


@app.put("/budgets")
def set_budget(b: BudgetIn, u=Depends(WRITE)):
    with tx(u["sub"]) as c:
        c.execute("INSERT INTO budgets(company_id,department,year,budget,spent) VALUES(%s,%s,%s,%s,%s) "
                  "ON CONFLICT (company_id,department,year) DO UPDATE SET budget=EXCLUDED.budget, spent=EXCLUDED.spent",
                  (u["cid"], b.department, b.year, b.budget, b.spent))
    return {"ok": True}


@app.get("/reports/insights")
def insights(u=Depends(current_user)):
    """Données des graphiques : revenus par pays, mois (12 derniers) et marge par ligne de produit."""
    cid = u["cid"]
    since = "issue_date >= date_trunc('month', current_date) - interval '11 months'"
    with tx() as c:
        pays = c.execute(f"SELECT country AS code, SUM(amount) AS montant FROM invoices WHERE company_id=%s AND kind='vente' "
                         f"AND country IS NOT NULL AND {since} GROUP BY 1 ORDER BY 2 DESC", (cid,)).fetchall()
        produits = c.execute(f"SELECT COALESCE(product_line,'Non affecté') AS ligne, SUM(amount) FILTER (WHERE kind='vente') AS ventes, "
                             f"SUM(amount) FILTER (WHERE kind='achat') AS achats FROM invoices WHERE company_id=%s AND {since} "
                             f"GROUP BY 1 ORDER BY 2 DESC NULLS LAST", (cid,)).fetchall()
        mensuel = c.execute("""WITH m AS (SELECT to_char(date_trunc('month', current_date) - make_interval(months => n), 'YYYY-MM') AS mois
                                          FROM generate_series(11, 0, -1) n)
            SELECT m.mois,
              COALESCE((SELECT SUM(amount) FROM invoices WHERE company_id=%(c)s AND kind='vente' AND to_char(issue_date,'YYYY-MM')=m.mois),0) AS revenus,
              COALESCE((SELECT SUM(amount) FROM invoices WHERE company_id=%(c)s AND kind='achat' AND to_char(issue_date,'YYYY-MM')=m.mois),0)
              + COALESCE((SELECT -SUM(amount) FROM bank_transactions WHERE company_id=%(c)s AND amount<0 AND status<>'rejetée'
                          AND matched_invoice_id IS NULL AND rule_label IS DISTINCT FROM %(r)s AND to_char(tx_date,'YYYY-MM')=m.mois),0) AS depenses
            FROM m ORDER BY 1""", {"c": cid, "r": "Retrait d'espèces"}).fetchall()
    return {"pays": pays, "produits": produits, "mensuel": mensuel}


# ---------- Interface web (même origine que l'API : pas de CORS à configurer) ----------
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/app/")


app.mount("/app", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "..", "static"), html=True), name="ui")
