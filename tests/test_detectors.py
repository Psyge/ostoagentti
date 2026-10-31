from conftest import invoice_row, write_csv
from ostoagentti.data import COLUMNS, load_csv
from ostoagentti.tools import Tools


def test_planted_spike_found_first(tools, synth):
    truth = synth["truth"]
    res = tools.detect_anomalies(truth["kohdekuukausi"])
    spikes = res["piikit"]["toimittaja"]
    assert spikes, "piikkejä ei löytynyt"
    assert spikes[0]["avain"] == truth["piikki_toimittaja"]["toimittaja_y_tunnus"]
    assert len(spikes) <= 5, f"liikaa vääriä hälytyksiä: {len(spikes)}"


def test_planted_new_supplier_found(tools, synth):
    truth = synth["truth"]
    res = tools.detect_anomalies(truth["kohdekuukausi"], kind="uudet_toimittajat")
    keys = [r["avain"] for r in res["uudet_toimittajat"]]
    assert truth["uusi_toimittaja"]["toimittaja_y_tunnus"] in keys


def test_all_planted_duplicates_found(tools, synth):
    truth = synth["truth"]
    res = tools.detect_anomalies(truth["kohdekuukausi"], kind="tuplat")
    found = {frozenset((r["lasku_a"], r["lasku_b"])) for r in res["mahdolliset_tuplat"]}
    for d in truth["tuplat"]:
        assert frozenset(d["laskut"]) in found


def test_normal_month_is_quiet(tools):
    res = tools.detect_anomalies("2025-05")
    assert len(res["piikit"]["toimittaja"]) <= 5
    assert res["mahdolliset_tuplat"] == []


def test_short_history_is_flagged(tools):
    res = tools.detect_anomalies("2023-03")
    assert "piikit" not in res and "uudet_toimittajat" not in res
    assert len(res["huomiot"]) == 2


def test_months_before_data_start_are_not_zeros(tmp_path):
    # 15 t€/kk maalis-elokuussa, syyskuussa 25 t€. Jos datan alkua edeltävät kuukaudet luettaisiin
    # nolliksi, mediaani putoaisi 7,5 t€:oon, hajonta kasvaisi eikä piikkiä löytyisi.
    rows = [invoice_row(f"L{i}", f"2025-{i:02d}-10", "15000.00") for i in range(3, 9)]
    rows.append(invoice_row("L9", "2025-09-10", "25000.00"))
    db = tmp_path / "w.duckdb"
    load_csv([write_csv(tmp_path / "w.csv", COLUMNS, rows, ";")], db)
    t = Tools(db)
    try:
        spikes = t.detect_anomalies("2025-09", kind="piikit")["piikit"]["toimittaja"]
    finally:
        t.close()
    assert [r["avain"] for r in spikes] == ["3000001-1"]
