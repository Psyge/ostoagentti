# Ostodata-agentti

[![tests](https://github.com/KAYTTAJA/REPO/actions/workflows/tests.yml/badge.svg)](https://github.com/KAYTTAJA/REPO/actions/workflows/tests.yml)

**[Kokeile demoa →](https://SOVELLUS.streamlit.app)**

Agentti analysoi Suomen julkisen sektorin ostolaskuja ja etsii niistä poikkeamia.

**Periaate:** LLM suunnittelee ja tulkitsee, koodi laskee. Agentti ei näe raakadataa. Se kirjoittaa SQL-kyselyn, joka ajetaan DuckDB:ssä vain luku -tilassa, ja tulkitsee tuloksen. Poikkeamatunnistimet ovat deterministisiä, ja niiden osuvuus mitataan testidatalla.

## Demo
Streamlit-sovellus (`app.py`) toimii selaimessa ilman asennusta:
- **Poikkeamat:** kuukausipiikit, mahdolliset tuplalaskut ja uudet toimittajat valitulle kuukaudelle. Tämä osa ei käytä kielimallia.
- **Kysy agentilta:** esimerkkikysymyksiä ja oma kysymys. Vastauksen alla näkyvät agentin ajamat SQL-kyselyt.
- **Kuukausiraportti:** agentti kirjoittaa havaintoraportin tunnistimien tulosten pohjalta.

Kielimallin kulut on rajattu kahdella tavalla. Valmiiksi lasketut esimerkkivastaukset (`demo/esimerkit.json`) eivät kuluta kiintiötä, ja uusien kysymysten määrää on rajoitettu kävijää ja vuorokautta kohden (`SESSION_LIMIT`, `DAILY_LIMIT`). Ilman API-avainta poikkeamat ja valmiit vastaukset toimivat silti.

Paikallisesti:
```bash
streamlit run app.py
```

## Julkaisu: GitHub + Streamlit Community Cloud
1. Tarkista, että `demo/laskut_2025.parquet` on olemassa (noin 38 Mt). Jos ei ole, lataa CSV (ks. *Oikea data*) ja aja `python -m ostoagentti.cli export demo/laskut_2025.parquet`.
2. Laske valmiit esimerkkivastaukset. Komento käyttää omaa avaintasi kerran ja testaa samalla LLM-yhteyden:
   ```bash
   python -m ostoagentti.cli esimerkit --months 2025-09 2025-12
   ```
3. Luo GitHubiin uusi repo ja pushaa projekti. `.gitignore` jättää tietokannan, virtuaaliympäristön ja salaisuudet pois.
4. Avaa [share.streamlit.io](https://share.streamlit.io) → *Create app* → valitse repo, haara `main` ja tiedosto `app.py`.
5. Valitse *Advanced settings* -kohdasta Python 3.12. Liitä *Secrets*-kenttään `.streamlit/secrets.toml.example`-tiedoston rivit omalla avaimellasi.
6. Paina *Deploy*. Ensimmäinen käynnistys asentaa paketit ja rakentaa tietokannan Parquet-tiedostosta. Sovellus päivittyy jokaisesta pushista.
7. Vaihda tämän README:n alun linkit (demo ja testimerkki) omiksi, ja lisää `REPO_URL` Secretsiin.

Streamlit Cloud nukuttaa sovelluksen, jos sillä ei ole kävijöitä, joten ensimmäinen avaus voi kestää hetken.

## Rakenne
```
app.py                    Streamlit-demo
ostoagentti/data.py       CSV -> DuckDB (erotin, desimaalipilkku, päivämäärämuoto ja toimittajatunnukset); Parquet-vienti demolle
ostoagentti/tools.py      describe_schema, run_sql (vain SELECT, riviraja, aikakatkaisu), detect_anomalies
ostoagentti/agent.py      tool use -silmukka + raporttitila
ostoagentti/config.py     LLM-presetit: gemini | github | ollama | azure (kaikki OpenAI-yhteensopivia)
ostoagentti/esimerkit.py  demon esimerkkikysymykset ja valmiit vastaukset
ostoagentti/cli.py        komentorivi
synth/make_data.py        synteettinen data samalla skeemalla + istutetut poikkeamat (truth.json)
eval/eval_detectors.py    tunnistimien recall ja precision usealla siemenellä
demo/                     demon data (Parquet) ja valmiit vastaukset
tests/                    43 testiä, myös Streamlit-sovellukselle (AppTest)
.github/workflows/        testit ja tunnistimien mittaus jokaisesta pushista
```

## Asennus
```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

## Nopea kokeilu ilman latausta ja avaimia
```bash
python synth/make_data.py --out data/synth
python -m ostoagentti.cli --db data/synth.duckdb load data/synth/synth_ostolaskut.csv
python -m ostoagentti.cli --db data/synth.duckdb anomalies --month 2025-09
python eval/eval_detectors.py --seeds 10
```
Tulos 10 siemenellä: recall 100 % (50/50 istutettua), keskimäärin 2,6 ylimääräistä hälytystä kuukaudessa, precision 66 %. Istutettu piikki on aina listan ensimmäisenä.

## Oikea data
[Avoindata.fi: Tutkihankintoja.fi-palvelun data (valtion ja kuntien ostolaskut)](https://www.avoindata.fi/data/fi/dataset/tutkihankintoja-data). Julkaisija on Hansel ja lisenssi CC BY 4.0. Lataa yksi tai useampi vuosi (`th_data_<vuosi>.csv`, vuosi 2025 on noin 790 Mt). Tunnistimet tarvitsevat vähintään 12 kk historiaa, joten lataa vähintään kaksi peräkkäistä vuotta.
```bash
python -m ostoagentti.cli load data/raw/th_data_2024.csv data/raw/th_data_2025.csv
python -m ostoagentti.cli schema
```
Tarkista ensimmäisellä latauksella:
1. `hylatyt_paivamaarat` ja `hylatyt_summat` ovat 0 tai lähellä sitä. Jos eivät ole, katso muutama rivi ja lisää muoto `data.py`:n `_DATE_EXPR`- tai `_SUM_EXPR`-lausekkeeseen.
2. `toimittajatunnus_puuttuu` kertoo anonymisoidut ja tunnuksettomat rivit. Lataus muuttaa paikkamerkit (`NA`, `1234567-1`, `FIVEROTON`) NULLiksi ja ALV-muodon `FI02012566` y-tunnukseksi `0201256-6`. Jos dataan ilmestyy uusi paikkamerkki, lisää se `data.py`:n `PLACEHOLDER_SUPPLIER_IDS`-listaan. Lisää uusi anonymisoitu nimi `tools.py`:n `SUPPLIER_OK`-ehtoon.
3. Jos sarakkeita puuttuu, virheilmoitus kertoo löytyneet nimet. Päivitä silloin `COLUMNS`.

### Havainnot vuoden 2025 datasta
- 3,48 milj. tiliöintiriviä, 1,30 milj. laskua, 12,8 mrd €. Lataus kestää noin 12 s, ja hylättyjä rivejä on 0.
- Sama toimittaja esiintyy kahdessa tunnusmuodossa (8 600 tunnusta): valtio käyttää muotoa `0201256-6`, osa kunnista muotoa `FI02012566`. Helsinki vaihtoi muotoa syyskuussa 2025. Ilman yhtenäistämistä vaihdos näkyi 130 milj. €:n vääränä piikkinä, joka oli kuukauden suurin. Yhtenäistämisen jälkeen toimittajia on 26 122 (ennen 34 728).
- `NA`-tunnus kasasi 79 000 riviä (317 milj. €) yhdeksi "toimittajaksi". Mukana oli anonymisoituja ja ulkomaisia toimittajia.
- Helsingin `tositepvm` on aina kuun 1. päivä. Tuplatunnistimen 3 päivän ikkuna tarkoittaa sille siis "sama kuukausi".
- Pelkällä 2025-datalla piikit voidaan arvioida heinäkuusta alkaen, mutta uusia toimittajia ei voida arvioida lainkaan. Joulukuun piikit ovat osin kausivaihtelua.

## LLM (ilmaiset vaihtoehdot)
| Provider | Hinta | Avain | Huom |
|---|---|---|---|
| `gemini` (oletus) | ilmainen taso | Google AI Studio → `GEMINI_API_KEY` | Oletusmalli `gemini-3.5-flash-lite`, jolla on väljemmät ilmaisrajat. Tarkempi vaihtoehto: `LLM_MODEL=gemini-3.8-flash`. Ilmaistason kehotteita voidaan käyttää mallin parantamiseen, joten se sopii julkiselle datalle mutta ei asiakasdatalle. |
| `github` | ilmainen, tiukat rajat | GitHub PAT → `GITHUB_TOKEN` | GitHub Models pyörii Azuressa. |
| `ollama` | ilmainen, paikallinen | ei tarvita | Data ei lähde koneelta; vaatii tehoa (esim. `qwen2.5:7b`). |
| `azure` | maksullinen | `AZURE_OPENAI_API_KEY` + `LLM_BASE_URL` + `LLM_MODEL` (deployment) | Sama koodi, vain muuttujat vaihtuvat. |

```bash
export GEMINI_API_KEY=...           # PowerShell: $env:GEMINI_API_KEY="..."
python -m ostoagentti.cli ask "Mitkä viisi hankintakategoriaa kasvoivat eniten 2025 verrattuna 2024?"
python -m ostoagentti.cli report --month 2025-09 --out raportti.md
```
Mallinimet vaihtuvat. Tarvittaessa aseta `LLM_MODEL`. Agentti listaa lopuksi käyttämänsä työkalukutsut ja SQL:t, jotta jokaisen luvun voi tarkistaa.

## Turvallisuus
- Agentin yhteys on `read_only` ja `enable_external_access=false`: ei kirjoituksia, tiedostoja, verkkoa tai `getenv`-kutsua.
- `run_sql` hyväksyy vain yhden SELECT/WITH-lauseen. Rivejä palautetaan enintään 50 ja aikakatkaisu on 20 s.
- Virheet palautetaan agentille, joka korjaa kyselynsä itse.
- Demossa jokaisella kävijällä on oma tietokantayhteys ja DuckDB:n muistiraja (`DUCKDB_MEMORY_LIMIT`, oletus 1 Gt).

## Data ja lisenssi
Data: Tutkihankintoja.fi-palvelun data, Hansel, [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). `demo/laskut_2025.parquet` on siitä siivottu ja tiivistetty versio.

## Seuraavaksi (datav2)
- Agentin evaluointi: kysymyssarja, jonka oikeat vastaukset lasketaan SQL:llä, ja osumaprosentti mallikohtaisesti.
- Kausivaihtelu: vertailu samaan kuukauteen edellisenä vuonna.
- Ajastettu raportti Telegramiin tai sähköpostiin.
