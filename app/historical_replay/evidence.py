"""Instance review of stored historical prefixes, not the 8B synthetic verifier.

Only IDs already issued by this instance's replay ingestor are reviewable.
An external source name, hash, candidate JSON or verified flag is not accepted.
This is local dataset integrity evidence, NOT real-account/exchange authority.
"""
from app.admission.models import AdmissionRequest, fingerprint
from app.offline_paper.storage import get, digest
from .storage import hget
from .models import HistoricalCandidate
from .provider import describe
from .data import verify_seal, HistoricalError

VERIFIER='historical-prefix-review/v1'


def review(store,db,candidate_id,*,now,allow_expired_history=False):
    raw=hget(db,'history_candidates',candidate_id)
    if raw is None: raise HistoricalError('NO_INSTANCE_ISSUED_HISTORICAL_CANDIDATE')
    c=HistoricalCandidate.model_validate(raw);run=store.run(db)
    dataset=verify_seal(hget(db,'history_meta','dataset'))
    identity=get(db,'identity','identity')
    cursor=hget(db,'history_cursor','cursor')
    ev=c.evidence
    bindings=(c.dataset_digest==dataset['content_digest']==run['dataset_digest'] and
        c.run_digest==run['content_digest']==cursor['run_digest'] and c.bundle_digest==identity['bundle_digest'] and
        run['instance_id']==store.instance_id and dataset['status']=='COMPLETE')
    if not bindings: raise HistoricalError('HISTORICAL_INSTANCE_OR_DATASET_BINDING_FAILED')
    if not allow_expired_history and now>=ev['expires_at']: raise HistoricalError('HISTORICAL_CANDIDATE_EXPIRED')
    for sample in ev['window']:
        stored=hget(db,'history_samples',str(sample['at_ms']))
        if stored!=sample or sample['at_ms']>cursor['at_ms']:
            raise HistoricalError('SAMPLE_NOT_IN_CONSUMED_HISTORICAL_PREFIX')
        for observation in sample['last'].values():
            file=next((f for f in dataset['files'] if f['file']==observation['source_file']),None)
            if file is None or file['sha256']!=observation['source_digest']:
                raise HistoricalError('OBSERVATION_NOT_BOUND_TO_VERIFIED_ARCHIVE')
            if observation['available_at_ms']>sample['at_ms'] or observation['event_time_ms']>observation['available_at_ms']:
                raise HistoricalError('FUTURE_OBSERVATION_IN_EVIDENCE')
    rebuilt=describe(store.bundle(db),store.model(db),run,c.setup.side,ev['window'],ev['level'],
        now=int(c.setup.created_at),budget_notional=ev['funding_budget_basis']['notional_basis'])
    computed_equality=rebuilt.model_dump(mode='json')==c.model_dump(mode='json')
    if not computed_equality: raise HistoricalError('HISTORICAL_STRUCTURE_RECOMPUTATION_FAILED')
    # Review outcome is computed from stored source/timeline/geometry equality.
    # This name is a NEW explicit 8C policy verifier, never paper-structure-review/v1.
    return c,computed_equality


def admission_request(store,db,candidate_id,request_id,risk,leverage,now):
    c,reviewed=review(store,db,candidate_id,now=now)
    s=c.setup
    return AdmissionRequest(request_id=request_id,risk_budget_usdt=risk,action='OPEN',leverage=leverage,
        margin_mode='ISOLATED',auto_add_margin=False,loss_recovery_sizing=False,sizing_basis='quality_risk_budget',
        confirmations=tuple(dict(evidence_id=e.evidence_id,evidence_digest=fingerprint(e),verified=reviewed,
            verifier=VERIFIER,checked_at=now) for e in s.structure_evidence),
        invalidation_review=dict(setup_digest=fingerprint(s),all_conditions_clear=reviewed,verifier=VERIFIER,checked_at=now))
