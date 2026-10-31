import shutil

import pytest

from ostoagentti.tools import ToolError, Tools, group_duplicate_pairs, validate_select


@pytest.mark.parametrize("sql", [
    "DROP TABLE laskut",
    "SELECT 1; DROP TABLE laskut",
    "COPY laskut TO 'x.csv'",
    "ATTACH 'x.db'",
    "INSTALL httpfs",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT getenv('GEMINI_API_KEY')",
    "PRAGMA database_list",
    "UPDATE laskut SET tili = 'x'",
    "",
])
def test_rejects_non_select(sql):
    with pytest.raises(ToolError):
        validate_select(sql)


def test_accepts_select_with_and_keywords_inside_literals():
    assert validate_select("WITH x AS (SELECT 1 AS a) SELECT a FROM x;")
    assert validate_select("SELECT count(*) FROM laskut WHERE toimittaja_nimi ILIKE '%drop; load%'")
    assert validate_select("select 1 -- ; drop")


def test_external_file_access_blocked_even_without_keyword(tools):
    res = tools.call("run_sql", {"sql": "SELECT * FROM '/etc/passwd'"})
    assert "error" in res


def test_run_sql_truncates_and_reports(tools):
    res = tools.run_sql("SELECT range AS n FROM range(500)")
    assert res["row_count"] == tools.max_rows and res["katkaistu"] is True


def test_sql_error_is_returned_not_raised(tools):
    res = tools.call("run_sql", {"sql": "SELECT ei_ole FROM laskut"})
    assert res["error"].startswith("SQL-virhe")


def test_timeout_interrupts_long_query(synth):
    t = Tools(synth["db"], timeout_s=0.3)
    try:
        res = t.call("run_sql", {"sql": "SELECT sum(a.range * b.range) FROM range(200000) a, range(200000) b"})
        assert "aikarajan" in res["error"]
        assert t.run_sql("SELECT 1 AS ok")["rivit"] == [[1]]  # yhteys toimii keskeytyksen jälkeen
    finally:
        t.close()


def test_describe_schema(tools):
    s = tools.describe_schema()
    names = [c["sarake"] for c in s["sarakkeet"]]
    assert {"tiliointisumma", "kuukausi", "toimittaja_y_tunnus"} <= set(names)
    assert s["tilastot"]["alku"] == "2023-01-01"


def test_unknown_tool_and_bad_month(tools):
    assert "error" in tools.call("poista_kaikki", {})
    assert "YYYY-MM" in tools.call("detect_anomalies", {"month": "syyskuu"})["error"]


def test_memory_limit_is_applied(synth, tmp_path):
    # Oma kopio: samaan tiedostoon ei voi avata yhteyttä eri asetuksilla samassa prosessissa.
    db = shutil.copy(synth["db"], tmp_path / "m.duckdb")
    t = Tools(db, memory_limit="256MB")
    try:
        assert t.run_sql("SELECT current_setting('memory_limit')")["rivit"][0][0].startswith("244")
    finally:
        t.close()


def test_duplicate_pairs_are_grouped():
    base = {"toimittaja": "A Oy", "toimittaja_y_tunnus": "1", "hankintayksikko": "Y", "summa": 100.0,
            "pvm_a": "2025-12-31", "pvm_b": "2025-12-31"}
    pairs = [{**base, "lasku_a": a, "lasku_b": b}
             for a, b in [("L1", "L2"), ("L1", "L3"), ("L1", "L4"), ("L2", "L3"), ("L2", "L4"), ("L3", "L4")]]
    pairs.append({**base, "summa": 5000.0, "lasku_a": "L8", "lasku_b": "L9", "pvm_a": "2025-12-01", "pvm_b": "2025-12-02"})
    groups = group_duplicate_pairs(pairs)
    assert [(g["summa"], g["laskuja"]) for g in groups] == [(5000.0, 2), (100.0, 4)]
    assert groups[1]["laskut"] == "L1, L2, L3, L4" and groups[0]["paivat"] == "2025-12-01, 2025-12-02"
