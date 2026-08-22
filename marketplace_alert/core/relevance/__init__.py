"""Relevance filtering: decides whether a marketplace's search result
actually matches what a saved search asked for, before it's ever treated
as discovered/new (persisted, counted, or notified about).

Triggered by a real observed problem: a "Makita drill" saved search
returned Etsy listings for DeWalt/Milwaukee battery holders and generic
tool organizers - technically keyword hits, not results anyone actually
wants. See PROJECT_CONTEXT.md/ARCHITECTURE.md "Relevance filtering" for
the full design and policy write-up.

Connectors remain retrieval-only - this package never touches a
connector, and no connector imports anything from here (see
`connectors/*/connector.py`, unchanged). The one integration point is
`core/relevance/service.py:filter_relevant_listings()`, called from
`core/saved_searches/runner.py` and `main.py`'s `/scan` route - the two
places (and only two) that ever turn connector results into persisted/
notified listings.
"""

from marketplace_alert.core.relevance.evaluator import evaluate_relevance
from marketplace_alert.core.relevance.models import RelevanceEvaluation, RelevanceFilterResult
from marketplace_alert.core.relevance.service import filter_relevant_listings

__all__ = [
    "evaluate_relevance",
    "filter_relevant_listings",
    "RelevanceEvaluation",
    "RelevanceFilterResult",
]
