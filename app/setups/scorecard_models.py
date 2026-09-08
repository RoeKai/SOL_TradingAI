"""Stage 4 descriptive records. These are not TradeSetup.score or risk decisions."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import Fraction, Positive, Record, Score, Text, Timestamp

ScoreLabel = Literal['excellent', 'good', 'fair', 'poor', 'invalid']
DescriptionStatus = Literal['complete', 'partial', 'unavailable']
DIMENSIONS = ('direction_confidence', 'entry_quality', 'stop_loss_quality',
              'take_profit_quality', 'rr_quality', 'position_quality', 'execution_clarity')


class ScoringContext(Record):
    evaluated_at: Timestamp  # Supplied explicitly; never read the system clock.
    max_data_age_seconds: Positive = 300


class ScoreIssue(Record):
    code: Literal['missing', 'stale', 'unverified', 'not_applicable', 'unsupported']
    field: Text
    explanation: Text


class ScoreCheck(Record):
    code: Text
    max_points: Annotated[Positive, Field(le=100)]
    points: Score | None
    explanation: Text
    inputs: tuple[Text, ...]
    issues: tuple[ScoreIssue, ...] = ()

    @model_validator(mode='after')
    def consistent_points(self):
        if self.points is not None and self.points > self.max_points:
            raise ValueError('Points cannot exceed this descriptive check weight')
        if (self.points is None) != bool(self.issues):
            raise ValueError('Unknown points require issues; known points cannot hide missing inputs')
        return self


class QualityDescription(Record):
    score: Score | None
    label: ScoreLabel
    explanation: Text
    status: DescriptionStatus
    # Known contributions, NOT an imputed final score or a probability.
    known_points: Score
    coverage: Fraction
    issues: tuple[ScoreIssue, ...] = ()

    @model_validator(mode='after')
    def description_consistency(self):
        if self.score is None:
            if self.label != 'invalid' or self.status == 'complete' or not self.issues:
                raise ValueError('Unscorable descriptions must explicitly carry unknown status/issues')
        else:
            if self.label == 'invalid' or self.status != 'complete' or self.issues or self.coverage != 1:
                raise ValueError('A final score requires all rubric inputs, not approval')
            if self.known_points != self.score:
                raise ValueError('Complete score and known contribution must agree')
        return self


class DimensionScore(QualityDescription):
    checks: tuple[ScoreCheck, ...]


class OverallQuality(QualityDescription):
    summary: Text
    aggregation: Literal['equal_mean_first_seven_no_missing_imputation'] = 'equal_mean_first_seven_no_missing_imputation'


class Scorecard(Record):
    schema_version: Literal['trade-scorecard/v1'] = 'trade-scorecard/v1'
    rubric_version: Literal['plan-quality/v1'] = 'plan-quality/v1'
    setup_id: Text
    plan_version: Text
    symbol: Text
    side: Literal['LONG', 'SHORT']
    context: ScoringContext
    data_as_of: Timestamp | None
    valid_until: Timestamp | None
    rr_calculation_version: Literal['linear-usdt-rr/v1'] = 'linear-usdt-rr/v1'
    rr_status: Literal['complete', 'partial', 'unavailable']
    direction_confidence: DimensionScore
    entry_quality: DimensionScore
    stop_loss_quality: DimensionScore
    take_profit_quality: DimensionScore
    rr_quality: DimensionScore
    position_quality: DimensionScore
    execution_clarity: DimensionScore
    overall_trade_quality: OverallQuality
    input_missing_items: tuple[Text, ...]
    recorded_rejection_codes: tuple[Text, ...] = ()  # Carried only, never re-decided/cleared.
    interpretation: Literal['descriptive_heuristic_not_win_probability'] = 'descriptive_heuristic_not_win_probability'
    verification: Literal['supplied_plan_and_metadata_only'] = 'supplied_plan_and_metadata_only'
    admission_status: Literal['not_evaluated'] = 'not_evaluated'
    execution_authority: Literal['none'] = 'none'
    limitations: tuple[Text, ...] = (
        'Labels describe an uncalibrated rubric, not admission, safety, or win probability',
        'Direction confidence is the caller-supplied evidence confidence, not a direction prediction',
        'Evidence and prices are caller-supplied; freshness is metadata-only, not authentication',
        'Missing inputs are not safe defaults; known_points is not a completed score',
        'RR labels do not change prices, costs, fractions, position advice, or RR arithmetic',
        'No account balance, actual quantity, margin mode, leverage, daily limits, or order checks',
        'No target-hit probabilities, stop adequacy model, exit-path simulation, or execution guarantee',
        'No strategy selection, Signal conversion, main-flow integration, or live capability',
    )
