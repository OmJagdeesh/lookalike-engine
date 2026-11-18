"""Stage 3.3: Single-call plain-language 3-sentence ICP summary generator."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from profile.lift import LiftTrait

from google import genai

DEFAULT_LLM_MODEL = "gemini-2.5-flash"

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _format_trait_line(trait: LiftTrait) -> str:
    return (
        f"- {trait.feature}={trait.value} "
        f"(lift={trait.lift:.2f}x, won_share={trait.won_share:.0%}, "
        f"baseline_share={trait.baseline_share:.0%}, support={trait.support_count})"
    )


def build_icp_prompt(
    product_sold: str,
    traits: list[LiftTrait],
    *,
    top_n: int = 6,
) -> str:
    """Build a strict, PII-free prompt containing only aggregated lift traits."""
    positives = [t for t in sorted(traits, key=lambda x: -x.lift) if t.lift > 1.0][:top_n]
    negatives = [t for t in sorted(traits, key=lambda x: x.lift) if t.lift < 1.0][:top_n]

    pos_block = "\n".join(_format_trait_line(t) for t in positives) or "- None identified"
    neg_block = "\n".join(_format_trait_line(t) for t in negatives) or "- None identified"

    return (
        "You are summarizing an Ideal Customer Profile (ICP) for a B2B sales team in India.\n"
        f"Product sold by our client: {product_sold}\n\n"
        "Top positive distinguishing traits (more common in won deals):\n"
        f"{pos_block}\n\n"
        "Top negative traits (less common in won deals):\n"
        f"{neg_block}\n\n"
        "Instructions:\n"
        "- Write EXACTLY 3 clear, plain-English sentences describing who the best customers are "
        "and what traits distinguish them from lost or baseline accounts.\n"
        "- Do NOT output bullet points, headings, JSON, scores, or invented statistics.\n"
        "- Do NOT mention any company names, personal names, emails, or phone numbers."
    )


def _normalize_three_sentences(text: str) -> str:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        raise ValueError("LLM returned an empty ICP description.")
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(cleaned) if s.strip()]
    if len(sentences) > 3:
        sentences = sentences[:3]
    result = " ".join(sentences)
    if not result.endswith((".", "!", "?")):
        result += "."
    return result


def generate_icp_fallback_text(product_sold: str, traits: list[LiftTrait]) -> str:
    """Deterministic 3-sentence ICP summary for offline tests when no LLM key is set."""
    positives = [t for t in sorted(traits, key=lambda x: -x.lift) if t.lift > 1.0][:3]
    negatives = [t for t in sorted(traits, key=lambda x: x.lift) if t.lift < 1.0][:2]

    if positives:
        pos_desc = ", ".join(f"{t.feature.replace('_', ' ')} ({t.value})" for t in positives)
    else:
        pos_desc = "active B2B industrial accounts"

    if negatives:
        neg_desc = ", ".join(f"{t.feature.replace('_', ' ')} ({t.value})" for t in negatives)
        sentence_2 = f"Lost or lower-fit accounts more frequently exhibit {neg_desc}."
    else:
        sentence_2 = "Won accounts consistently stand out on operational stability and local fit."

    return (
        f"Ideal buyers for {product_sold} are businesses matching {pos_desc}. "
        f"{sentence_2} "
        "Sales reps should prioritize outreach to active GST-compliant prospects sharing these top traits."
    )


def generate_icp_text(
    product_sold: str,
    traits: list[LiftTrait],
    *,
    llm_callable: Callable[[str], str] | None = None,
    api_key: str | None = None,
    model: str | None = None,
    use_fallback_if_no_key: bool = False,
) -> str:
    """Generate a 3-sentence plain-language ICP summary using a single LLM call.

    The LLM writes summary prose only and never computes or alters numeric weights/scores.
    """
    prompt = build_icp_prompt(product_sold, traits)

    if llm_callable is not None:
        raw_text = llm_callable(prompt)
        return _normalize_three_sentences(raw_text)

    resolved_key = api_key or os.getenv("LLM_API_KEY", "").strip()
    if not resolved_key or resolved_key == "your_llm_api_key_here":
        if use_fallback_if_no_key:
            return generate_icp_fallback_text(product_sold, traits)
        raise RuntimeError(
            "LLM_API_KEY is not configured. Set LLM_API_KEY in .env or pass llm_callable."
        )

    resolved_model = model or os.getenv("LLM_MODEL", DEFAULT_LLM_MODEL)
    client = genai.Client(api_key=resolved_key)
    response = client.models.generate_content(model=resolved_model, contents=prompt)
    raw_text = response.text or ""
    return _normalize_three_sentences(raw_text)
