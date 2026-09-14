"""The reasoning layer. Not a search engine, and never the default path.

OpenAI is asked to do the things regular expressions genuinely cannot: decide
which of six people called "Virat Kohli" the user meant, and read a sentence
that a keyword list would misjudge. Everything it is given has already been
retrieved from a real source, and everything it returns is validated here
before the pipeline will act on it.

Three rules hold this in place:

  no key, no change   Without OPENAI_API_KEY the pipeline behaves exactly as
                      it did before. Ranking falls back to the deterministic
                      scorer. Nothing degrades silently and nothing breaks.

  cached like a source  Calls go through the shared Fetcher, so an identical
                      prompt is answered from disk. Searching "Virat Kohli"
                      twice is billed once.

  evidence in, JSON out  The model never sees a URL it should fetch and is
                      never asked for facts from memory. It is given the
                      candidates a free source already returned and asked to
                      order them.

It is called over plain HTTPS rather than through the `openai` SDK so that the
project keeps its existing dependency list: `pip install` is not needed to
turn this on, only a key.
"""

from __future__ import annotations

import json

from .. import config


class OpenAIClient:
    """Chat completions, budgeted and metered.

    `budget` is the most calls this client may make. It exists because a loop
    bug should cost one call, not an account.
    """

    def __init__(self, fetcher, budget: int, purpose: str,
                 model: str | None = None):
        self.fetcher = fetcher
        self.model = model or config.OPENAI_MODEL
        self.purpose = purpose
        self.budget = budget
        self.calls = 0

    @property
    def available(self) -> bool:
        return bool(config.OPENAI_API_KEY) and self.calls < self.budget

    def complete_json(self, system: str, user: str,
                      max_tokens: int = 700) -> dict | None:
        """One JSON-mode completion. None on any failure, never an exception.

        A missing or malformed answer must leave the caller on its
        deterministic path, so every error here is a note rather than a raise.
        """
        if not self.available:
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
            self.fetcher.meter.record_tokens(
                self.purpose,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                cached=cached,
            )

        self.calls += 1
        data = self.fetcher.post_json(
            config.OPENAI_CHAT,
            payload,
            {
                "Authorization": f"Bearer {config.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            attempts=2,
            on_response=on_response,
            timeout=config.OPENAI_TIMEOUT,
        )
        if not data:
            self.fetcher.note(
                "OpenAI did not answer; falling back to the deterministic path."
            )
            return None

        try:
            content = data["choices"][0]["message"]["content"]
            answer = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError):
            self.fetcher.note("OpenAI returned a reply that was not usable JSON.")
            return None

        return answer if isinstance(answer, dict) else None


def describe_configuration() -> dict:
    """What the UI should say about the reasoning layer's availability."""
    return {
        "configured": bool(config.OPENAI_API_KEY),
        "model": config.OPENAI_MODEL if config.OPENAI_API_KEY else None,
        "note": (
            "OpenAI ranks identity candidates and re-reads flagged articles."
            if config.OPENAI_API_KEY
            else "No OPENAI_API_KEY set. Candidates are ranked by the "
                 "deterministic scorer, which is free and needs no key."
        ),
    }
