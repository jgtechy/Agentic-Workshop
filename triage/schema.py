"""The triage decision schema: every decision the agent makes must match it."""

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Category = Literal["billing", "bug", "access", "performance", "how-to"]
Priority = Literal["P1", "P2", "P3", "P4"]
Route = Literal["billing-team", "bug-team", "access-team", "performance-team", "how-to-team"]

# A sentence end (. ! ?), optionally closed by a quote or bracket, then whitespace and
# more text means a second sentence.
_SENTENCE_BREAK = re.compile(r"[.!?][\"')\]]*\s+\S")


class TriageDecision(BaseModel):
    """Where a support ticket goes, how urgent it is, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: Category = Field(description="The ticket's category from the triage policy.")
    priority: Priority = Field(description="P1 is the most urgent, P4 the least.")
    route: Route = Field(description="The team the ticket is routed to.")
    rationale: str = Field(description="One sentence naming the policy rule that was applied.")

    @field_validator("rationale")
    @classmethod
    def _one_sentence(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        if _SENTENCE_BREAK.search(text):
            raise ValueError("must be a single sentence")
        return text


class TriageValidationError(ValueError):
    """A decision does not match the schema. The message names every offending field."""


def validate_decision(payload: str | bytes | dict) -> TriageDecision:
    """Validate a decision given as a dict or as JSON text."""
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except ValueError as error:  # bad JSON, bad UTF-8, or an integer too long to parse
            raise TriageValidationError(f"Decision is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise TriageValidationError(f"Decision must be a JSON object, got {type(payload).__name__}")
    try:
        return TriageDecision.model_validate(payload)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'decision'}: {e['msg'].removeprefix('Value error, ')}" for e in error.errors()
        )
        raise TriageValidationError(f"Invalid triage decision: {problems}") from error
