"""Streamlit-demo. Paikallisesti: streamlit run app.py

Asetukset ympäristömuuttujina tai Streamlitin Secrets-kohdassa (juuritasolla):
  GEMINI_API_KEY        agentti ja raportit (tai muu config.py:n provider). Ilman avainta poikkeamat
                        ja valmiit esimerkit toimivat silti.
  DAILY_LIMIT           uusia LLM-kysymyksiä vuorokaudessa koko demolle (oletus 50)
  SESSION_LIMIT         uusia LLM-kysymyksiä kävijää kohden (oletus 5)
  OSTO_DB               tietokanta (oletus data/ostot.duckdb). Jos puuttuu, rakennetaan OSTO_PARQUET-tiedostosta.
  OSTO_PARQUET          oletus demo/laskut_2025.parquet
  OSTO_ESIMERKIT        valmiit vastaukset (oletus demo/esimerkit.json)
  DUCKDB_MEMORY_LIMIT   oletus 1GB
  REPO_URL              GitHub-repon osoite Miten tämä toimii -välilehdelle
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from ostoagentti import esimerkit
from ostoagentti.config import llm_settings
from ostoagentti.data import load_parquet
from ostoagentti.tools import Tools, group_duplicate_pairs

st.set_page_config(page_title="Ostodata-agentti", page_icon="🔎", layout="wide")

# config.py lukee avaimen ympäristömuuttujista, joten Streamlitin Secrets kopioidaan sinne.
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, (str, int, float)):
            os.environ.setdefault(_k, str(_v))
except Exception:  # paikallisesti secrets.toml-tiedostoa ei yleensä ole
    pass

ROOT = Path(__file__).resolve().parent  # polut eivät riipu siitä, mistä hakemistosta sovellus käynnistetään
DB_PATH = os.environ.get("OSTO_DB", str(ROOT / "data" / "ostot.duckdb"))
PARQUET_PATH = os.environ.get("OSTO_PARQUET", str(ROOT / "demo" / "laskut_2025.parquet"))
EXAMPLES_PATH = os.environ.get("OSTO_ESIMERKIT", str(ROOT / esimerkit.DEFAULT_PATH))
MEMORY_LIMIT = os.environ.get("DUCKDB_MEMORY_LIMIT", "1GB") or None
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "50"))
SESSION_LIMIT = int(os.environ.get("SESSION_LIMIT", "5"))
REPO_URL = os.environ.get("REPO_URL", "")
MAX_QUESTION_CHARS = 300
MIN_HISTORY_MONTHS = 6  # sama kuin tunnistimen spike_min_active_months
DATA_URL = "https://www.avoindata.fi/data/fi/dataset/tutkihankintoja-data"

KUUKAUDET = ["tammikuu", "helmikuu", "maaliskuu", "huhtikuu", "toukokuu", "kesäkuu", "heinäkuu",
             "elokuu", "syyskuu", "lokakuu", "marraskuu", "joulukuu"]

SPIKE_COLS = {
    "nimi": "Toimittaja", "avain": "Y-tunnus", "kuukauden_eur": "Kuukausi (€)",
    "mediaani_12kk_eur": "Mediaani 12 kk (€)", "maksimi_12kk_eur": "Korkein 12 kk (€)",
    "ylitys_eur": "Ylitys (€)", "robust_z": "Robust z", "aktiivisia_kk": "Aktiivisia kk",
}
CATEGORY_COLS = {"nimi": "Hankintakategoria", **{k: v for k, v in SPIKE_COLS.items() if k not in ("nimi", "avain")}}
DUP_COLS = {
    "toimittaja": "Toimittaja", "toimittaja_y_tunnus": "Y-tunnus", "hankintayksikko": "Hankintayksikkö",
    "summa": "Summa (€)", "laskuja": "Laskuja", "paivat": "Päivät", "laskut": "Laskujen tunnukset",
}
NEW_COLS = {
    "nimi": "Toimittaja", "avain": "Y-tunnus", "kuukauden_eur": "Kuukausi (€)", "laskuja": "Laskuja",
    "hankintakategoria": "Hankintakategoria", "hankintayksikot": "Hankintayksiköt",
}


# --- muotoilu ---------------------------------------------------------------
def fmt_num(x: float, decimals: int = 0) -> str:
    return f"{x:,.{decimals}f}".replace(",", " ").replace(".", ",")


def fmt_eur(x: float) -> str:
    for div, unit in ((1e9, "mrd. €"), (1e6, "milj. €"), (1e3, "t€")):
        if abs(x) >= div:
            return f"{fmt_num(x / div, 1 if div > 1e3 else 0)} {unit}"
    return f"{fmt_num(x)} €"


def fmt_month(m: str) -> str:
    y, mm = m.split("-")
    return f"{KUUKAUDET[int(mm) - 1].capitalize()} {y}"


def table(rows: list[dict], cols: dict[str, str], empty: str = "Ei havaintoja tältä kuukaudelta.") -> None:
    if not rows:
        st.caption(empty)
        return
    df = pd.DataFrame(rows)[list(cols)].rename(columns=cols)
    numbers = {label: st.column_config.NumberColumn(format="localized")
               for label in df.columns if df[label].dtype.kind in "if"}
    st.dataframe(df, hide_index=True, column_config=numbers)


# --- data -------------------------------------------------------------------
@st.cache_resource(show_spinner="Rakennetaan tietokantaa demon datasta…")
def ensure_db(db_path: str, parquet_path: str) -> str:
    if not Path(db_path).exists():
        if not Path(parquet_path).exists():
            raise FileNotFoundError(
                f"Tietokantaa {db_path} tai dataa {parquet_path} ei löydy. "
                "Aja ensin: python -m ostoagentti.cli export demo/laskut_2025.parquet"
            )
        load_parquet(parquet_path, db_path)
    return db_path


def new_tools(db: str) -> Tools:
    return Tools(db, memory_limit=MEMORY_LIMIT)


def session_tools(db: str) -> Tools:
    """Oma yhteys jokaiselle kävijälle: aikakatkaisu keskeyttää koko yhteyden, eikä yhteys ole säieturvallinen."""
    if st.session_state.get("tools_db") != db:
        st.session_state.tools = new_tools(db)
        st.session_state.tools_db = db
    return st.session_state.tools


@st.cache_data(show_spinner=False)
def overview(db: str) -> dict:
    t = new_tools(db)
    try:
        stats = t.describe_schema()["tilastot"]
        eur = t.run_sql("SELECT sum(tiliointisumma) FROM laskut")["rivit"][0][0]
        months = [r[0] for r in t.run_sql(
            "SELECT DISTINCT strftime(kuukausi, '%Y-%m') FROM laskut ORDER BY 1")["rivit"]]
    finally:
        t.close()
    return {**stats, "eur": eur, "kuukaudet": months}


@st.cache_data(show_spinner="Ajetaan tunnistimia…")
def anomalies(db: str, month: str) -> dict:
    t = new_tools(db)
    try:
        res = t.detect_anomalies(month)
        # Ryhmittelyä varten pareja haetaan enemmän kuin agentin oletusraja (10).
        pairs = t.detect_anomalies(month, kind="tuplat", limit=200)["mahdolliset_tuplat"]
    finally:
        t.close()
    res["tuplaryhmat"] = group_duplicate_pairs(pairs)[:10]
    return res


@st.cache_data(show_spinner=False)
def examples(path: str) -> dict:
    return esimerkit.load(path)


# --- agentti ----------------------------------------------------------------
@st.cache_resource
def shared_state() -> dict:
    """Kaikille kävijöille yhteinen: päivän käyttölaskuri ja kerran lasketut vastaukset."""
    return {"lock": threading.Lock(), "day": "", "count": 0, "answers": {}}


def llm_problem() -> str | None:
    try:
        llm_settings()
        return None
    except ValueError as e:
        return str(e)


def take_quota() -> str | None:
    """Kirjaa yhden LLM-kysymyksen. Palauttaa syyn, jos raja on täynnä."""
    used = st.session_state.get("used", 0)
    if used >= SESSION_LIMIT:
        return f"Olet esittänyt jo {SESSION_LIMIT} uutta kysymystä tässä istunnossa. Valmiit esimerkit toimivat edelleen."
    s = shared_state()
    with s["lock"]:
        today = date.today().isoformat()
        if s["day"] != today:
            s["day"], s["count"] = today, 0
        if s["count"] >= DAILY_LIMIT:
            return "Demon päiväraja on täynnä. Valmiit esimerkit toimivat edelleen, ja uusia kysymyksiä voi esittää huomenna."
        s["count"] += 1
    st.session_state.used = used + 1
    return None


def _key(text: str) -> str:
    return " ".join(text.lower().split())


def known_answer(kind: str, arg: str) -> dict | None:
    ready = examples(EXAMPLES_PATH)["kysymykset" if kind == "kysymys" else "raportit"]
    if arg in ready:
        return {**ready[arg], "lahde": "valmis"}
    hit = shared_state()["answers"].get((kind, _key(arg)))
    return {**hit, "lahde": "muisti"} if hit else None


def llm_error_text(e: Exception) -> str:
    import openai

    if isinstance(e, openai.RateLimitError):
        return "Kielimallin ilmaiskiintiö on hetkellisesti täynnä. Yritä hetken kuluttua uudelleen."
    if isinstance(e, openai.AuthenticationError):
        return "API-avain ei kelpaa. Tarkista avain Streamlitin Secrets-asetuksista."
    if isinstance(e, openai.NotFoundError):
        return "Mallia ei löydy. Aseta LLM_MODEL ajantasaiseen mallinimeen."
    return f"Kielimallin kutsu epäonnistui ({type(e).__name__})."


def step_label(s: dict) -> str:
    mark = "✓" if s["ok"] else "✗"
    detail = " ".join(str(s["args"].get("sql") or s["args"].get("month") or "").split())
    if len(detail) > 100:
        detail = detail[:100] + "…"
    rows = f" · {s['rows']} riviä" if s.get("rows") is not None else ""
    return f"{mark} **{s['tool']}**{rows}" + (f" · `{detail}`" if detail else "")


def run_agent(db: str, kind: str, arg: str) -> dict | None:
    hit = known_answer(kind, arg)
    if hit:
        return hit
    problem = llm_problem()
    if problem:
        st.warning(f"Agentti ei ole käytössä tässä asennuksessa ({problem}). Poikkeamat ja valmiit esimerkit toimivat.")
        return None
    limit = take_quota()
    if limit:
        st.warning(limit)
        return None

    import openai

    from ostoagentti.agent import Agent
    from ostoagentti.config import make_client

    client, model = make_client()
    agent = Agent(session_tools(db), client, model)
    with st.status("Agentti työskentelee…", expanded=True) as status:
        def on_step(step: dict) -> None:
            status.write(step_label(step))

        try:
            if kind == "kysymys":
                res = agent.run(arg, on_step=on_step)
            else:
                res, _ = agent.report(arg, on_step=on_step)
        except openai.APIError as e:
            status.update(label="Kielimallin kutsu epäonnistui", state="error")
            st.error(llm_error_text(e))
            return None
        status.update(label=f"Valmis · {len(res.steps)} työkalukutsua", state="complete", expanded=False)
    answer = {"answer": res.answer, "steps": res.steps}
    shared_state()["answers"][(kind, _key(arg))] = answer
    return {**answer, "lahde": "uusi"}


def show_answer(res: dict) -> None:
    st.markdown(res["answer"].replace("$", "\\$"))  # $ aloittaisi muuten LaTeX-kaavan
    if res.get("lahde") == "valmis":
        st.caption("Valmiiksi laskettu vastaus: ei kuluttanut kielimallin kiintiötä.")
    elif res.get("lahde") == "muisti":
        st.caption("Sama kysymys esitettiin aiemmin, joten vastaus näytetään muistista.")
    steps = res.get("steps") or []
    with st.expander(f"Näytä työkalukutsut ja SQL ({len(steps)})"):
        if not steps:
            st.caption("Agentti vastasi ilman työkalukutsuja.")
        for i, s in enumerate(steps, 1):
            status = "ok" if s["ok"] else f"virhe: {s['error']}"
            rows = f", {s['rows']} riviä" if s.get("rows") is not None else ""
            st.markdown(f"**{i}. {s['tool']}** ({status}{rows})")
            if s["args"].get("sql"):
                st.code(s["args"]["sql"], language="sql")
            elif s["args"]:
                st.code(json.dumps(s["args"], ensure_ascii=False), language="json")


def answer_area(db: str, kind: str, arg: str | None) -> None:
    """Ajaa uuden pyynnön ja näyttää viimeisimmän vastauksen myös seuraavilla uudelleenajoilla."""
    if arg:
        res = run_agent(db, kind, arg)
        if res:
            st.session_state[f"viimeisin_{kind}"] = {"arg": arg, **res}
    last = st.session_state.get(f"viimeisin_{kind}")
    if last:
        st.divider()
        st.markdown(f"**Kysymys:** {last['arg']}" if kind == "kysymys" else f"**Raportti:** {fmt_month(last['arg'])}")
        show_answer(last)


# --- sivu -------------------------------------------------------------------
try:
    db = ensure_db(DB_PATH, PARQUET_PATH)
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()

ov = overview(db)
years = sorted({ov["alku"][:4], ov["loppu"][:4]})
period = f"vuodelta {years[0]}" if len(years) == 1 else f"vuosilta {years[0]}–{years[-1]}"
months = ov["kuukaudet"]
selectable = months[MIN_HISTORY_MONTHS:] or months
ready = examples(EXAMPLES_PATH)

st.title("Ostodata-agentti")
st.markdown(
    f"Suomen julkisen sektorin ostolaskut {period} ([Tutkihankintoja.fi]({DATA_URL})). "
    "Kielimalli suunnittelee ja tulkitsee, koodi laskee: jokainen luku tulee SQL-kyselystä, jonka voit tarkistaa."
)
c = st.columns(4)
c[0].metric("Ostot yhteensä", fmt_eur(ov["eur"]))
c[1].metric("Laskuja", fmt_num(ov["laskuja"]))
c[2].metric("Toimittajia", fmt_num(ov["toimittajia"]))
c[3].metric("Hankintayksiköitä", fmt_num(ov["hankintayksikoita"]))

tab_poik, tab_kysy, tab_rap, tab_info = st.tabs(["Poikkeamat", "Kysy agentilta", "Kuukausiraportti", "Miten tämä toimii"])

with tab_poik:
    st.caption(
        "Deterministiset tunnistimet ilman kielimallia. Poikkeama ei ole väärinkäytös, "
        "vaan kohta, joka kannattaa tarkistaa."
    )
    month = st.selectbox("Kuukausi", selectable, index=len(selectable) - 1, format_func=fmt_month, key="kk_poik")
    res = anomalies(db, month)
    for note in res["huomiot"]:
        st.info(note)
    spikes = res.get("piikit", {})
    st.subheader("Toimittajat, joiden laskutus hyppäsi")
    st.caption(
        "Kuukauden summa verrattuna edeltävien 12 kuukauden mediaaniin (robust z ≥ 4). "
        "Lisäksi summan pitää olla vähintään 1,5 × korkein kuukausi 12 kuukauden ajalta."
    )
    table(spikes.get("toimittaja", []), SPIKE_COLS)
    st.subheader("Hankintakategoriat, joissa ostot hyppäsivät")
    table(spikes.get("hankintakategoria", []), CATEGORY_COLS)
    st.subheader("Mahdolliset tuplalaskut")
    st.caption(
        "Sama toimittaja, hankintayksikkö ja summa enintään 3 päivän sisällä. Osa yksiköistä kirjaa "
        "päivämääräksi aina kuun 1. päivän, jolloin ehto tarkoittaa käytännössä samaa kuukautta."
    )
    table(res["tuplaryhmat"], DUP_COLS)
    if "uudet_toimittajat" in res:
        st.subheader("Uudet isot toimittajat")
        table([{**r, "hankintayksikot": ", ".join(r["hankintayksikot"] or [])} for r in res["uudet_toimittajat"]],
              NEW_COLS)

with tab_kysy:
    problem = llm_problem()
    if problem and not ready["kysymykset"]:
        st.info("Kielimalli ei ole käytössä tässä asennuksessa. Poikkeamat-välilehti toimii ilman sitä.")
    st.write("Valitse esimerkki tai kirjoita oma kysymys. Agentti kirjoittaa SQL-kyselyt itse ja näyttää ne vastauksen alla.")
    question = None
    cols = st.columns(2)
    for i, q in enumerate(esimerkit.QUESTIONS):
        if cols[i % 2].button(q, key=f"esim_{i}", width="stretch"):
            question = q
    with st.form("oma_kysymys", border=False):
        text = st.text_input("Oma kysymys", max_chars=MAX_QUESTION_CHARS,
                             placeholder="Esim. Paljonko Helsinki osti siivouspalveluita?")
        if st.form_submit_button("Kysy", type="primary") and text.strip():
            question = text.strip()
    answer_area(db, "kysymys", question)
    if problem is None:
        left = max(0, SESSION_LIMIT - st.session_state.get("used", 0))
        st.caption(f"Uusia kysymyksiä jäljellä tässä istunnossa: {left}. Valmiit esimerkit eivät kuluta kiintiötä.")

with tab_rap:
    st.write("Agentti saa tunnistimien tulokset, tarkentaa niitä SQL-kyselyillä ja kirjoittaa havaintoraportin.")
    rmonth = st.selectbox(
        "Kuukausi", selectable, index=len(selectable) - 1, key="kk_rap",
        format_func=lambda m: fmt_month(m) + (" · valmis raportti" if m in ready["raportit"] else ""),
    )
    label = "Näytä raportti" if rmonth in ready["raportit"] else "Kirjoita raportti"
    answer_area(db, "raportti", rmonth if st.button(label, type="primary", key="rap_btn") else None)

with tab_info:
    st.markdown(f"""
### Periaate
Kielimalli ei näe raakadataa eikä laske lukuja. Se valitsee työkalun, kirjoittaa SQL-kyselyn ja tulkitsee
tuloksen. Jokaisen vastauksen alta näkee, mitä kyselyitä ajettiin.

1. **describe_schema** kertoo taulun rakenteen ja datan erityispiirteet.
2. **run_sql** ajaa yhden SELECT-kyselyn DuckDB:ssä vain luku -tilassa. Rivejä palautetaan enintään 50, ja aikaraja on 20 s.
3. **detect_anomalies** ajaa deterministiset tunnistimet: kuukausipiikit, uudet isot toimittajat ja mahdolliset tuplalaskut.

### Turvallisuus
Yhteys on vain luku -tilassa, eikä siitä pääse käsiksi tiedostoihin, verkkoon tai ympäristömuuttujiin.
Kirjoittavat lauseet ja tiedostoja lukevat funktiot hylätään jo ennen tietokantaa. Virheet palautetaan agentille, joka korjaa kyselynsä itse.

### Tunnistimien mittaus
Synteettiseen dataan istutetaan tunnettuja poikkeamia. Tulos 10 siemenellä: recall 100 % (50/50),
keskimäärin 2,6 ylimääräistä hälytystä kuukaudessa ja precision 66 %.

### Data
[Tutkihankintoja.fi-palvelun data]({DATA_URL}): valtion ja kuntien ostolaskut, julkaisija Hansel, lisenssi CC BY 4.0.
Latauksessa toimittajatunnukset yhtenäistetään (`FI02012566` → `0201256-6`), ja paikkamerkit (`NA`, `1234567-1`)
muutetaan tyhjiksi. Ilman yhtenäistämistä Helsingin syyskuussa 2025 tekemä tunnusmuodon vaihto näkyisi 130 milj. €:n
vääränä piikkinä.

### Rajoitukset
- Datassa on vain yksi vuosi, joten uusia toimittajia ei voida arvioida, ja joulukuun piikit ovat osin kausivaihtelua.
- Anonymisoidut toimittajat (esim. "Salassa pidettävä") jätetään toimittajavertailujen ulkopuolelle.
- Kielimalli voi tulkita väärin. Tarkista luvut SQL-kyselyistä.
""" + (f"\n[Lähdekoodi GitHubissa]({REPO_URL})" if REPO_URL else ""))

st.caption(f"Data: Tutkihankintoja.fi / Hansel, CC BY 4.0 · tiedot {fmt_month(months[0]).lower()}–{fmt_month(months[-1]).lower()}")
