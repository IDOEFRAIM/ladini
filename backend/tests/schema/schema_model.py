"""Modèle normalisé d'un schéma PostgreSQL + 3 extracteurs + comparateur.

Trois sources décrivent le MÊME schéma et doivent rester identiques :

  drizzle     : snapshot Drizzle (`drizzle/meta/NNNN_snapshot.json`) — SOURCE DE VÉRITÉ
  sqlalchemy  : `Base.metadata` (modèles Python) — miroir pour les tables utilisées par le backend
  postgres    : catalogue d'une base réelle reconstruite depuis les migrations

Le comparateur retourne des `Divergence` (table, colonne, aspect, valeur par
source, gravité) — jamais un simple booléen — pour que le test produise un
rapport lisible.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

Key = tuple[str, str]  # (schema, table)

# ── Types ───────────────────────────────────────────────────────────────────

_TYPE_ALIASES = {
    "character varying": "varchar",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "double precision": "float8",
    "float": "float8",
    "real": "float4",
    "integer": "int4",
    "int": "int4",
    "bigint": "int8",
    "smallint": "int2",
    "boolean": "bool",
    "character": "char",
}


def norm_type(raw: str) -> str:
    t = raw.strip().lower()
    array = t.endswith("[]") or t.startswith("array")
    t = t.removesuffix("[]")
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\(\s*(\d+)\s*,\s*(\d+)\s*\)", r"(\1,\2)", t)
    # varchar(255) / varchar sans longueur : la longueur est un détail
    # d'affichage tant qu'aucune contrainte métier ne s'y appuie.
    t = re.sub(r"^(character varying|varchar)\(\d+\)$", "varchar", t)
    t = _TYPE_ALIASES.get(t, t)
    # SQLAlchemy Float(24) → FLOAT(24) : c'est `real` (float4) dans PostgreSQL.
    m = re.fullmatch(r"float\((\d+)\)", t)
    if m:
        t = "float4" if int(m.group(1)) <= 24 else "float8"
    return t + ("[]" if array else "")


# ── Défauts ─────────────────────────────────────────────────────────────────

def norm_default(raw: Any) -> str | None:
    """Réduit un défaut SQL à sa substance (sans cast ni parenthèses)."""
    if raw is None:
        return None
    s = str(raw).strip()
    # PostgreSQL ajoute des casts : 'PENDING'::character varying, '{}'::text[]
    s = re.sub(r"::[a-z_ ]+(\[\])?", "", s, flags=re.I)
    s = s.strip()
    while s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
    low = s.lower()
    if low in {"now()", "current_timestamp", "current_timestamp()", "now"}:
        return "now()"
    if low in {"gen_random_uuid()", "uuid_generate_v4()"}:
        return "gen_random_uuid()"
    if low in {"true", "false"}:
        return low
    if re.fullmatch(r"-?\d+(\.\d+)?", low):
        return str(float(low)).rstrip("0").rstrip(".") if "." in low else low
    if s.startswith("'") and s.endswith("'"):
        return s  # littéral texte, casse préservée
    if low in {"'{}'", "'[]'", "'{}'::jsonb"}:
        return low
    return low


def norm_where(raw: Any) -> str | None:
    """Prédicat d'index partiel réduit à sa substance (sans qualificatifs, casts, parenthèses)."""
    if raw is None:
        return None
    w = str(raw)
    w = re.sub(r'"[A-Za-z_]+"\."[A-Za-z_]+"\.', "", w)  # "schema"."table".col
    w = re.sub(r"::[a-z_ ]+", "", w, flags=re.I)
    w = w.replace('"', "").replace("(", " ").replace(")", " ")
    return re.sub(r"\s+", " ", w).strip().lower() or None


# ── Modèle ─────────────────────────────────────────────────────────────────

@dataclass
class Col:
    type: str
    nullable: bool
    default: str | None = None


@dataclass
class FK:
    cols: tuple[str, ...]
    ref: Key
    refcols: tuple[str, ...]
    ondelete: str = "NO ACTION"
    onupdate: str = "NO ACTION"


@dataclass
class Idx:
    cols: tuple[str, ...]
    unique: bool = False
    where: str | None = None
    method: str = "btree"


@dataclass
class Table:
    cols: dict[str, Col] = field(default_factory=dict)
    pk: tuple[str, ...] = ()
    fks: dict[tuple[str, ...], FK] = field(default_factory=dict)
    uniques: set[tuple[str, ...]] = field(default_factory=set)  # contraintes + index uniques non partiels
    indexes: dict[str, Idx] = field(default_factory=dict)
    checks: set[str] = field(default_factory=set)  # NOMS des contraintes CHECK (le comportement est testé sur PostgreSQL)


Schema = dict[Key, Table]


def _act(x: str | None) -> str:
    return (x or "no action").upper().replace("_", " ")


# ── Extracteur Drizzle ─────────────────────────────────────────────────────

def load_drizzle(snapshot: Path | str) -> Schema:
    data = json.loads(Path(snapshot).read_text(encoding="utf-8"))
    out: Schema = {}
    for t in data["tables"].values():
        tb = Table()
        for name, c in t["columns"].items():
            tb.cols[name] = Col(norm_type(c["type"]), not c.get("notNull", False), norm_default(c.get("default")))
            if c.get("primaryKey"):
                tb.pk = tb.pk + (name,)
        for pk in (t.get("compositePrimaryKeys") or {}).values():
            tb.pk = tuple(pk["columns"])
        for fk in t.get("foreignKeys", {}).values():
            f = FK(tuple(fk["columnsFrom"]), (fk.get("schemaTo") or "public", fk["tableTo"]),
                   tuple(fk["columnsTo"]), _act(fk.get("onDelete")), _act(fk.get("onUpdate")))
            tb.fks[f.cols] = f
        for ck in (t.get("checkConstraints") or {}).values():
            tb.checks.add(ck["name"])
        for uc in (t.get("uniqueConstraints") or {}).values():
            tb.uniques.add(tuple(uc["columns"]))
        for iname, ix in t.get("indexes", {}).items():
            cols = tuple(c["expression"] for c in ix["columns"])
            tb.indexes[iname] = Idx(cols, bool(ix.get("isUnique")), norm_where(ix.get("where")), ix.get("method", "btree"))
            if ix.get("isUnique") and not ix.get("where"):
                tb.uniques.add(cols)
        out[(t.get("schema") or "public", t["name"])] = tb
    return out


# ── Extracteur SQLAlchemy ──────────────────────────────────────────────────

def load_sqlalchemy(metadata) -> Schema:
    from sqlalchemy import CheckConstraint, Index, UniqueConstraint
    from sqlalchemy.dialects import postgresql

    dialect = postgresql.dialect()
    out: Schema = {}
    for t in metadata.tables.values():
        tb = Table()
        for c in t.columns:
            d = None
            if c.server_default is not None:
                arg = getattr(c.server_default, "arg", None)
                d = norm_default(getattr(arg, "text", None) or (f"'{arg}'" if isinstance(arg, str) else arg))
            tb.cols[c.name] = Col(norm_type(c.type.compile(dialect=dialect)), bool(c.nullable), d)
        tb.pk = tuple(c.name for c in t.primary_key.columns)
        for fk in t.foreign_key_constraints:
            e = fk.elements[0].column.table
            f = FK(tuple(c.name for c in fk.columns), (e.schema or "public", e.name),
                   tuple(x.column.name for x in fk.elements), _act(fk.ondelete), _act(fk.onupdate))
            tb.fks[f.cols] = f
        for c in t.constraints:
            if isinstance(c, CheckConstraint) and c.name:
                tb.checks.add(c.name)
            if isinstance(c, UniqueConstraint):
                tb.uniques.add(tuple(x.name for x in c.columns))
        for c in t.columns:
            if c.unique:
                tb.uniques.add((c.name,))
        for i in t.indexes:
            assert isinstance(i, Index)
            where = i.dialect_options["postgresql"].get("where")
            method = i.dialect_options["postgresql"].get("using") or "btree"
            cols = tuple(getattr(e, "name", None) or str(e) for e in i.expressions)
            tb.indexes[i.name] = Idx(cols, bool(i.unique), norm_where(where), method)
            if i.unique and where is None:
                tb.uniques.add(cols)
        out[(t.schema or "public", t.name)] = tb
    return out


# ── Extracteur PostgreSQL ──────────────────────────────────────────────────

_SCHEMAS = ("public", "auth", "governance", "marketplace", "intelligence")

_Q_COLS = """
select n.nspname, c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
       not a.attnotnull, pg_get_expr(d.adbin, d.adrelid)
from pg_attribute a join pg_class c on c.oid=a.attrelid join pg_namespace n on n.oid=c.relnamespace
left join pg_attrdef d on d.adrelid=a.attrelid and d.adnum=a.attnum
where c.relkind in ('r','p') and a.attnum>0 and not a.attisdropped and n.nspname = any(%s)
order by 1,2,a.attnum"""

_Q_CONS = """
select n.nspname, c.relname, co.contype, co.conkey, co.confkey, co.confrelid::regclass::text,
       co.confdeltype, co.confupdtype, c.oid, co.confrelid, co.conname
from pg_constraint co join pg_class c on c.oid=co.conrelid join pg_namespace n on n.oid=c.relnamespace
where co.contype in ('p','u','f','c') and n.nspname = any(%s)"""

_Q_IDX = """
select n.nspname, c.relname, c.oid, i.relname, ix.indisunique, ix.indkey::int2[], pg_get_expr(ix.indpred, ix.indrelid),
       am.amname, ix.indisprimary, ix.indexrelid in (select conindid from pg_constraint where contype in ('u','p'))
from pg_index ix join pg_class c on c.oid=ix.indrelid join pg_class i on i.oid=ix.indexrelid
join pg_namespace n on n.oid=c.relnamespace join pg_am am on am.oid=i.relam
where n.nspname = any(%s)"""

_Q_ATTNUM = """
select c.oid, a.attnum, a.attname from pg_attribute a join pg_class c on c.oid=a.attrelid
join pg_namespace n on n.oid=c.relnamespace where a.attnum>0 and not a.attisdropped and n.nspname = any(%s)"""

_ACTION = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}


def load_postgres(conn, schemas: Iterable[str] = _SCHEMAS) -> Schema:
    """`conn` : connexion psycopg/psycopg2/asyncpg-sync-like exposant `.cursor()` (paramstyle %s)."""
    schemas = list(schemas)
    cur = conn.cursor()
    out: Schema = {}

    def q(sql):
        cur.execute(sql, (schemas,))
        return cur.fetchall()

    for s, t, col, typ, nullable, default in q(_Q_COLS):
        out.setdefault((s, t), Table()).cols[col] = Col(norm_type(typ), bool(nullable), norm_default(default))
    names: dict[tuple[int, int], str] = {(o, n): a for o, n, a in q(_Q_ATTNUM)}

    def cols_of(oid, nums):
        return tuple(names[(oid, n)] for n in nums)

    for s, t, ty, conkey, confkey, confrel, deltype, updtype, oid, confrelid, conname in q(_Q_CONS):
        tb = out[(s, t)]
        if ty == "c":
            tb.checks.add(conname)
            continue
        cols = cols_of(oid, conkey)
        if ty == "p":
            tb.pk = cols
        elif ty == "u":
            tb.uniques.add(cols)
        else:
            ref = confrel.replace('"', "")
            rs, rt = ref.split(".", 1) if "." in ref else ("public", ref)
            tb.fks[cols] = FK(cols, (rs, rt), cols_of(confrelid, confkey), _ACTION[deltype], _ACTION[updtype])
    for s, t, oid, name, uniq, indkey, pred, method, primary, from_con in q(_Q_IDX):
        tb = out[(s, t)]
        cols = tuple(names[(oid, k)] if k > 0 else "<expr>" for k in indkey)
        if primary or from_con:
            continue  # déjà représenté par pk/uniques
        tb.indexes[name] = Idx(cols, bool(uniq), norm_where(pred), method)
        if uniq and not pred:
            tb.uniques.add(cols)
    return out


# ── Comparateur ────────────────────────────────────────────────────────────

@dataclass
class Divergence:
    table: str
    column: str
    aspect: str
    values: dict[str, Any]
    severity: str = "critical"

    def line(self) -> str:
        v = " | ".join(f"{k}={val}" for k, val in self.values.items())
        return f"[{self.severity}] {self.table:<40} {self.column:<28} {self.aspect:<10} {v}"


def _absent(v):
    return "∅" if v is None else v


def compare(a: Schema, b: Schema, an: str, bn: str, *, only_tables: Iterable[Key] | None = None,
            ignore_tables: Iterable[Key] = (), check_indexes: bool = True) -> list[Divergence]:
    """Compare `a` (référence) à `b`. Tables absentes d'un côté = divergence `table`."""
    out: list[Divergence] = []
    ign = set(ignore_tables)
    keys = set(a) | set(b)
    if only_tables is not None:
        keys &= set(only_tables)
    for k in sorted(keys - ign):
        tn = ".".join(k)
        if k not in a or k not in b:
            out.append(Divergence(tn, "*", "table", {an: "présent" if k in a else "ABSENT", bn: "présent" if k in b else "ABSENT"}))
            continue
        x, y = a[k], b[k]
        for c in sorted(set(x.cols) | set(y.cols)):
            if c not in x.cols or c not in y.cols:
                out.append(Divergence(tn, c, "column", {an: "présente" if c in x.cols else "ABSENTE", bn: "présente" if c in y.cols else "ABSENTE"}))
                continue
            p, q = x.cols[c], y.cols[c]
            if p.type != q.type:
                out.append(Divergence(tn, c, "type", {an: p.type, bn: q.type}))
            if p.nullable != q.nullable:
                out.append(Divergence(tn, c, "nullable", {an: p.nullable, bn: q.nullable}))
            if p.default != q.default:
                out.append(Divergence(tn, c, "default", {an: _absent(p.default), bn: _absent(q.default)}, "warning"))
        if x.pk != y.pk:
            out.append(Divergence(tn, "*", "pk", {an: x.pk, bn: y.pk}))
        for cols in sorted(set(x.fks) | set(y.fks)):
            f, g = x.fks.get(cols), y.fks.get(cols)
            cn = ",".join(cols)
            if f is None or g is None:
                out.append(Divergence(tn, cn, "fk", {an: f"→{'.'.join(f.ref)}" if f else "ABSENTE", bn: f"→{'.'.join(g.ref)}" if g else "ABSENTE"}))
            else:
                if (f.ref, f.refcols) != (g.ref, g.refcols):
                    out.append(Divergence(tn, cn, "fk-target", {an: (f.ref, f.refcols), bn: (g.ref, g.refcols)}))
                if f.ondelete != g.ondelete:
                    out.append(Divergence(tn, cn, "ondelete", {an: f.ondelete, bn: g.ondelete}))
                if f.onupdate != g.onupdate:
                    out.append(Divergence(tn, cn, "onupdate", {an: f.onupdate, bn: g.onupdate}))
        for ck in sorted(x.checks ^ y.checks):
            out.append(Divergence(tn, ck, "check", {an: "oui" if ck in x.checks else "ABSENTE", bn: "oui" if ck in y.checks else "ABSENTE"}))
        for u in sorted(x.uniques ^ y.uniques):
            out.append(Divergence(tn, ",".join(u), "unique", {an: "oui" if u in x.uniques else "ABSENTE", bn: "oui" if u in y.uniques else "ABSENTE"}))
        if check_indexes:
            # Comparaison par (colonnes, unique, where) — les NOMS d'index ne comptent pas.
            sx = {(i.cols, i.unique, i.where, i.method) for i in x.indexes.values() if not (i.unique and not i.where)}
            sy = {(i.cols, i.unique, i.where, i.method) for i in y.indexes.values() if not (i.unique and not i.where)}
            for sig in sorted(sx ^ sy, key=str):
                out.append(Divergence(tn, ",".join(sig[0]), "index", {an: "oui" if sig in sx else "ABSENT", bn: "oui" if sig in sy else "ABSENT"}, "warning"))
    return out


def render(divs: list[Divergence], title: str) -> str:
    if not divs:
        return f"{title}: aucune divergence."
    crit = [d for d in divs if d.severity == "critical"]
    warn = [d for d in divs if d.severity != "critical"]
    lines = [f"{title}: {len(crit)} critique(s), {len(warn)} avertissement(s)"]
    lines += [d.line() for d in crit + warn]
    return "\n".join(lines)


# ── Matrice unifiée (3 sources côte à côte) ────────────────────────────────

@dataclass
class Row:
    table: str
    item: str
    drizzle: str
    sqlalchemy: str
    postgres: str
    status: str
    action: str


def _fmt_col(c: Col | None) -> str:
    if c is None:
        return "ABSENT"
    return f"{c.type} {'NULL' if c.nullable else 'NOT NULL'}" + (f" DEFAULT {c.default}" if c.default else "")


def _fmt_fk(f: FK | None) -> str:
    return "ABSENTE" if f is None else f"→{'.'.join(f.ref)}({','.join(f.refcols)}) del={f.ondelete} upd={f.onupdate}"


def _index_sigs(t: Table) -> set:
    return {(i.cols, i.unique, i.where, i.method) for i in t.indexes.values() if not (i.unique and not i.where)}


def build_matrix(D: Schema, S: Schema, P: Schema) -> tuple[list[Row], int]:
    """Retourne (lignes divergentes, nombre d'éléments identiques)."""
    rows: list[Row] = []
    ok = 0
    for k in sorted(set(D) | set(S) | set(P)):
        tn = ".".join(k)
        d, s, p = D.get(k), S.get(k), P.get(k)
        present = f"D={'✓' if d else '✗'} S={'✓' if s else '✗'} P={'✓' if p else '✗'}"
        if not (d and s and p):
            if s and not d:
                act = "Modèle sans table Drizzle : supprimer le modèle (mort) OU déclarer la table dans Drizzle"
            elif d and not s:
                act = "Table Drizzle sans modèle Python : OK si non utilisée par Python (seed/site), sinon ajouter le miroir"
            else:
                act = "Rejouer les migrations / corriger la source manquante"
            rows.append(Row(tn, "(table)", "présente" if d else "ABSENTE", "présente" if s else "ABSENTE",
                            "présente" if p else "ABSENTE", "DIVERGENT", f"{act}  [{present}]"))
            continue
        for c in sorted(set(d.cols) | set(s.cols) | set(p.cols)):
            cd, cs, cp = d.cols.get(c), s.cols.get(c), p.cols.get(c)
            vals = [_fmt_col(x) for x in (cd, cs, cp)]
            if len(set(vals)) == 1:
                ok += 1
                continue
            kind = "type" if len({x.type for x in (cd, cs, cp) if x}) > 1 or not (cd and cs and cp) else (
                "nullable" if len({x.nullable for x in (cd, cs, cp)}) > 1 else "default")
            act = {"type": "Aligner le type sur Drizzle (source de vérité)",
                   "nullable": "Aligner la nullabilité sur Drizzle",
                   "default": "Ajouter server_default côté SQLAlchemy (ou retirer côté Drizzle si inutile)"}[kind]
            rows.append(Row(tn, f"col:{c}", vals[0], vals[1], vals[2], "DIVERGENT", act))
        for cols in sorted(set(d.fks) | set(s.fks) | set(p.fks)):
            fd, fs, fp = d.fks.get(cols), s.fks.get(cols), p.fks.get(cols)
            vals = [_fmt_fk(x) for x in (fd, fs, fp)]
            if len(set(vals)) == 1:
                ok += 1
                continue
            rows.append(Row(tn, f"fk:{','.join(cols)}", *vals, "DIVERGENT",
                            "Classer A/B/C : A = ajouter dans Drizzle + migration ; B = retirer ForeignKey du modèle ; C = supprimer"))
        for ck in sorted(d.checks | s.checks | p.checks):
            vals = ["oui" if ck in x.checks else "ABSENTE" for x in (d, s, p)]
            if len(set(vals)) == 1:
                ok += 1
                continue
            rows.append(Row(tn, f"check:{ck}", *vals, "DIVERGENT", "Aligner sur Drizzle"))
        for u in sorted(d.uniques | s.uniques | p.uniques):
            vals = ["oui" if u in x.uniques else "ABSENTE" for x in (d, s, p)]
            if len(set(vals)) == 1:
                ok += 1
                continue
            rows.append(Row(tn, f"unique:{','.join(u)}", *vals, "DIVERGENT", "Aligner sur Drizzle"))
        sd, ss, sp = _index_sigs(d), _index_sigs(s), _index_sigs(p)
        for x in sorted(sd | ss | sp, key=str):
            vals = ["oui" if x in y else "ABSENT" for y in (sd, ss, sp)]
            if len(set(vals)) == 1:
                ok += 1
                continue
            rows.append(Row(tn, f"index:{','.join(x[0])}{' (partiel)' if x[2] else ''}", *vals, "DIVERGENT",
                            "Index : Drizzle est la source ; côté SQLAlchemy miroir, ou retirer si sans requête"))
    return rows, ok


def _esc(x) -> str:
    return str(x).replace("|", r"\|")


def matrix_markdown(rows: list[Row], ok: int, title: str) -> str:
    esc = _esc
    out = [f"# {title}", "", f"Éléments identiques sur les 3 sources : **{ok}** — divergents : **{len(rows)}**", "",
           "| table | élément | drizzle | sqlalchemy | postgres | statut | action recommandée |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {esc(r.table)} | {esc(r.item)} | {esc(r.drizzle)} | {esc(r.sqlalchemy)} | {esc(r.postgres)} | {r.status} | {esc(r.action)} |")
    return "\n".join(out) + "\n"
