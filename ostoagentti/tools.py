"""Agentin työkalut. Kaikki laskenta tapahtuu täällä deterministisesti – LLM ei laske lukuja.

- describe_schema: taulun rakenne ja perustiedot
- run_sql: vain luku -kysely (yksi SELECT/WITH), riviraja ja aikakatkaisu
- detect_anomalies: kuukausipiikit (robust z), uudet toimittajat, mahdolliset tuplalaskut
"""
from __future__ import annotations

import json
import re
import threading
from datetime import date
from pathlib import Path

import duckdb

MAX_ROWS = 50
SQL_TIMEOUT_S = 20

DEFAULTS = {
    "spike_min_eur": 20_000.0,
    "spike_z": 4.0,
    "spike_floor_eur": 1_000.0,
    "spike_min_active_months": 6,
    "spike_vs_max": 1.5,  # pitää ylittää myös 12 kk korkein kuukausi 1,5-kertaisesti

    "new_min_eur": 20_000.0,
    "new_lookback_months": 24,
    "dup_min_eur": 1_000.0,
    "dup_max_days": 3,
    "limit": 10,
}

# Kenttä -> (avainsarake, näyttönimi)
DIMENSIONS = {
    "toimittaja": ("toimittaja_y_tunnus", "toimittaja_nimi"),
    "hankintakategoria": ("hankintakategoria", "hankintakategoria"),
    "hankintayksikko": ("hankintayksikko_tunnus", "hankintayksikko"),
}

# Salatut/anonymisoidut toimittajat kasautuisivat yhdeksi "toimittajaksi" -> suljetaan pois.
# Useimmilta puuttuu tunnus (data.py muuttaa paikkamerkit NULLiksi); osalla on tunnus, mutta nimi on anonymisoitu.
SUPPLIER_OK = """
toimittaja_y_tunnus IS NOT NULL
AND coalesce(toimittaja_nimi, '') NOT ILIKE '%luottamuksellinen%'
AND coalesce(toimittaja_nimi, '') NOT ILIKE '%salassa pidettävä%'
AND coalesce(toimittaja_nimi, '') NOT ILIKE '%ei julkaista%'
AND coalesce(toimittaja_nimi, '') NOT ILIKE '%ei julkinen%'
AND coalesce(toimittaja_nimi, '') NOT ILIKE '%yksityishenkilö%'
AND coalesce(toimittaja_nimi, '') NOT ILIKE '%yksityinen elinkeinonharjoittaja%'
AND coalesce(toimittaja_nimi, '') NOT ILIKE '%anonymisoitu%'
AND coalesce(toimittaja_nimi, '') NOT ILIKE '%confidential%'
AND coalesce(toimittaja_nimi, '') NOT ILIKE '%not public%'
"""

_FORBIDDEN = re.compile(
    r"\b(attach|detach|copy|export|import|install|load|pragma|set|reset|call|create|insert|"
    r"update|delete|drop|alter|truncate|vacuum|checkpoint|use|getenv|read_csv\w*|read_parquet|"
    r"read_json\w*|read_text|read_blob|glob|sniff_csv|parquet_\w+)\b",
    re.IGNORECASE,
)
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class ToolError(Exception):
    """Virhe, joka palautetaan agentille korjattavaksi."""


def connect_readonly(db_path: str | Path, memory_limit: str | None = None) -> duckdb.DuckDBPyConnection:
    config = {"enable_external_access": False, "lock_configuration": True}
    if memory_limit:
        # Pienellä palvelimella (esim. Streamlit Cloud) DuckDB veisi oletuksena 80 % muistista.
        # lock_configuration estää muuttamasta tätä myöhemmin, joten raja annetaan tässä.
        config["memory_limit"] = memory_limit
    return duckdb.connect(str(db_path), read_only=True, config=config)


def _strip_literals_and_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r'"(?:[^"]|"")*"', '""', sql)
    return sql


def validate_select(sql: str) -> str:
    """Hyväksyy vain yhden SELECT/WITH-lauseen. Palauttaa siistityn SQL:n."""
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise ToolError("Tyhjä kysely")
    bare = _strip_literals_and_comments(cleaned)
    if ";" in bare:
        raise ToolError("Vain yksi lause sallitaan")
    if not re.match(r"^\s*(select|with)\b", bare, re.IGNORECASE):
        raise ToolError("Vain SELECT- tai WITH-kyselyt sallitaan")
    hit = _FORBIDDEN.search(bare)
    if hit:
        raise ToolError(f"Kielletty avainsana tai funktio: {hit.group(0)}")
    return cleaned


def _month_start(month: str) -> date:
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        raise ToolError("Kuukausi muodossa YYYY-MM, esim. 2025-09")
    y, m = month.split("-")
    return date(int(y), int(m), 1)


def _jsonable(value):
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if value is None or isinstance(value, (int, str, bool)):
        return value
    return str(value)


class Tools:
    def __init__(self, db_path: str | Path, max_rows: int = MAX_ROWS, timeout_s: float = SQL_TIMEOUT_S,
                 memory_limit: str | None = None):
        self.con = connect_readonly(db_path, memory_limit)
        self.max_rows = max_rows
        self.timeout_s = timeout_s

    def close(self) -> None:
        self.con.close()

    # --- apu ---------------------------------------------------------------
    def _query(self, sql: str, params: dict | None = None) -> tuple[list[str], list[tuple]]:
        timer = threading.Timer(self.timeout_s, self.con.interrupt)
        timer.start()
        try:
            cur = self.con.execute(sql, params or {})
            cols = [d[0] for d in cur.description]
            return cols, cur.fetchall()
        except duckdb.InterruptException as e:
            raise ToolError(f"Kysely keskeytettiin aikarajan ({self.timeout_s} s) vuoksi") from e
        finally:
            timer.cancel()

    def _records(self, sql: str, params: dict | None = None) -> list[dict]:
        cols, rows = self._query(sql, params)
        return [_jsonable(dict(zip(cols, r))) for r in rows]

    # --- työkalut ----------------------------------------------------------
    def describe_schema(self) -> dict:
        cols = self._records(
            "SELECT column_name AS sarake, data_type AS tyyppi FROM information_schema.columns "
            "WHERE table_name = 'laskut' ORDER BY ordinal_position"
        )
        stats = self._records("""
            SELECT count(*) AS riveja, count(DISTINCT lasku_id) AS laskuja,
                   min(tositepvm) AS alku, max(tositepvm) AS loppu,
                   count(DISTINCT toimittaja_y_tunnus) AS toimittajia,
                   count(DISTINCT hankintayksikko_tunnus) AS hankintayksikoita
            FROM laskut
        """)[0]
        sektorit = [r["sektori"] for r in self._records(
            "SELECT DISTINCT sektori FROM laskut WHERE sektori IS NOT NULL ORDER BY 1 LIMIT 10")]
        kategoriat = [r["hankintakategoria"] for r in self._records(
            "SELECT hankintakategoria, sum(tiliointisumma) s FROM laskut "
            "WHERE hankintakategoria IS NOT NULL GROUP BY 1 ORDER BY s DESC LIMIT 15")]
        return {
            "taulu": "laskut",
            "sarakkeet": cols,
            "tilastot": stats,
            "sektorit": sektorit,
            "suurimmat_hankintakategoriat": kategoriat,
            "huomiot": [
                "Yksi rivi on tiliöintirivi; laskun summa = SUM(tiliointisumma) GROUP BY lasku_id.",
                "kuukausi = tositepvm:n kuukauden ensimmäinen päivä (DATE).",
                "Summat euroja. Negatiiviset rivit ovat hyvityksiä.",
                "Anonymisoiduilla (esim. 'Salassa pidettävä') ja osalla ulkomaisista toimittajista "
                "toimittaja_y_tunnus on NULL. Rajaa ne pois toimittajavertailuista.",
                "toimittaja_y_tunnus on yhtenäistetty muotoon 1234567-8 (FI12345678 muunnettu).",
            ],
        }

    def run_sql(self, sql: str) -> dict:
        cleaned = validate_select(sql)
        wrapped = f"SELECT * FROM ({cleaned}) AS q LIMIT {self.max_rows + 1}"
        try:
            cols, rows = self._query(wrapped)
        except ToolError:
            raise
        except duckdb.Error as e:
            raise ToolError(f"SQL-virhe: {str(e).splitlines()[0]}") from e
        truncated = len(rows) > self.max_rows
        rows = rows[: self.max_rows]
        return {
            "sarakkeet": cols,
            "rivit": [_jsonable(list(r)) for r in rows],
            "row_count": len(rows),
            "katkaistu": truncated,
            "sql": cleaned,
        }

    def detect_anomalies(self, month: str, kind: str = "all", **overrides) -> dict:
        m = _month_start(month)
        p = {**DEFAULTS, **{k: v for k, v in overrides.items() if k in DEFAULTS and v is not None}}
        kinds = {"all", "piikit", "uudet_toimittajat", "tuplat"}
        if kind not in kinds:
            raise ToolError(f"kind on yksi näistä: {sorted(kinds)}")

        data_start = self._query("SELECT min(kuukausi) FROM laskut")[1][0][0]
        history_months = (m.year - data_start.year) * 12 + (m.month - data_start.month) if data_start else 0

        out: dict = {"kuukausi": month, "historiaa_kk": history_months, "parametrit": p, "huomiot": []}
        if kind in ("all", "piikit"):
            if history_months < p["spike_min_active_months"]:
                out["huomiot"].append("Piikkejä ei arvioitu: liian vähän historiaa.")
            else:
                out["piikit"] = {d: self._spikes(m, d, p) for d in ("toimittaja", "hankintakategoria")}
        if kind in ("all", "uudet_toimittajat"):
            if history_months < 12:
                out["huomiot"].append("Uusia toimittajia ei arvioitu: alle 12 kk historiaa.")
            else:
                out["uudet_toimittajat"] = self._new_suppliers(m, p)
        if kind in ("all", "tuplat"):
            out["mahdolliset_tuplat"] = self._duplicates(m, p)
        return out

    # --- tunnistimet -------------------------------------------------------
    def _spikes(self, m: date, dimension: str, p: dict) -> list[dict]:
        key_col, label_col = DIMENSIONS[dimension]
        extra = f"AND {SUPPLIER_OK}" if dimension == "toimittaja" else ""
        sql = f"""
        WITH monthly AS (
          SELECT {key_col} AS avain, max({label_col}) AS nimi, kuukausi, sum(tiliointisumma) AS eur
          FROM laskut
          WHERE kuukausi BETWEEN $m - INTERVAL 12 MONTH AND $m AND {key_col} IS NOT NULL {extra}
          GROUP BY {key_col}, kuukausi
        ),
        keys AS (SELECT avain, max(nimi) AS nimi FROM monthly GROUP BY avain),
        months AS (  -- datan alkua edeltävät kuukaudet ovat tuntemattomia, eivät nollia
          SELECT CAST(generate_series AS DATE) AS kuukausi
          FROM generate_series($m - INTERVAL 12 MONTH, $m - INTERVAL 1 MONTH, INTERVAL 1 MONTH)
          WHERE generate_series >= (SELECT min(kuukausi) FROM laskut)
        ),
        hist AS (
          SELECT k.avain, coalesce(mo.eur, 0) AS eur
          FROM keys k CROSS JOIN months ms
          LEFT JOIN monthly mo ON mo.avain = k.avain AND mo.kuukausi = ms.kuukausi
        ),
        stats AS (
          SELECT avain, median(eur) AS med, mad(eur) AS mad, max(eur) AS maks,
                 count(*) FILTER (WHERE eur > 0) AS aktiivisia_kk
          FROM hist GROUP BY avain
        ),
        cur AS (SELECT avain, eur FROM monthly WHERE kuukausi = $m),
        scored AS (
          SELECT k.avain, k.nimi, c.eur, s.med, s.maks, s.aktiivisia_kk,
                 (c.eur - s.med) / GREATEST(1.4826 * s.mad, 0.1 * s.med, $floor) AS robust_z
          FROM cur c JOIN stats s USING (avain) JOIN keys k USING (avain)
        )
        SELECT avain, nimi, eur AS kuukauden_eur, med AS mediaani_12kk_eur, maks AS maksimi_12kk_eur,
               eur - med AS ylitys_eur, robust_z, aktiivisia_kk
        FROM scored
        WHERE eur >= $min_eur AND aktiivisia_kk >= $min_active AND robust_z >= $z
          AND eur >= $vs_max * maks
        ORDER BY ylitys_eur DESC
        LIMIT $limit
        """
        return self._records(sql, {
            "m": m, "floor": p["spike_floor_eur"], "min_eur": p["spike_min_eur"],
            "min_active": p["spike_min_active_months"], "z": p["spike_z"],
            "vs_max": p["spike_vs_max"], "limit": p["limit"],
        })

    def _new_suppliers(self, m: date, p: dict) -> list[dict]:
        sql = f"""
        WITH cur AS (
          SELECT toimittaja_y_tunnus AS avain, max(toimittaja_nimi) AS nimi,
                 sum(tiliointisumma) AS kuukauden_eur, count(DISTINCT lasku_id) AS laskuja,
                 list(DISTINCT hankintayksikko)[1:3] AS hankintayksikot,
                 max(hankintakategoria) AS hankintakategoria
          FROM laskut WHERE kuukausi = $m AND {SUPPLIER_OK}
          GROUP BY toimittaja_y_tunnus
        ),
        prev AS (
          SELECT DISTINCT toimittaja_y_tunnus AS avain FROM laskut
          WHERE kuukausi >= $m - to_months(CAST($lookback AS INTEGER)) AND kuukausi < $m
            AND toimittaja_y_tunnus IS NOT NULL
        )
        SELECT * FROM cur
        WHERE avain NOT IN (SELECT avain FROM prev) AND kuukauden_eur >= $min_eur
        ORDER BY kuukauden_eur DESC
        LIMIT $limit
        """
        return self._records(sql, {
            "m": m, "lookback": p["new_lookback_months"], "min_eur": p["new_min_eur"], "limit": p["limit"],
        })

    def _duplicates(self, m: date, p: dict) -> list[dict]:
        sql = f"""
        WITH inv AS (
          SELECT lasku_id, hankintayksikko_tunnus, max(hankintayksikko) AS hankintayksikko,
                 toimittaja_y_tunnus, max(toimittaja_nimi) AS toimittaja,
                 min(tositepvm) AS pvm, round(sum(tiliointisumma), 2) AS summa
          FROM laskut
          WHERE tositepvm BETWEEN $m - INTERVAL 7 DAY AND $m + INTERVAL 1 MONTH - INTERVAL 1 DAY
            AND {SUPPLIER_OK}
          GROUP BY lasku_id, hankintayksikko_tunnus, toimittaja_y_tunnus
        )
        SELECT a.toimittaja, a.toimittaja_y_tunnus, a.hankintayksikko, a.summa,
               a.lasku_id AS lasku_a, a.pvm AS pvm_a, b.lasku_id AS lasku_b, b.pvm AS pvm_b
        FROM inv a JOIN inv b
          ON a.toimittaja_y_tunnus = b.toimittaja_y_tunnus
         AND a.hankintayksikko_tunnus = b.hankintayksikko_tunnus
         AND a.summa = b.summa
         AND a.lasku_id < b.lasku_id
         AND abs(date_diff('day', a.pvm, b.pvm)) <= $days
        WHERE a.summa >= $min_eur AND greatest(a.pvm, b.pvm) >= $m
        ORDER BY a.summa DESC
        LIMIT $limit
        """
        return self._records(sql, {
            "m": m, "days": p["dup_max_days"], "min_eur": p["dup_min_eur"], "limit": p["limit"],
        })

    # --- agentin rajapinta -------------------------------------------------
    def call(self, name: str, args: dict) -> dict:
        """Ajaa työkalun. Virheet palautetaan agentille, jotta se voi korjata kyselyn."""
        try:
            if name == "describe_schema":
                return self.describe_schema()
            if name == "run_sql":
                return self.run_sql(str(args.get("sql", "")))
            if name == "detect_anomalies":
                return self.detect_anomalies(str(args.get("month", "")), str(args.get("kind", "all")))
            return {"error": f"Tuntematon työkalu: {name}"}
        except ToolError as e:
            return {"error": str(e)}
        except duckdb.Error as e:
            return {"error": f"Tietokantavirhe: {str(e).splitlines()[0]}"}


TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "describe_schema",
            "description": "Palauttaa taulun laskut sarakkeet, tyypit, aikavälin ja perustilastot.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Ajaa yhden DuckDB SELECT- tai WITH-kyselyn tauluun laskut (vain luku). "
                f"Palauttaa enintään {MAX_ROWS} riviä. Aggregoi tietokannassa, älä hae raakarivejä."
            ),
            "parameters": {
                "type": "object",
                "properties": {"sql": {"type": "string", "description": "DuckDB SQL"}},
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_anomalies",
            "description": (
                "Ajaa deterministiset poikkeamatunnistimet valitulle kuukaudelle: kuukausipiikit "
                "(toimittaja, hankintakategoria; robust z 12 kk mediaaniin ja selvästi yli 12 kk "
                "korkeimman kuukauden), uudet isot toimittajat ja mahdolliset tuplalaskut."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "month": {"type": "string", "description": "YYYY-MM"},
                    "kind": {
                        "type": "string",
                        "enum": ["all", "piikit", "uudet_toimittajat", "tuplat"],
                    },
                },
                "required": ["month"],
            },
        },
    },
]


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def group_duplicate_pairs(pairs: list[dict]) -> list[dict]:
    """Kokoaa tuplatunnistimen parit ryhmiksi näyttöä varten: neljä samaa laskua on yksi rivi, ei kuusi paria."""
    groups: dict[tuple, dict] = {}
    for r in pairs:
        key = (r["toimittaja_y_tunnus"], r["hankintayksikko"], r["summa"])
        g = groups.setdefault(key, {
            "toimittaja": r["toimittaja"], "toimittaja_y_tunnus": r["toimittaja_y_tunnus"],
            "hankintayksikko": r["hankintayksikko"], "summa": r["summa"], "laskut": set(), "paivat": set(),
        })
        g["laskut"].update((r["lasku_a"], r["lasku_b"]))
        g["paivat"].update((r["pvm_a"], r["pvm_b"]))
    out = [{**g, "laskuja": len(g["laskut"]), "laskut": ", ".join(sorted(g["laskut"])),
            "paivat": ", ".join(sorted(g["paivat"]))} for g in groups.values()]
    return sorted(out, key=lambda g: g["summa"] * g["laskuja"], reverse=True)
