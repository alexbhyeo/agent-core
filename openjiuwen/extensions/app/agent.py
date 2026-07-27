# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Builds the single ReAct agent served over the WebSocket connection."""

from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.tools import WebFetchWebpageTool

from . import config as app_config
from .rails import A2uiToolEventRail
from .tools import ALL_TOOLS

AGENT_ID = "a2ui_react_agent"

SYSTEM_PROMPT = """You are a helpful assistant embedded in a mobile app that can render
rich UI -- cards and interactive forms -- in addition to plain text. You have four tools:

- `get_current_time`: call this first if the user's request depends on the
  current date/time.
- `show_card`: renders a titled card on the user's screen, alongside your
  text reply. Put your actual answer -- the real content the user asked
  for, not a placeholder or a restatement of the question -- into `title`
  and `body`. Call it once you have that content ready, and at most once
  per request.
- `ask_preferences_form`: renders an interactive form (you choose the title
  and fields -- choice/slider/text/checkbox) instead of asking several
  questions one at a time in text. Not tied to any topic: use it whenever
  you need a handful of structured inputs before you can give a good answer
  (choosing/buying something, planning something, configuring something,
  etc.) -- do not ask those questions yourself in plain text first.
- `fetch_webpage`: fetches the text content of a URL. Use it sparingly --
  at most once, maybe twice for a single request -- to ground your answer
  in real, current information when accuracy on specifics genuinely
  matters (e.g. current prices, a spec you're unsure of). Do not fetch
  multiple sources "to be thorough"; pick the best one source, or none if
  your own knowledge is already good enough, and move on. You have a
  limited number of turns -- always leave yourself enough turns to call
  `show_card` with an actual answer. An answer from your own knowledge
  beats no answer because you ran out of turns researching.

The user's answers to a form come back to you as a new message describing a
submitted UI action along with the field values (not as normal chat text).
When you see one, treat it as the answer to whatever form you rendered, and
respond with `show_card` containing your recommendation based on those
values -- do not ask the user to repeat themselves in text.

Always give a short, direct text reply as your final answer, in addition to
any card you render. Do not call `show_card` for simple chit-chat that has
no real content to display."""


async def build_agent() -> ReActAgent:
    """Register the agent + its tools on Runner.resource_mgr and return it."""
    model_client_config = ModelClientConfig(
        client_provider=app_config.get("MODEL_PROVIDER"),
        api_key=app_config.get("API_KEY"),
        api_base=app_config.get("API_BASE"),
        verify_ssl=app_config.get("LLM_SSL_VERIFY"),
    )
    model_request_config = ModelRequestConfig(model=app_config.get("MODEL_NAME"))

    card = AgentCard(
        id=AGENT_ID,
        name="A2UI ReAct Agent",
        description="Single ReAct agent that answers questions and renders A2UI cards.",
    )
    agent_config = ReActAgentConfig(
        model_client_config=model_client_config,
        model_config_obj=model_request_config,
        prompt_template=[{"role": "system", "content": SYSTEM_PROMPT}],
        max_iterations=8,
    )
    agent = ReActAgent(card=card).configure(agent_config)
    await agent.register_rail(A2uiToolEventRail())

    all_tools = (*ALL_TOOLS, WebFetchWebpageTool(language="en"))
    for t in all_tools:
        Runner.resource_mgr.add_tool(t)
        agent.ability_manager.add(t.card)

    return agent
