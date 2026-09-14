"""The reasoning layer, asynchronously. Same contract as V1's client.

`affluense/sources/openai_client.py` stays the reference implementation, and V1
code reached through the bridge still uses it verbatim. This is the same thing
for the paths V2 drives directly: identical payload, `temperature` 0, JSON mode,
token counts taken from the API's own `usage` block, and a failure that returns
None rather than raising so the caller stays on its deterministic path.

Two differences, both about scheduling rather than meaning:

  concurrent      extraction batches are issued together instead of one after
                  another. V1's serial chain is most of the analysis stage.

  budget is the   V1 builds a fresh client -- and therefore a fresh budget --
  run's           per company, so its stated ceiling is really that ceiling
                  times the number of companies. Here the budget is passed in
                  by the orchestrator and belongs to the run. The transport
                  enforces a second, absolute ceiling underneath, which also
                  binds V1 clients coming through the bridge.
"""

from __future__ import annotations

import json

from affluense import config as v1config


class AsyncOpenAIClient:
    """Chat completions: budgeted, metered, cached, never raising."""

    def __init__(self, transport, budget, purpose: str,
                 model: str | None = None):
        self.transport = transport
        self.budget = budget
        self.purpose = purpose
        self.model = model or v1config.OPENAI_MODEL

    @property
    def configured(self) -> bool:
        return bool(v1config.OPENAI_API_KEY)

    @property
    def available(self) -> bool:
        return self.configured and self.budget.remaining > 0

    async def complete_json(self, system: str, user: str,
                            max_tokens: int = 700) -> dict | None:
        """One JSON-mode completion. None on any failure, never an exception."""
        if not self.configured:
            return None
        # Claimed before dispatch, in whatever order the orchestrator queued the
        # work, so a run that exhausts its budget always spends it on the same
        # batches.
        if not self.budget.claim(1):
            return None

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Determinism as far as the API allows it. Two identical runs
            # should not disagree about who a person is.
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }

        def on_response(parsed, cached):
            usage = (parsed or {}).get("usage") or {}
            self.transport.meter.record_tokens(
                self.purpose,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                cached=cached,
            )

        data = await self.transport.post_json(
            v1config.OPENAI_CHAT,
            payload,
            {
                "Authorization": f"Bearer {v1config.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            attempts=2,
            on_response=on_response,
            timeout=v1config.OPENAI_TIMEOUT,
        )
        if not data:
            # The call did not happen, so the slot goes back. Without this a
            # run that hit a transient outage would spend its budget on
            # nothing.
            self.budget.release(1)
            self.transport.note(
                "OpenAI did not answer; falling back to the deterministic path."
            )
            return None

        try:
            content = data["choices"][0]["message"]["content"]
            answer = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError):
            self.transport.note(
                "OpenAI returned a reply that was not usable JSON."
            )
            return None

        return answer if isinstance(answer, dict) else None
