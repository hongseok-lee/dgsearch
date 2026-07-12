from __future__ import annotations


def is_tradable(article: dict) -> bool:
    """Return true only for listings the API marks as currently ongoing."""
    return article.get("status") == "Ongoing"

