"""Mittaa tunnistimet istutettuja poikkeamia vastaan usealla satunnaissiemenellä (ei LLM:ää, ilmainen).

Käyttö: python eval/eval_detectors.py --seeds 10 --scale 1.0
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "synth"))

from make_data import generate  # noqa: E402
from ostoagentti.data import load_csv  # noqa: E402
from ostoagentti.tools import Tools  # noqa: E402


def evaluate(seed: int, scale: float, workdir: Path) -> dict:
    d = workdir / f"seed{seed}"
    res = generate(d, scale=scale, seed=seed)
    db = d / "ostot.duckdb"
    load_csv([res["csv"]], db)
    truth = res["truth"]
    tools = Tools(db)
    try:
        out = tools.detect_anomalies(truth["kohdekuukausi"])
    finally:
        tools.close()

    spikes = out["piikit"]["toimittaja"]
    spike_keys = [r["avain"] for r in spikes]
    new_keys = [r["avain"] for r in out["uudet_toimittajat"]]
    dup_pairs = {frozenset((r["lasku_a"], r["lasku_b"])) for r in out["mahdolliset_tuplat"]}

    planted_spike = truth["piikki_toimittaja"]["toimittaja_y_tunnus"]
    planted_new = truth["uusi_toimittaja"]["toimittaja_y_tunnus"]
    dups_found = sum(frozenset(t["laskut"]) in dup_pairs for t in truth["tuplat"])
    planted_total = 2 + len(truth["tuplat"])
    found_total = (planted_spike in spike_keys) + (planted_new in new_keys) + dups_found
    extra = (len(spike_keys) - (planted_spike in spike_keys)) + (len(new_keys) - (planted_new in new_keys)) \
        + (len(dup_pairs) - dups_found)
    return {
        "seed": seed,
        "riveja": truth["riveja"],
        "loydetty": found_total,
        "istutettu": planted_total,
        "piikki_sija": spike_keys.index(planted_spike) + 1 if planted_spike in spike_keys else None,
        "ylimaaraisia": extra,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--scale", type=float, default=1.0)
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        results = [evaluate(s, args.scale, Path(tmp)) for s in range(1, args.seeds + 1)]

    print(f"{'siemen':>6} {'rivejä':>8} {'löydetty':>9} {'piikin sija':>11} {'ylimääräisiä':>12}")
    for r in results:
        sija = r["piikki_sija"] if r["piikki_sija"] else "-"
        print(f"{r['seed']:>6} {r['riveja']:>8} {r['loydetty']:>4}/{r['istutettu']:<4} {sija:>11} {r['ylimaaraisia']:>12}")
    found = sum(r["loydetty"] for r in results)
    planted = sum(r["istutettu"] for r in results)
    extra = sum(r["ylimaaraisia"] for r in results)
    precision = found / (found + extra) if found + extra else 0.0
    print(f"\nRecall {found}/{planted} = {found / planted:.0%}  |  "
          f"Ylimääräisiä hälytyksiä keskimäärin {extra / len(results):.1f} / kk  |  Precision {precision:.0%}")


if __name__ == "__main__":
    main()
