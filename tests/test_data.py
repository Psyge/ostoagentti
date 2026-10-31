import duckdb
import pytest

from conftest import invoice_row as _row
from conftest import write_csv
from ostoagentti.data import COLUMNS, export_parquet, load_csv, load_parquet


def test_semicolon_decimal_comma_bom(tmp_path):
    csv = write_csv(tmp_path / "a.csv", COLUMNS, [
        _row("L1", "2025-09-01", "1 234,56"),
        _row("L1", "2025-09-01", "1.000,44"),
        _row("L2", "2025-09-15", "-50,00"),
    ], ";", bom=True)
    s = load_csv([csv], tmp_path / "a.duckdb")
    assert s["riveja"] == 3 and s["laskuja"] == 2
    assert s["hylatyt_paivamaarat"] == 0 and s["hylatyt_summat"] == 0
    assert s["yhteensa_eur"] == pytest.approx(2185.0)


def test_tab_dot_decimal_finnish_dates_and_multiple_files(tmp_path):
    a = write_csv(tmp_path / "a.tsv", COLUMNS, [_row("L1", "1.9.2025", "100.50")], "\t")
    b = write_csv(tmp_path / "b.csv", COLUMNS, [_row("L2", "2025-10-02T00:00:00", "200,25")], ";")
    db = tmp_path / "b.duckdb"
    s = load_csv([a, b], db)
    assert s["riveja"] == 2 and s["alku"] == "2025-09-01" and s["loppu"] == "2025-10-02"
    con = duckdb.connect(str(db), read_only=True)
    months = [str(r[0]) for r in con.execute("SELECT kuukausi FROM laskut ORDER BY 1").fetchall()]
    con.close()
    assert months == ["2025-09-01", "2025-10-01"]


def test_bad_values_are_counted_and_dropped(tmp_path):
    csv = write_csv(tmp_path / "c.csv", COLUMNS, [
        _row("L1", "2025-09-01", "10,00"),
        _row("L2", "ei pvm", "10,00"),
        _row("L3", "2025-09-02", "kymmenen"),
    ], ";")
    s = load_csv([csv], tmp_path / "c.duckdb")
    assert s["riveja"] == 1
    assert s["hylatyt_paivamaarat"] == 1 and s["hylatyt_summat"] == 1


def test_missing_column_gives_clear_error(tmp_path):
    header = [c for c in COLUMNS if c != "tili"]
    csv = write_csv(tmp_path / "d.csv", header, [], ";")
    with pytest.raises(ValueError, match="tili"):
        load_csv([csv], tmp_path / "d.duckdb")


def test_synth_loads_cleanly(synth):
    s = synth["summary"]
    assert s["riveja"] == synth["truth"]["riveja"]
    assert s["hylatyt_paivamaarat"] == 0 and s["hylatyt_summat"] == 0


def test_supplier_ids_are_normalized(tmp_path):
    # Oikeassa datassa sama toimittaja on sekä y-tunnuksena että ALV-muodossa, ja anonymisoiduilla on paikkamerkki.
    csv = write_csv(tmp_path / "e.csv", COLUMNS, [
        _row("L1", "2025-08-01", "10,00", ytunnus="0201256-6"),
        _row("L2", "2025-09-01", "10,00", ytunnus="FI02012566"),
        _row("L3", "2025-09-01", "10,00", toimittaja="Salassa pidettävä", ytunnus="NA"),
        _row("L4", "2025-09-01", "10,00", toimittaja="Revient-Bohemia", ytunnus="1234567-1"),
    ], ";")
    s = load_csv([csv], tmp_path / "e.duckdb")
    assert s["toimittajia"] == 1 and s["toimittajatunnus_puuttuu"] == 2


def test_parquet_roundtrip(synth, tmp_path):
    pq = tmp_path / "l.parquet"
    exported = export_parquet(synth["db"], pq)
    rebuilt = load_parquet(pq, tmp_path / "r.duckdb")
    assert rebuilt == exported and rebuilt["riveja"] == synth["truth"]["riveja"]
    assert not (tmp_path / "r.duckdb.building").exists()
