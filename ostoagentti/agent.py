"""Agenttisilmukka (tool use). LLM valitsee työkalut ja tulkitsee tulokset; luvut tulevat työkaluista."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .tools import TOOL_SPECS, Tools, dumps

SYSTEM_PROMPT = """Olet data-analyytikkoagentti. Käytössäsi on DuckDB-taulu `laskut`:
Suomen julkisen sektorin ostolaskut (Tutkihankintoja.fi-data).

Säännöt:
- Älä koskaan keksi tai arvioi lukuja. Jokainen luku vastauksessa tulee työkalun tuloksesta.
- Jos et tunne saraketta, aja ensin describe_schema.
- Yksi rivi on tiliöintirivi. Laskun summa = SUM(tiliointisumma) GROUP BY lasku_id.
- Aggregoi tietokannassa (GROUP BY, ORDER BY, LIMIT). Älä hae raakarivejä.
- Toimittajia vertaillessa ryhmittele toimittaja_y_tunnus-sarakkeella ja rajaa NULL-tunnukset pois (anonymisoidut).
- Jos kysely palauttaa virheen, korjaa se ja yritä uudelleen.
- Poikkeama ei ole väärinkäytös. Kuvaa havainto neutraalisti ja kerro, mitä kannattaisi tarkistaa.
- Vastaa suomeksi, tiiviisti. Pyöristä eurot tuhansiin (esim. 1,2 milj. €, 340 t€).
"""

REPORT_PROMPT = """Kirjoita havaintoraportti kuukaudelle {month} alla olevien tunnistintulosten perusteella.
Voit tarkentaa havaintoja run_sql-kyselyillä (esim. mistä laskuista piikki koostuu), mutta älä toista
tunnistimia. Rakenne (markdown):

## Yhteenveto
2–3 virkettä.
## Havainnot
Tärkein ensin. Jokaisesta: mitä havaittiin, luvut, miksi sillä voi olla merkitystä.
## Tarkistettavaa
Konkreettiset jatkotoimet.

Tunnistintulokset (JSON):
{results}
"""

FINAL_NUDGE = "Työkalukierrokset loppuivat. Kirjoita nyt lopullinen vastaus saamiesi tulosten perusteella."


def _tool_call_message(c) -> dict:
    """Työkalukutsu historiaan. Gemini 3 palauttaa extra_content-kentässä thought_signaturen,
    joka on lähetettävä takaisin sellaisenaan; muuten seuraava kutsu päättyy 400-virheeseen."""
    out = {"id": c.id, "type": "function",
           "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
    extra = getattr(c, "extra_content", None)
    if extra:
        out["extra_content"] = extra
    return out


@dataclass
class AgentResult:
    answer: str
    steps: list[dict] = field(default_factory=list)
    hit_step_limit: bool = False


class Agent:
    def __init__(self, tools: Tools, client, model: str, max_steps: int = 8,
                 max_tool_chars: int = 6000, temperature: float = 0.0):
        self.tools = tools
        self.client = client
        self.model = model
        self.max_steps = max_steps
        self.max_tool_chars = max_tool_chars
        self.temperature = temperature

    def _complete(self, messages: list[dict], with_tools: bool = True):
        kwargs = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if with_tools:
            kwargs.update(tools=TOOL_SPECS, tool_choice="auto")
        return self.client.chat.completions.create(**kwargs).choices[0].message

    def _tool_content(self, result: dict) -> str:
        text = dumps(result)
        if len(text) <= self.max_tool_chars:
            return text
        # Pidetään JSON validina: lyhennetään rivilistaa, ei katkaista merkkijonoa.
        if isinstance(result.get("rivit"), list):
            trimmed = dict(result)
            while trimmed["rivit"] and len(dumps(trimmed)) > self.max_tool_chars:
                trimmed["rivit"] = trimmed["rivit"][: max(1, len(trimmed["rivit"]) // 2)]
                trimmed["katkaistu"] = True
                if len(trimmed["rivit"]) == 1:
                    break
            return dumps(trimmed)
        return dumps({"error": "Tulos liian suuri. Aggregoi tai rajaa kyselyä."})

    def run(self, question: str, on_step=None) -> AgentResult:
        """on_step(step) kutsutaan jokaisen työkalukutsun jälkeen, jotta käyttöliittymä voi näyttää etenemisen."""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": question}]
        steps: list[dict] = []
        for _ in range(self.max_steps):
            msg = self._complete(messages)
            calls = msg.tool_calls or []
            if not calls:
                return AgentResult(msg.content or "", steps)
            messages.append({
                "role": "assistant",
                "content": msg.content or None,
                "tool_calls": [_tool_call_message(c) for c in calls],
            })
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                    if not isinstance(args, dict):
                        raise ValueError
                    result = self.tools.call(c.function.name, args)
                except (json.JSONDecodeError, ValueError):
                    args, result = {}, {"error": "Argumentit eivät olleet JSON-objekti"}
                steps.append({
                    "tool": c.function.name,
                    "args": args,
                    "ok": "error" not in result,
                    "error": result.get("error"),
                    "rows": result.get("row_count"),
                })
                if on_step:
                    on_step(steps[-1])
                messages.append({"role": "tool", "tool_call_id": c.id, "content": self._tool_content(result)})
        messages.append({"role": "user", "content": FINAL_NUDGE})
        msg = self._complete(messages, with_tools=False)
        return AgentResult(msg.content or "", steps, hit_step_limit=True)

    def report(self, month: str, on_step=None) -> tuple[AgentResult, dict]:
        """Ajaa tunnistimet deterministisesti ja pyytää LLM:ltä tulkinnan."""
        results = self.tools.detect_anomalies(month)
        prompt = REPORT_PROMPT.format(month=month, results=dumps(results))
        return self.run(prompt, on_step), results
