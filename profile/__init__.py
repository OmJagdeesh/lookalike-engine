"""Stage 3: Lift calculation, profile weights, and plain-language ICP summary."""

from profile.icp_text import (
    DEFAULT_LLM_MODEL,
    build_icp_prompt,
    generate_icp_fallback_text,
    generate_icp_text,
)
from profile.lift import (
    CATEGORICAL_FEATURES,
    LiftResult,
    LiftTrait,
    build_seed_feature_dataframe,
    calculate_lift,
    load_client_seed_dataframe,
)
from profile.weights import (
    DEFAULT_W_MAX,
    InMemoryProfileStore,
    PostgresProfileStore,
    ProfileRecord,
    ProfileStore,
    compute_feature_weights,
    create_profile_version,
)

__all__ = [
    "CATEGORICAL_FEATURES",
    "DEFAULT_LLM_MODEL",
    "DEFAULT_W_MAX",
    "InMemoryProfileStore",
    "LiftResult",
    "LiftTrait",
    "PostgresProfileStore",
    "ProfileRecord",
    "ProfileStore",
    "build_icp_prompt",
    "build_seed_feature_dataframe",
    "calculate_lift",
    "compute_feature_weights",
    "create_profile_version",
    "generate_icp_fallback_text",
    "generate_icp_text",
    "load_client_seed_dataframe",
]
