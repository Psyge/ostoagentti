"""Demon esimerkkikysymykset ja niiden valmiiksi lasketut vastaukset.

Valmiit vastaukset näytetään heti eivätkä kuluta LLM-kiintiötä. Päivitys (käyttää LLM:ää):
  python -m ostoagentti.cli esimerkit --months 2025-09 2025-12
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_PATH = Path("demo/esimerkit.json")

QUESTIONS = [
    "Mitkä viisi toimittajaa saivat eniten rahaa vuonna 2025?",
    "Miten ostot jakautuvat hankintakategorioittain?",
    "Minä kuukautena ostettiin eniten, ja paljonko?",
    "Mitkä hankintayksiköt ostivat eniten ICT-hankintoja?",
    "Kuinka suuri osa ostoista meni toimittajille, joiden tiedot on salattu?",
    "Mitä poikkeamia syyskuulta 2025 löytyy?",
]


def load(path: str | Path = DEFAULT_PATH) -> dict:
    p = Path(path)
    if not p.exists():
        return {"kysymykset": {}, "raportit": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def _save(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def precompute(agent, months: list[str], path: str | Path = DEFAULT_PATH,
               questions: list[str] = QUESTIONS) -> dict:
    """Ajaa agentin esimerkeille. Tallentaa jokaisen vastauksen heti, ettei kiintiövirhe hukkaa aiempia."""
    path = Path(path)
    data = load(path)
    for q in questions:
        res = agent.run(q)
        data["kysymykset"][q] = {"answer": res.answer, "steps": res.steps}
        _save(data, path)
    for m in months:
        res, _ = agent.report(m)
        data["raportit"][m] = {"answer": res.answer, "steps": res.steps}
        _save(data, path)
    return data
