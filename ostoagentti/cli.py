"""Komentorivi.

  python -m ostoagentti.cli load data/raw/*.csv
  python -m ostoagentti.cli schema
  python -m ostoagentti.cli sql "SELECT hankintakategoria, sum(tiliointisumma) FROM laskut GROUP BY 1 ORDER BY 2 DESC"
  python -m ostoagentti.cli anomalies --month 2025-09        (ei LLM:ää, ilmainen)
  python -m ostoagentti.cli ask "Mikä hankintakategoria kasvoi eniten 2025?"
  python -m ostoagentti.cli report --month 2025-09 --out raportti.md
  python -m ostoagentti.cli export demo/laskut_2025.parquet           (Streamlit-demon data)
  python -m ostoagentti.cli esimerkit --months 2025-09 2025-12       (demon valmiit vastaukset)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .data import export_parquet, load_csv
from .esimerkit import DEFAULT_PATH as EXAMPLES_PATH
from .tools import ToolError, Tools

DEFAULT_DB = "data/ostot.duckdb"


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _print_steps(steps: list[dict]) -> None:
    print("\n--- Työkalukutsut ---", file=sys.stderr)
    for i, s in enumerate(steps, 1):
        detail = s["args"].get("sql") or s["args"].get("month") or ""
        status = "ok" if s["ok"] else f"virhe: {s['error']}"
        print(f"{i}. {s['tool']} [{status}] {detail}".strip(), file=sys.stderr)


def _agent(db: str):
    from .agent import Agent
    from .config import make_client

    client, model = make_client()
    return Agent(Tools(db), client, model)


def main(argv: list[str] | None = None) -> int:
    # Windows kirjoittaa ohjatun tulosteen muuten cp1252:na, ja vieraat merkit kaatavat tulostuksen.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="ostoagentti", description="Ostodata-agentti")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"DuckDB-tiedosto (oletus {DEFAULT_DB})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("load", help="Lataa CSV-tiedostot")
    p.add_argument("files", nargs="+")
    p.add_argument("--encoding", default="utf-8")
    sub.add_parser("schema", help="Näytä skeema")
    p = sub.add_parser("sql", help="Aja vain luku -kysely")
    p.add_argument("query")
    p = sub.add_parser("anomalies", help="Aja tunnistimet (ei LLM:ää)")
    p.add_argument("--month", required=True)
    p = sub.add_parser("ask", help="Kysy agentilta")
    p.add_argument("question")
    p = sub.add_parser("report", help="Havaintoraportti kuukaudelle")
    p.add_argument("--month", required=True)
    p.add_argument("--out")
    p = sub.add_parser("export", help="Vie tietokanta Parquet-tiedostoksi (Streamlit-demo)")
    p.add_argument("out")
    p = sub.add_parser("esimerkit", help="Laske demon esimerkkivastaukset valmiiksi (käyttää LLM:ää)")
    p.add_argument("--months", nargs="*", default=[], help="Kuukaudet, joille kirjoitetaan valmis raportti")
    p.add_argument("--out", default=str(EXAMPLES_PATH))

    args = ap.parse_args(argv)
    try:
        if args.cmd == "load":
            _print_json(load_csv(args.files, args.db, encoding=args.encoding))
            return 0
        if not Path(args.db).exists():
            print(f"Tietokantaa {args.db} ei löydy. Aja ensin: load <csv>", file=sys.stderr)
            return 2
        if args.cmd == "schema":
            _print_json(Tools(args.db).describe_schema())
        elif args.cmd == "sql":
            _print_json(Tools(args.db).run_sql(args.query))
        elif args.cmd == "anomalies":
            _print_json(Tools(args.db).detect_anomalies(args.month))
        elif args.cmd == "ask":
            res = _agent(args.db).run(args.question)
            print(res.answer)
            _print_steps(res.steps)
        elif args.cmd == "report":
            res, _ = _agent(args.db).report(args.month)
            if args.out:
                Path(args.out).write_text(res.answer, encoding="utf-8")
                print(f"Raportti: {args.out}")
            else:
                print(res.answer)
            _print_steps(res.steps)
        elif args.cmd == "export":
            _print_json(export_parquet(args.db, args.out))
        elif args.cmd == "esimerkit":
            from .esimerkit import precompute

            data = precompute(_agent(args.db), args.months, args.out)
            print(f"{len(data['kysymykset'])} kysymystä ja {len(data['raportit'])} raporttia -> {args.out}")
        return 0
    except (ToolError, ValueError) as e:
        print(f"Virhe: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
