"""Stage 5: Append-only outcome capture, re-profiling, and holdout backtesting."""

from feedback.backtest import (
    BacktestComparisonReport,
    BacktestMetrics,
    HoldoutSplit,
    compare_profile_versions,
    evaluate_profile_holdout,
    run_holdout_backtest,
    split_holdout_won_customers,
    train_backtest_profile,
)
from feedback.outcomes import (
    RESOLVED_OUTCOME_STATUSES,
    VALID_OUTCOME_STATUSES,
    ClientResolvedOutcome,
    InMemoryOutcomeStore,
    LeadOutcomeRecord,
    OutcomeStore,
    PostgresOutcomeStore,
    ReprofileResult,
    combine_seeds_and_resolved_outcomes,
    import_outcomes_csv,
    reprofile_client,
    validate_outcome_status,
)

__all__ = [
    "RESOLVED_OUTCOME_STATUSES",
    "VALID_OUTCOME_STATUSES",
    "BacktestComparisonReport",
    "BacktestMetrics",
    "ClientResolvedOutcome",
    "HoldoutSplit",
    "InMemoryOutcomeStore",
    "LeadOutcomeRecord",
    "OutcomeStore",
    "PostgresOutcomeStore",
    "ReprofileResult",
    "combine_seeds_and_resolved_outcomes",
    "compare_profile_versions",
    "evaluate_profile_holdout",
    "import_outcomes_csv",
    "reprofile_client",
    "run_holdout_backtest",
    "split_holdout_won_customers",
    "train_backtest_profile",
    "validate_outcome_status",
]
