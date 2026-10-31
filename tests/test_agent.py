import json
from types import SimpleNamespace as NS

import pytest

from ostoagentti.agent import Agent
from ostoagentti.config import llm_settings


def _call(cid, name, args):
    return NS(id=cid, type="function", function=NS(name=name, arguments=json.dumps(args)))


def _msg(content=None, calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls))])


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.script.pop(0) if self.script else _msg(calls=[_call("x", "describe_schema", {})])


def test_tool_loop_recovers_from_sql_error(tools):
    client = FakeClient([
        _msg(calls=[_call("c1", "run_sql", {"sql": "SELECT ei_ole FROM laskut"})]),
        _msg(calls=[_call("c2", "run_sql", {"sql": "SELECT round(sum(tiliointisumma)) AS eur FROM laskut"})]),
        _msg(content="Yhteensä X €."),
    ])
    res = Agent(tools, client, "fake").run("Paljonko ostettiin?")
    assert res.answer == "Yhteensä X €." and not res.hit_step_limit
    assert [s["ok"] for s in res.steps] == [False, True]
    tool_msgs = [m for m in client.requests[-1]["messages"] if m["role"] == "tool"]
    assert "SQL-virhe" in tool_msgs[0]["content"]
    assert json.loads(tool_msgs[1]["content"])["rivit"][0][0] > 0


def test_step_limit_forces_final_answer_without_tools(tools):
    client = FakeClient([])
    client.script = [_msg(calls=[_call(f"c{i}", "describe_schema", {})]) for i in range(3)] + [_msg(content="Loppu")]
    res = Agent(tools, client, "fake", max_steps=3).run("kysymys")
    assert res.hit_step_limit and res.answer == "Loppu"
    assert "tools" not in client.requests[-1]


def test_bad_arguments_are_reported(tools):
    bad = NS(id="b", type="function", function=NS(name="run_sql", arguments="ei json"))
    client = FakeClient([_msg(calls=[bad]), _msg(content="ok")])
    res = Agent(tools, client, "fake").run("q")
    assert res.steps[0]["ok"] is False


def test_large_result_stays_valid_json(tools):
    agent = Agent(tools, FakeClient([]), "fake", max_tool_chars=300)
    content = agent._tool_content(tools.run_sql("SELECT toimittaja_nimi, lasku_id FROM laskut"))
    data = json.loads(content)
    assert data["katkaistu"] is True and len(data["rivit"]) < 50


def test_report_runs_detectors_first(tools, synth):
    client = FakeClient([_msg(content="## Yhteenveto\n...")])
    res, results = Agent(tools, client, "fake").report(synth["truth"]["kohdekuukausi"])
    assert "piikit" in results
    assert synth["truth"]["piikki_toimittaja"]["toimittaja_y_tunnus"] in client.requests[0]["messages"][1]["content"]


def test_llm_settings_presets():
    s = llm_settings({"GEMINI_API_KEY": "k"})
    assert s.provider == "gemini" and s.base_url.startswith("https://generativelanguage")
    assert llm_settings({"LLM_PROVIDER": "ollama"}).api_key == "ollama"
    assert llm_settings({"LLM_PROVIDER": "github", "GITHUB_TOKEN": "t", "LLM_MODEL": "m"}).model == "m"
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        llm_settings({"LLM_PROVIDER": "azure", "AZURE_OPENAI_API_KEY": "k", "LLM_MODEL": "d"})
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        llm_settings({})


def test_on_step_reports_each_tool_call(tools):
    client = FakeClient([
        _msg(calls=[_call("c1", "describe_schema", {}), _call("c2", "run_sql", {"sql": "SELECT 1"})]),
        _msg(content="ok"),
    ])
    seen = []
    Agent(tools, client, "fake").run("q", on_step=seen.append)
    assert [s["tool"] for s in seen] == ["describe_schema", "run_sql"]
