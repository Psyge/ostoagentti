"""Synteettinen ostolaskudata samalla skeemalla kuin Tutkihankintoja.fi-data.

Kaikki nimet ja tunnukset ovat keksittyjä (Demo...). Dataan istutetaan tunnetut
poikkeamat, joiden avulla tunnistimia ja agenttia voidaan mitata. Totuus tallentuu
truth.json-tiedostoon.

Käyttö: python synth/make_data.py --out data/synth --scale 1.0
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

COLUMNS = [
    "lasku_id", "hankintayksikko", "hankintayksikko_tunnus", "ylaorganisaatio",
    "ylaorganisaatio_tunnus", "toimittaja_y_tunnus", "toimittaja_nimi", "tili",
    "hankintakategoria", "tuote_palveluryhma", "tositepvm", "tiliointisumma", "sektori",
]

CATEGORIES = {
    "Rakentaminen": (["Talonrakennus", "Korjausrakentaminen"], "4350", 9000),
    "Kiinteistöpalvelut": (["Siivous", "Kiinteistöhuolto"], "4360", 2500),
    "ICT-palvelut": (["Ohjelmistolisenssit", "ICT-asiantuntijat"], "4370", 6000),
    "Asiantuntijapalvelut": (["Konsultointi", "Lakipalvelut"], "4310", 5000),
    "Kuljetuspalvelut": (["Henkilökuljetus", "Tavarakuljetus"], "4380", 1800),
    "Koulutuspalvelut": (["Henkilöstökoulutus", "Kurssit"], "4320", 1500),
    "Ruokapalvelut": (["Ateriapalvelut", "Elintarvikkeet"], "4390", 2200),
    "Terveyspalvelut": (["Työterveyshuolto", "Ostopalvelut"], "4340", 7000),
    "Toimistotarvikkeet": (["Paperi", "Toimistokalusteet"], "4510", 600),
    "Energia": (["Sähkö", "Kaukolämpö"], "4610", 12000),
}

UNITS = [
    ("Demovirasto Pohjoinen", "Demoministeriön hallinnonala", "Valtio"),
    ("Demovirasto Etelä", "Demoministeriön hallinnonala", "Valtio"),
    ("Demolaitos", "Demoministeriön hallinnonala", "Valtio"),
    ("Esimerkkikaupunki / Sivistystoimiala", "Esimerkkikaupunki", "Muut julkiset organisaatiot"),
    ("Esimerkkikaupunki / Kaupunkiympäristö", "Esimerkkikaupunki", "Muut julkiset organisaatiot"),
    ("Esimerkkikaupunki / Kulttuuri ja vapaa-aika", "Esimerkkikaupunki", "Muut julkiset organisaatiot"),
    ("Demo-hyvinvointialue / Sosiaalipalvelut", "Demo-hyvinvointialue", "Muut julkiset organisaatiot"),
    ("Demo-hyvinvointialue / Terveyspalvelut", "Demo-hyvinvointialue", "Muut julkiset organisaatiot"),
]

TARGET_MONTH = "2025-09"


def ytunnus(n: int) -> str:
    return f"{n:07d}-{n % 10}"


def month_iter(start: date, months: int):
    y, m = start.year, start.month
    for _ in range(months):
        yield date(y, m, 1)
        m += 1
        if m == 13:
            y, m = y + 1, 1


def days_in_month(d: date) -> int:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return (nxt - d).days


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def fmt_eur(x: float, decimal_comma: bool) -> str:
    s = f"{x:.2f}"
    return s.replace(".", ",") if decimal_comma else s


def generate(out_dir: Path, scale: float = 1.0, seed: int = 42, decimal_comma: bool = True,
             delimiter: str = ";") -> dict:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    start, n_months = date(2023, 1, 1), 36

    units = [
        {"nimi": n, "tunnus": ytunnus(1_000_000 + i), "yla": y, "yla_tunnus": ytunnus(2_000_000 + i // 3), "sektori": s}
        for i, (n, y, s) in enumerate(UNITS)
    ]
    cat_names = list(CATEGORIES)
    n_suppliers = max(40, int(600 * scale))
    suppliers = []
    for i in range(n_suppliers):
        cat = rng.choice(cat_names)
        groups, tili, base = CATEGORIES[cat]
        suppliers.append({
            "nimi": f"Demo{cat.split('-')[0].split()[0]} {i:03d} Oy",
            "tunnus": ytunnus(3_000_000 + i),
            "kategoria": cat,
            "ryhma": rng.choice(groups),
            "tili": tili,
            "typical": base * math.exp(rng.gauss(0, 0.6)),
            "rate": max(0.3, rng.lognormvariate(0.6, 0.7)),
            "units": rng.sample(units, k=rng.choice([1, 1, 2, 3])),
            "start_month": 0 if rng.random() < 0.85 else rng.randint(1, 20),
        })

    # --- istutetut poikkeamat (kohdekuukausi 2025-09) -----------------------
    steady = [s for s in suppliers if s["start_month"] == 0 and s["rate"] >= 1.5]
    spike_supplier = max(steady, key=lambda s: s["typical"] * s["rate"])
    spike_factor = 8.0
    new_supplier = {
        "nimi": "DemoUusi Konsultointi 999 Oy", "tunnus": ytunnus(3_999_999), "kategoria": "Asiantuntijapalvelut",
        "ryhma": "Konsultointi", "tili": "4310", "typical": 45_000.0, "rate": 4.0,
        "units": [units[0]], "start_month": 32,
    }
    suppliers.append(new_supplier)
    dup_candidates = [s for s in steady if s is not spike_supplier]
    dup_suppliers = rng.sample(dup_candidates, k=3)

    rows: list[list[str]] = []
    inv_no = 0
    target_idx = 32  # 2023-01 + 32 kk = 2025-09
    duplicates_truth = []

    def add_invoice(sup, unit, day: date, total: float) -> str:
        nonlocal inv_no
        inv_no += 1
        lasku_id = f"SYN-{inv_no:07d}"
        parts = 1 if total < 3000 or rng.random() < 0.6 else rng.choice([2, 3])
        weights = [rng.random() + 0.2 for _ in range(parts)]
        wsum = sum(weights)
        amounts = [round(total * w / wsum, 2) for w in weights]
        amounts[-1] = round(total - sum(amounts[:-1]), 2)
        for a in amounts:
            rows.append([
                lasku_id, unit["nimi"], unit["tunnus"], unit["yla"], unit["yla_tunnus"],
                sup["tunnus"], sup["nimi"], sup["tili"], sup["kategoria"], sup["ryhma"],
                day.isoformat(), fmt_eur(a, decimal_comma), unit["sektori"],
            ])
        return lasku_id

    for mi, month in enumerate(month_iter(start, n_months)):
        season = 1.6 if month.month == 12 else (0.6 if month.month == 7 else 1.0)
        for sup in suppliers:
            if mi < sup["start_month"]:
                continue
            factor = season * (spike_factor if (sup is spike_supplier and mi == target_idx) else 1.0)
            n = poisson(rng, sup["rate"] * factor)
            for _ in range(n):
                unit = rng.choice(sup["units"])
                day = month + timedelta(days=rng.randrange(days_in_month(month)))
                total = round(sup["typical"] * math.exp(rng.gauss(0, 0.45)), 2)
                add_invoice(sup, unit, day, total)
        if mi == target_idx:
            for sup in dup_suppliers:
                unit = sup["units"][0]
                day = month + timedelta(days=rng.randint(3, 20))
                total = round(max(sup["typical"] * 1.3, 1500.0) + 123.45, 2)  # yli tunnistimen 1 000 € rajan
                a = add_invoice(sup, unit, day, total)
                b = add_invoice(sup, unit, day + timedelta(days=rng.choice([0, 1, 2])), total)
                duplicates_truth.append({"toimittaja_y_tunnus": sup["tunnus"], "laskut": [a, b], "summa": total})

    rng.shuffle(rows)
    csv_path = out_dir / "synth_ostolaskut.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=delimiter)
        w.writerow(COLUMNS)
        w.writerows(rows)

    truth = {
        "kohdekuukausi": TARGET_MONTH,
        "piikki_toimittaja": {"toimittaja_y_tunnus": spike_supplier["tunnus"], "nimi": spike_supplier["nimi"], "kerroin": spike_factor},
        "uusi_toimittaja": {"toimittaja_y_tunnus": new_supplier["tunnus"], "nimi": new_supplier["nimi"]},
        "tuplat": duplicates_truth,
        "riveja": len(rows),
    }
    (out_dir / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"csv": str(csv_path), "truth": truth}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="data/synth")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    res = generate(Path(args.out), scale=args.scale, seed=args.seed)
    print(f"{res['truth']['riveja']} riviä -> {res['csv']}")


if __name__ == "__main__":
    main()
