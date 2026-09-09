"""Narrow offline CLI. Only specified module-local config/plan files are read."""

import argparse
from decimal import Decimal
import json
from pathlib import Path

from app.utils.paths import ModulePaths, read_text_nofollow
from .compiler import compile_bundle, issue
from .contracts import validate_contract
from .encoding import canonical, parse_text
from .examples import example_plan
from .inputs import PlanInputs
from .models import ContractValidationResult

ROOT = Path(__file__).absolute().parents[2]
FILES = {'main':'config.yaml','admission':'admission.yaml','exit':'exit-policy.yaml','manifest':'configuration.yaml','plan':'plan.json'}


def read_input(role, supplied):
    """No root option, parent search, .env, arbitrary extension or runtime startup."""
    path=Path(supplied)
    if path.is_absolute(): path=path.relative_to(ROOT)
    if '..' in path.parts or path.name!=FILES[role]:
        raise ValueError('INPUT_PATH_REFUSED')
    if path.as_posix()!=FILES[role] and not (len(path.parts)>=4 and path.parts[:2]==('examples','configuration')):
        raise ValueError('INPUT_SCOPE_REFUSED')
    paths=ModulePaths(ROOT)
    return read_text_nofollow(paths.file(path))


def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('DUPLICATE_JSON_KEY')
        result[key]=value
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description='Offline configuration contracts: NO execution or Live permission')
    for role in ('main','admission','exit','manifest'):
        parser.add_argument('--'+role,default=FILES[role])
    parser.add_argument('--plan',help='Explicit plan.json under examples/configuration/<case>/; optional declared records')
    parser.add_argument('--at',help='Explicit UTC Unix seconds; never reads the system clock')
    parser.add_argument('--example',choices=('config-valid','synonym-conflict','allocation-mismatch','runner-unmodeled'))
    parser.add_argument('--json',action='store_true',dest='json_output')
    parser.add_argument('--parameters',action='store_true',help='Include immutable bundle and parameter origins in JSON')
    args=parser.parse_args(argv)
    compilation=None
    try:
        if args.plan and args.example: raise ValueError('Use a plan OR an example')
        texts={role+'_text':read_input(role,getattr(args,role)) for role in ('main','admission','exit','manifest')}
        if args.example=='synonym-conflict':
            raw=parse_text(texts['main_text'])
            raw['strategies']['panic_rebound']['drop_threshold_pct']=-2
            raw['strategies']['panic_rebound']['drop_pct']=3
            texts['main_text']=canonical(raw)
        compilation=compile_bundle(**texts)
        inputs=None
        if args.plan:
            inputs=PlanInputs.model_validate(json.loads(read_input('plan',args.plan),object_pairs_hook=_pairs,
                parse_float=Decimal,parse_constant=lambda value: (_ for _ in ()).throw(ValueError('NONFINITE_JSON'))))
        elif args.example in ('allocation-mismatch','runner-unmodeled'):
            inputs=example_plan(args.example)
        result=validate_contract(compilation,inputs,evaluated_at=args.at)
        if args.json_output:
            output={'validation':result.model_dump(mode='json')}
            if args.parameters: output['bundle']=None if compilation.bundle is None else compilation.bundle.model_dump(mode='json')
            print(json.dumps(output,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False))
        else:
            print('Config parse: '+result.config_parsing+'; config consistency: '+result.config_consistency)
            print('Plan: '+result.plan_consistency+'; runtime metadata: '+result.runtime_metadata+'; trusted runtime: '+result.runtime_trust)
            print('Bundle: '+str(result.bundle_digest))
            for item in result.issues:
                print(item.severity+' '+item.reason_code+' ['+item.field_path+']: '+item.suggestion)
            print('NOT_INTEGRATED; execution_authority=none; live_allowed=false; no account or order access')
        if result.config_parsing=='FAIL' or result.config_consistency=='FAIL': return 2
        if inputs is not None and (result.plan_consistency!='PASS' or result.runtime_metadata!='PASS'): return 3
        return 0
    except (ValueError,TypeError,ArithmeticError,OSError):
        # Do not echo secrets, arbitrary file paths or source values in errors.
        problem=issue('OFFLINE_INPUT_REFUSED','offline_input','explicit module-local files',
            'invalid input; values and host paths withheld','bounded permitted files and supported strict input schemas',
            'Check local filenames/types/versions; there is no parent-directory fallback or secret loading')
        result=ContractValidationResult(bundle_digest=None if compilation is None or compilation.bundle is None else compilation.bundle.bundle_digest,
            config_parsing='FAIL' if compilation is None else compilation.parsing,
            config_consistency='NOT_EVALUATED' if compilation is None else compilation.consistency,
            plan_consistency='FAIL' if args.plan else 'NOT_EVALUATED',runtime_metadata='NOT_EVALUATED',issues=(problem,))
        output={'validation':result.model_dump(mode='json')}
        print(json.dumps(output,ensure_ascii=False) if args.json_output else 'OFFLINE_INPUT_REFUSED; no file fallback or execution permission')
        return 2


if __name__=='__main__':
    raise SystemExit(main())
