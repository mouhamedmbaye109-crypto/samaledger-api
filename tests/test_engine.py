from app.engine import classify, parse_amount, tx_hash
from datetime import date


def test_encaissement_wave_rapproche():
    r = classify("VIR WAVE client", 150000, True)
    assert (r["debit"], r["credit"], r["confidence"]) == ("521", "411", "élevée")


def test_paiement_fournisseur_n_est_pas_un_salaire():
    r = classify("PAIEMENT FOURN ABC", -95000)
    assert (r["debit"], r["credit"]) == ("401", "521")


def test_frais_et_retrait():
    assert classify("FRAIS BANCAIRES", -3500)["debit"] == "631"
    assert classify("RETRAIT DAB PLATEAU", -40000)["debit"] == "571"


def test_inconnu_va_en_attente():
    r = classify("VIREMENT INCONNU XZ", 60000)
    assert (r["debit"], r["credit"], r["confidence"]) == ("521", "471", "faible")


def test_facture_sans_regle():
    assert classify("VIR DIVERS", 80000, True)["credit"] == "411"


def test_parse_et_hash():
    assert parse_amount("1 500") == 1500 and parse_amount("-3500") == -3500
    assert tx_hash(date(2026, 9, 4), "a ", 5) == tx_hash(date(2026, 9, 4), "A", 5)
