"""Ostolaskudatan lataus DuckDB:hen.

Lähde: Tutkihankintoja.fi-palvelun data (valtion ja kuntien ostolaskut), avoindata.fi.
Yksi rivi = yksi tiliöintirivi. Laskun summa = SUM(tiliointisumma) per lasku_id.

Lataus on muotoa kohtaan joustava, koska julkaisumuoto on vaihdellut
(sarkain -> puolipiste 3.12.2025). Erotin, desimaalipilkku ja päivämäärämuoto
tunnistetaan automaattisesti, ja jäsennysvirheet raportoidaan yhteenvedossa.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

COLUMNS = [
    "lasku_id",
    "hankintayksikko",
    "hankintayksikko_tunnus",
    "ylaorganisaatio",
    "ylaorganisaatio_tunnus",
    "toimittaja_y_tunnus",
    "toimittaja_nimi",
    "tili",
    "hankintakategoria",
    "tuote_palveluryhma",
    "tositepvm",
    "tiliointisumma",
    "sektori",
]
TEXT_COLUMNS = [c for c in COLUMNS if c not in ("tositepvm", "tiliointisumma")]


def _sql_str(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def read_header(path: Path, encoding: str = "utf-8") -> str:
    enc = "utf-8-sig" if encoding.lower().replace("_", "-") in ("utf-8", "utf8") else encoding
    with open(path, encoding=enc, errors="replace") as f:
        return f.readline()


def sniff_delimiter(header: str) -> str:
    counts = {d: header.count(d) for d in (";", "\t", ",")}
    delim = max(counts, key=counts.get)
    if counts[delim] == 0:
        raise ValueError("Erotinta ei tunnistettu otsikkorivistä")
    return delim


def check_columns(header: str, delim: str, path: Path) -> None:
    found = [c.strip().strip('"').lower() for c in header.strip().split(delim)]
    missing = [c for c in COLUMNS if c not in found]
    if missing:
        raise ValueError(
            f"{path.name}: puuttuvat sarakkeet {missing}. Löytyi: {found}. "
            "Päivitä COLUMNS-lista vastaamaan tiedoston otsikkoa."
        )


def _read_csv_sql(path: Path, delim: str, encoding: str) -> str:
    cols = ", ".join(f'"{c}"' for c in COLUMNS)
    return (
        f"SELECT {cols} FROM read_csv({_sql_str(str(path))}, delim={_sql_str(delim)}, "
        f"header=true, all_varchar=true, quote='\"', encoding={_sql_str(encoding)}, "
        "normalize_names=true, null_padding=true)"
    )


# Desimaalipilkku ("1 234,56" / "1.234,56") ja piste ("1234.56") molemmat käyvät.
_SUM_EXPR = """
TRY_CAST(
  CASE WHEN summa_txt LIKE '%,%'
       THEN replace(replace(summa_txt, '.', ''), ',', '.')
       ELSE summa_txt END
AS DOUBLE)
"""
_DATE_EXPR = """
COALESCE(
  TRY_STRPTIME(pvm_txt, '%Y-%m-%d'),
  TRY_STRPTIME(pvm_txt, '%d.%m.%Y'),
  TRY_STRPTIME(left(pvm_txt, 10), '%Y-%m-%d'),
  TRY_STRPTIME(left(pvm_txt, 10), '%d.%m.%Y')
)::DATE
"""

# Tunnukset, jotka eivät yksilöi toimittajaa (anonymisoidut, ulkomaiset ilman y-tunnusta, paikkamerkit).
# Ne muutetaan NULLiksi, muuten ne kasautuisivat yhdeksi jättitoimittajaksi.
PLACEHOLDER_SUPPLIER_IDS = ("NA", "1234567-1", "FIVEROTON")

# Sama toimittaja esiintyy muodoissa 0201256-6 ja FI02012566 (ALV-tunniste) -> yhtenäistetään y-tunnukseksi.
_YTUNNUS_EXPR = f"""
CASE
  WHEN toimittaja_y_tunnus IN ({", ".join(_sql_str(v) for v in PLACEHOLDER_SUPPLIER_IDS)}) THEN NULL
  WHEN regexp_full_match(toimittaja_y_tunnus, 'FI[0-9]{{8}}')
    THEN substr(toimittaja_y_tunnus, 3, 7) || '-' || substr(toimittaja_y_tunnus, 10, 1)
  ELSE toimittaja_y_tunnus
END
"""


def load_csv(files: list[str | Path], db_path: str | Path, encoding: str = "utf-8") -> dict:
    """Lataa CSV-tiedostot tauluun `laskut` (korvaa aiemman). Palauttaa yhteenvedon."""
    paths = [Path(f) for f in files]
    if not paths:
        raise ValueError("Anna vähintään yksi CSV-tiedosto")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute("DROP TABLE IF EXISTS laskut_stage")
        for i, path in enumerate(paths):
            header = read_header(path, encoding)
            delim = sniff_delimiter(header)
            check_columns(header, delim, path)
            src = _read_csv_sql(path, delim, encoding)
            if i == 0:
                con.execute(f"CREATE TABLE laskut_stage AS {src}")
            else:
                con.execute(f"INSERT INTO laskut_stage {src}")

        text_cols = ", ".join(f"NULLIF(trim({c}), '') AS {c}" for c in TEXT_COLUMNS)
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE typed AS
            WITH r AS (
              SELECT {text_cols},
                     trim(tositepvm) AS pvm_txt,
                     replace(replace(replace(trim(tiliointisumma), ' ', ''), chr(160), ''), '−', '-') AS summa_txt
              FROM laskut_stage
            )
            SELECT * REPLACE ({_YTUNNUS_EXPR} AS toimittaja_y_tunnus),
                   {_DATE_EXPR} AS tositepvm, {_SUM_EXPR} AS tiliointisumma
            FROM r
        """)
        bad_dates, bad_sums, no_supplier_id = con.execute("""
            SELECT count(*) FILTER (WHERE tositepvm IS NULL AND pvm_txt IS NOT NULL AND pvm_txt <> ''),
                   count(*) FILTER (WHERE tiliointisumma IS NULL AND summa_txt IS NOT NULL AND summa_txt <> ''),
                   count(*) FILTER (WHERE toimittaja_y_tunnus IS NULL)
            FROM typed
        """).fetchone()
        con.execute("""
            CREATE OR REPLACE TABLE laskut AS
            SELECT * EXCLUDE (pvm_txt, summa_txt),
                   date_trunc('month', tositepvm)::DATE AS kuukausi
            FROM typed
            WHERE tositepvm IS NOT NULL AND tiliointisumma IS NOT NULL
        """)
        con.execute("DROP TABLE laskut_stage")
        summary = summarize(con)
        summary.update({
            "tiedostot": [p.name for p in paths],
            "hylatyt_paivamaarat": bad_dates,
            "hylatyt_summat": bad_sums,
            "toimittajatunnus_puuttuu": no_supplier_id,
        })
        return summary
    finally:
        con.close()


def summarize(con: duckdb.DuckDBPyConnection) -> dict:
    rows, laskuja, yksikoita, toimittajia, alku, loppu, eur = con.execute("""
        SELECT count(*), count(DISTINCT lasku_id), count(DISTINCT hankintayksikko_tunnus),
               count(DISTINCT toimittaja_y_tunnus), min(tositepvm), max(tositepvm),
               round(sum(tiliointisumma), 2)
        FROM laskut
    """).fetchone()
    return {
        "riveja": rows,
        "laskuja": laskuja,
        "hankintayksikoita": yksikoita,
        "toimittajia": toimittajia,
        "alku": str(alku) if alku else None,
        "loppu": str(loppu) if loppu else None,
        "yhteensa_eur": eur,
    }


def export_parquet(db_path: str | Path, out: str | Path) -> dict:
    """Vie taulun laskut Parquet-tiedostoksi demoa varten (vuoden CSV 790 Mt -> noin 40 Mt)."""
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        con.execute(
            f"COPY (SELECT * FROM laskut ORDER BY kuukausi, hankintayksikko_tunnus) "
            f"TO {_sql_str(str(out))} (FORMAT parquet, COMPRESSION zstd)"
        )
        return summarize(con)
    finally:
        con.close()


def load_parquet(path: str | Path, db_path: str | Path) -> dict:
    """Rakentaa tietokannan export_parquet-tiedostosta. Data on jo siivottu, joten tarkistuksia ei tarvita.

    Kirjoitetaan ensin väliaikaiseen tiedostoon, jotta keskeytynyt rakennus ei jätä puolikasta kantaa.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_name(db_path.name + ".building")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    try:
        con.execute(f"CREATE TABLE laskut AS SELECT * FROM read_parquet({_sql_str(str(path))})")
        summary = summarize(con)
    finally:
        con.close()
    tmp.replace(db_path)
    return summary
