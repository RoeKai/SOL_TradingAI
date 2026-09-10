"""Ordered parallel read-only evaluation equals the same serial function."""
from concurrent.futures import ProcessPoolExecutor
import pytest
from test_stage08d_quantification import inputs
from scripts.attribute_8d_parallel import initialize,evaluate_row


@pytest.mark.parametrize('workers',[1,2])
def test_readonly_worker_count_does_not_change_candidate_comparison(tmp_path,workers):
    p,r,args,now=inputs(tmp_path)
    context=(args[1],args[3],args[2],r.run)
    jobs=[(n,args[0].candidate_id,args[0].model_dump(mode='json')) for n in range(2)]
    initialize(context);expected=list(map(evaluate_row,jobs))
    with ProcessPoolExecutor(max_workers=workers,initializer=initialize,initargs=(context,)) as pool:
        actual=list(pool.map(evaluate_row,jobs))
    assert actual==expected
    assert all(x['groups']['D']['result'] in ('APPROVE','REDUCE') for x in actual)
    assert p.summary()['counts']['orders']==0
