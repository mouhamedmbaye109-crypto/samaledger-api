-- SamaLedger – schéma PostgreSQL 16 : multi-sociétés, SYSCOHADA, partie double contrôlée par la base
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE companies (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  currency char(3) NOT NULL DEFAULT 'XOF',
  created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES companies ON DELETE CASCADE,
  email text NOT NULL UNIQUE,
  password_hash text NOT NULL,
  role text NOT NULL DEFAULT 'comptable' CHECK (role IN ('admin','comptable','lecteur')),
  created_at timestamptz NOT NULL DEFAULT now());

-- Modèle de plan comptable copié dans chaque nouvelle société (extrait SYSCOHADA révisé, à compléter)
CREATE TABLE account_template (code text PRIMARY KEY, label text NOT NULL);
INSERT INTO account_template VALUES
('10','Capital'),('11','Réserves'),('12','Report à nouveau'),('13','Résultat net de l''exercice'),('16','Emprunts et dettes assimilées'),
('21','Immobilisations incorporelles'),('22','Terrains'),('23','Bâtiments et installations'),('24','Matériel, mobilier et actifs biologiques'),('245','Matériel de transport'),('28','Amortissements'),
('31','Marchandises'),('32','Matières premières et fournitures liées'),('36','Produits finis'),('39','Dépréciations des stocks'),
('401','Fournisseurs, dettes en compte'),('411','Clients'),('421','Personnel, avances et acomptes'),('422','Personnel, rémunérations dues'),('431','Sécurité sociale'),
('4431','État, TVA facturée sur ventes'),('4452','État, TVA récupérable sur achats'),('447','État, impôts retenus à la source'),('471','Comptes d''attente'),
('52','Banques'),('521','Banques locales'),('55','Instruments de monnaie électronique'),('56','Banques, crédits de trésorerie'),('57','Caisse'),('571','Caisse siège'),
('60','Achats et variations de stocks'),('61','Transports'),('62','Services extérieurs A'),('63','Services extérieurs B'),('631','Frais bancaires'),('64','Impôts et taxes'),
('66','Charges de personnel'),('661','Rémunérations directes'),('67','Frais financiers'),('68','Dotations aux amortissements'),
('70','Ventes'),('701','Ventes de marchandises'),('71','Subventions d''exploitation'),('75','Autres produits'),('77','Revenus financiers'),
('81','Valeurs comptables des cessions'),('82','Produits des cessions d''immobilisations'),('83','Charges HAO'),('84','Produits HAO'),('89','Impôts sur le résultat');

CREATE TABLE accounts (
  company_id uuid NOT NULL REFERENCES companies ON DELETE CASCADE,
  code text NOT NULL CHECK (code ~ '^[1-8][0-9]{1,9}$'),
  label text NOT NULL,
  class smallint GENERATED ALWAYS AS ((ascii(code) - 48)::smallint) STORED,
  PRIMARY KEY (company_id, code));

CREATE TABLE third_parties (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES companies ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('client','fournisseur')),
  name text NOT NULL,
  UNIQUE (company_id, kind, name));

CREATE TABLE invoices (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES companies ON DELETE CASCADE,
  third_party_id uuid NOT NULL REFERENCES third_parties,
  number text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('vente','achat')),
  amount bigint NOT NULL CHECK (amount > 0),          -- XOF : pas de décimales
  country char(2), product_line text,          -- pays du client / ligne de produit (rapports)
  issue_date date NOT NULL,
  due_date date NOT NULL,
  status text NOT NULL DEFAULT 'ouverte' CHECK (status IN ('ouverte','payée')),
  UNIQUE (company_id, kind, number));

CREATE TABLE bank_accounts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES companies ON DELETE CASCADE,
  name text NOT NULL,
  account_code text NOT NULL DEFAULT '521',
  FOREIGN KEY (company_id, account_code) REFERENCES accounts);

CREATE TABLE journal_entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES companies ON DELETE CASCADE,
  entry_date date NOT NULL,
  journal text NOT NULL DEFAULT 'BQ',
  label text NOT NULL,
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','validated')),
  created_by uuid REFERENCES users,
  validated_by uuid REFERENCES users,
  validated_at timestamptz,
  source_tx_id uuid,
  reversal_of uuid UNIQUE REFERENCES journal_entries,  -- une écriture ne se contrepasse qu'une fois
  created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE entry_lines (
  id bigserial PRIMARY KEY,
  entry_id uuid NOT NULL REFERENCES journal_entries ON DELETE CASCADE,
  company_id uuid NOT NULL,
  account_code text NOT NULL,
  debit bigint NOT NULL DEFAULT 0,
  credit bigint NOT NULL DEFAULT 0,
  CHECK (debit >= 0 AND credit >= 0 AND ((debit = 0) <> (credit = 0))),
  FOREIGN KEY (company_id, account_code) REFERENCES accounts);

CREATE TABLE bank_transactions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES companies ON DELETE CASCADE,
  bank_account_id uuid NOT NULL REFERENCES bank_accounts,
  tx_date date NOT NULL,
  label text NOT NULL,
  amount bigint NOT NULL CHECK (amount <> 0),          -- + entrée, - sortie
  dedup_hash text NOT NULL,
  is_duplicate boolean NOT NULL DEFAULT false,
  status text NOT NULL DEFAULT 'classée' CHECK (status IN ('classée','validée','rejetée')),
  debit_code text, credit_code text,
  rule_label text, explanation text, confidence text,
  matched_invoice_id uuid REFERENCES invoices,
  entry_id uuid REFERENCES journal_entries,
  created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE budgets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES companies ON DELETE CASCADE,
  department text NOT NULL, year int NOT NULL,
  budget bigint NOT NULL CHECK (budget >= 0), spent bigint NOT NULL DEFAULT 0 CHECK (spent >= 0),
  UNIQUE (company_id, department, year));

CREATE TABLE audit_log (
  id bigserial PRIMARY KEY,
  company_id uuid, table_name text NOT NULL, row_id text, action text NOT NULL,
  old_data jsonb, new_data jsonb, actor text,
  at timestamptz NOT NULL DEFAULT now());

CREATE INDEX ON bank_transactions (company_id, status);
CREATE INDEX ON bank_transactions (company_id, dedup_hash);
CREATE INDEX ON entry_lines (company_id, account_code);
CREATE INDEX ON entry_lines (entry_id);
CREATE INDEX ON journal_entries (company_id, entry_date);

-- 1) Une écriture validée est immuable ; on la corrige par contrepassation.
--    À la validation, débit = crédit est vérifié par la base elle-même.
CREATE FUNCTION trg_entry_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE d bigint; c bigint;
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status = 'validated' THEN RAISE EXCEPTION 'Écriture validée : suppression interdite'; END IF;
    RETURN OLD;
  END IF;
  IF OLD.status = 'validated' THEN
    RAISE EXCEPTION 'Écriture validée : modification interdite (utiliser la contrepassation)';
  END IF;
  IF NEW.status = 'validated' THEN
    SELECT COALESCE(sum(debit),0), COALESCE(sum(credit),0) INTO d, c FROM entry_lines WHERE entry_id = NEW.id;
    IF d <> c OR d = 0 THEN RAISE EXCEPTION 'Écriture déséquilibrée (débit %, crédit %)', d, c; END IF;
    NEW.validated_at := now();
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER entry_guard BEFORE UPDATE OR DELETE ON journal_entries FOR EACH ROW EXECUTE FUNCTION trg_entry_guard();

CREATE FUNCTION trg_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE eid uuid := CASE WHEN TG_OP = 'DELETE' THEN OLD.entry_id ELSE NEW.entry_id END;
BEGIN
  IF EXISTS (SELECT 1 FROM journal_entries WHERE id = eid AND status = 'validated') THEN
    RAISE EXCEPTION 'Écriture validée : lignes non modifiables';
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;
CREATE TRIGGER line_guard BEFORE INSERT OR UPDATE OR DELETE ON entry_lines FOR EACH ROW EXECUTE FUNCTION trg_line_guard();

-- 2) Journal d'audit alimenté par la base (l'application ne peut pas l'oublier) ; en lecture seule
CREATE FUNCTION trg_audit() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r jsonb := to_jsonb(CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END);
BEGIN
  INSERT INTO audit_log(company_id, table_name, row_id, action, old_data, new_data, actor)
  VALUES ((r->>'company_id')::uuid, TG_TABLE_NAME, r->>'id', TG_OP,
          CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END,
          CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END,
          current_setting('app.user_id', true));
  RETURN NULL;
END $$;
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON journal_entries FOR EACH ROW EXECUTE FUNCTION trg_audit();
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON entry_lines FOR EACH ROW EXECUTE FUNCTION trg_audit();
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON bank_transactions FOR EACH ROW EXECUTE FUNCTION trg_audit();
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON invoices FOR EACH ROW EXECUTE FUNCTION trg_audit();
CREATE TRIGGER audit AFTER INSERT OR UPDATE OR DELETE ON accounts FOR EACH ROW EXECUTE FUNCTION trg_audit();

CREATE FUNCTION trg_readonly() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Journal d''audit en lecture seule'; END $$;
CREATE TRIGGER audit_ro BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION trg_readonly();
