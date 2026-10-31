import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "synth"))

from make_data import generate  # noqa: E402
from ostoagentti.data import load_csv  # noqa: E402
from ostoagentti.tools import Tools  # noqa: E402


@pytest.fixture(scope="session")
def synth(tmp_path_factory):
    out = tmp_path_factory.mktemp("synth")
    res = generate(out, scale=0.5, seed=7)
    db = out / "ostot.duckdb"
    summary = load_csv([res["csv"]], db)
    return {"db": db, "truth": res["truth"], "summary": summary}


@pytest.fixture()
def tools(synth):
    t = Tools(synth["db"])
    yield t
    t.close()


def write_csv(path: Path, header: list[str], rows: list[list[str]], delim: str, bom: bool = False) -> Path:
    text = delim.join(header) + "\n" + "\n".join(delim.join(r) for r in rows) + "\n"
    path.write_text(("﻿" if bom else "") + text, encoding="utf-8")
    return path


def invoice_row(lasku, pvm, summa, toimittaja="Demo A Oy", ytunnus="3000001-1"):
    return [lasku, "Yksikkö", "1000000-0", "Ylä", "2000000-0", ytunnus, toimittaja,
            "4310", "Asiantuntijapalvelut", "Konsultointi", pvm, summa, "Valtio"]


def load_truth(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
