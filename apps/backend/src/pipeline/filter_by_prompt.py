import asyncio
import logging

from pydantic import BaseModel, Field

from src.clients.openai_client import QUERY_GEN_MODEL, get_openai_client
from src.db.models import Lead

logger = logging.getLogger(__name__)

BATCH_SIZE = 12
LLM_CONCURRENCY = 5
MAX_CONTEXT_CHARS = 500


class FilterDecision(BaseModel):
    invite_id: str = Field(description="The lead's invite_id, copied verbatim from the prompt")
    matches: bool = Field(description="True if the group clearly fits the user's category")
    reason: str = Field(description="One short sentence (<120 chars) citing specifics from the text")


class FilterBatch(BaseModel):
    decisions: list[FilterDecision]


SYSTEM_PROMPT = """\
You categorize WhatsApp groups against a user-provided category description.
For EACH input group, decide if it clearly fits the category.

Rules:
- "matches: true" ONLY if the group clearly fits. When in doubt, false.
- "reason" is ONE short sentence (<120 chars) citing specifics from the text.
- Return one decision per input invite_id, copied verbatim.
"""


def _format_lead_block(leads: list[Lead]) -> str:
    """Lead presented to the LLM with verified WhatsApp group name first
    (highest signal), description (rare), and scraped source-text fallback."""
    parts: list[str] = []
    for ld in leads:
        lines = [f"[{ld.invite_id}]"]
        if ld.verified_group_name:
            lines.append(f"  group_name: {ld.verified_group_name}")
        if ld.verified_group_description:
            lines.append(f"  group_description: {ld.verified_group_description}")
        ctx = (ld.source_text or "").strip().replace("\n", " ")
        if len(ctx) > MAX_CONTEXT_CHARS:
            ctx = ctx[:MAX_CONTEXT_CHARS] + "…"
        if ctx:
            lines.append(f"  source_context: {ctx}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def _filter_batch(category: str, batch: list[Lead]) -> list[FilterDecision]:
    client = get_openai_client()
    user_msg = f"Category: {category}\n\nGroups:\n\n{_format_lead_block(batch)}"
    completion = await client.chat.completions.parse(
        model=QUERY_GEN_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        response_format=FilterBatch,
    )
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        return []
    return parsed.decisions


async def filter_leads(category: str, leads: list[Lead]) -> dict[str, FilterDecision]:
    """Run an LLM yes/no classification of `leads` against a free-form `category` string.
    Returns a dict keyed by invite_id; missing entries silently dropped."""
    if not leads:
        return {}
    batches = [leads[i : i + BATCH_SIZE] for i in range(0, len(leads), BATCH_SIZE)]
    sem = asyncio.Semaphore(LLM_CONCURRENCY)

    async def _run(b: list[Lead]) -> list[FilterDecision]:
        async with sem:
            try:
                return await _filter_batch(category, b)
            except Exception as e:
                logger.exception("filter batch failed (size=%d): %s", len(b), e)
                return []

    results = await asyncio.gather(*[_run(b) for b in batches])
    return {d.invite_id: d for batch in results for d in batch}
