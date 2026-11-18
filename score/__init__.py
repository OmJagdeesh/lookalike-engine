"""Stage 4: Scoped candidate collection, exclusion filtering, deterministic scoring, and export."""

from score.collect import (
    MAX_INDUSTRIES_PER_COLLECTION,
    PLACES_CANDIDATE_SEARCH_SOURCE,
    CollectionReport,
    check_robots_txt_allowed,
    collect_from_csv,
    collect_from_directory_if_permitted,
    collect_from_places,
    validate_collection_scope,
)
from score.exclude import (
    INACTIVE_GST_STATUSES,
    ExcludedCandidate,
    ExclusionResult,
    filter_candidates,
    load_client_seed_business_ids,
)
from score.export import (
    EXPORT_COLUMNS,
    export_leads_csv,
    export_leads_csv_string,
    leads_to_export_rows,
)
from score.scorer import (
    InMemoryLeadScoreStore,
    LeadScoreRecord,
    LeadScoreStore,
    PostgresLeadScoreStore,
    ScoredLead,
    score_candidates,
)

__all__ = [
    "EXPORT_COLUMNS",
    "INACTIVE_GST_STATUSES",
    "MAX_INDUSTRIES_PER_COLLECTION",
    "PLACES_CANDIDATE_SEARCH_SOURCE",
    "CollectionReport",
    "ExcludedCandidate",
    "ExclusionResult",
    "InMemoryLeadScoreStore",
    "LeadScoreRecord",
    "LeadScoreStore",
    "PostgresLeadScoreStore",
    "ScoredLead",
    "check_robots_txt_allowed",
    "collect_from_csv",
    "collect_from_directory_if_permitted",
    "collect_from_places",
    "export_leads_csv",
    "export_leads_csv_string",
    "filter_candidates",
    "leads_to_export_rows",
    "load_client_seed_business_ids",
    "score_candidates",
    "validate_collection_scope",
]
