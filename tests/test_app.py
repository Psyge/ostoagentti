import json
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from ostoagentti import esimerkit
from ostoagentti.data import export_parquet
from test_agent import FakeClient, _call, _msg

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture()
def app_env(synth, tmp_path, monkeypatch):
    """Demo rakentaa kannan Parquetista kuten Streamlit Cloudissa. Oletuksena ei API-avainta."""
    parquet = tmp_path / "laskut.parquet"
    export_parquet(synth["db"], parquet)
    monkeypatch.setenv("OSTO_DB", str(tmp_path / "demo.duckdb"))
    monkeypatch.setenv("OSTO_PARQUET", str(parquet))
    monkeypatch.setenv("OSTO_ESIMERKIT", str(tmp_path / "esimerkit.json"))
    for k in ("LLM_PROVIDER", "LLM_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    st.cache_data.clear()
    st.cache_resource.clear()
    return tmp_path


def _run() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at


def test_builds_db_and_shows_anomalies_without_key(app_env, synth):
    at = _run()
    assert (app_env / "demo.duckdb").exists()
    assert [m.label for m in at.metric] == ["Ostot yhteensä", "Laskuja", "Toimittajia", "Hankintayksiköitä"]
    at.selectbox(key="kk_poik").set_value(synth["truth"]["kohdekuukausi"]).run()
    assert not at.exception
    spike_names = at.dataframe[0].value["Toimittaja"].tolist()
    assert spike_names[0] == synth["truth"]["piikki_toimittaja"]["nimi"]
    dup_counts = [df.value["Laskuja"].tolist() for df in at.dataframe if "Laskuja" in df.value and "Päivät" in df.value]
    assert dup_counts and all(n >= 2 for n in dup_counts[0])


def test_example_without_key_or_ready_answer_warns(app_env):
    at = _run()
    at.button(key="esim_0").click().run()
    assert any("ei ole käytössä" in w.value for w in at.warning)


def test_ready_answer_is_shown_without_llm(app_env):
    q = esimerkit.QUESTIONS[0]
    step = {"tool": "run_sql", "args": {"sql": "SELECT 1"}, "ok": True, "error": None, "rows": 1}
    (app_env / "esimerkit.json").write_text(json.dumps(
        {"kysymykset": {q: {"answer": "Valmis vastaus 42 €.", "steps": [step]}}, "raportit": {}}), encoding="utf-8")
    at = _run()
    at.button(key="esim_0").click().run()
    assert any("Valmis vastaus 42" in m.value for m in at.markdown)
    assert any(c.value == "SELECT 1" for c in at.code)


def test_live_answer_and_session_limit(app_env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("SESSION_LIMIT", "1")
    client = FakeClient([
        _msg(calls=[_call("c1", "run_sql", {"sql": "SELECT count(*) AS n FROM laskut"})]),
        _msg(content="Rivejä on paljon."),
    ])
    monkeypatch.setattr("ostoagentti.config.make_client", lambda: (client, "fake"))
    at = _run()
    at.button(key="esim_1").click().run()
    assert not at.exception
    assert any("Rivejä on paljon" in m.value for m in at.markdown)
    at.button(key="esim_2").click().run()
    assert any("uutta kysymystä" in w.value for w in at.warning)
