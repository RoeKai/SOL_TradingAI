"""Default public-market paper daemon; production live execution is sealed."""
import argparse
import asyncio
import json
import math
from pathlib import Path


async def run_headless(runtime, seconds=0):
    await runtime.start()
    try:
        if seconds:
            await asyncio.sleep(seconds)
        else:
            await asyncio.Event().wait()
    finally:
        await runtime.stop()
    return runtime.snapshot()


def main():
    parser = argparse.ArgumentParser(description='SOL AI: public-market paper trading only, no private orders')
    parser.add_argument('--isolation-smoke', action='store_true', help='Synthetic acceptance; strictly no network')
    parser.add_argument('--cycles', type=int, default=1, choices=(1, 2, 3))
    parser.add_argument('--headless', action='store_true', help='Paper engine without the dashboard or login token')
    parser.add_argument('--run-seconds', type=float, default=0, help='Headless bounded run (0 means until Ctrl+C)')
    parser.add_argument('--check', action='store_true', help='Validate local config and isolation without network/state writes')
    args = parser.parse_args()
    if args.run_seconds < 0 or args.run_seconds > 86400 or not math.isfinite(args.run_seconds):
        parser.error('--run-seconds must be finite and between 0 and 86400')
    try:
        root = Path(__file__).absolute().parent
        if args.isolation_smoke:
            from app.bootstrap import run_isolation_smoke
            result = run_isolation_smoke(root, args.cycles)
        else:
            from app.runtime import PaperRuntime
            runtime = PaperRuntime(root)
            if args.check:
                from app.strategies.engine import StrategyEngine
                StrategyEngine(runtime.config.strategies)
                result = {'config_valid': True, 'dry_run': True, 'live_capability': False, 'network': 'none'}
            elif args.headless:
                result = asyncio.run(run_headless(runtime, args.run_seconds))
                # Only the readiness/safety receipt is printed, not all state/logs.
                result = {k: result[k] for k in ('status', 'mode', 'dry_run', 'ready', 'data', 'safety')}
            else:
                if args.run_seconds:
                    parser.error('--run-seconds requires --headless')
                from app.dashboard.server import create_app
                import uvicorn
                application = create_app(runtime, runtime.config, runtime.controls['SOL_DASHBOARD_TOKEN'])
                uvicorn.run(application, host=runtime.config.dashboard.host, port=runtime.config.dashboard.port,
                            access_log=False, proxy_headers=False)
                return 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        # No secret/config values in the failure output.
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__,
                          'reason': 'STARTUP_REFUSED_CHECK_LOCAL_CONFIG_ENV_AND_ISOLATION',
                          'help': 'Only dry_run=true/live.enabled=false; dashboard needs own .env SOL_DASHBOARD_TOKEN (24+ chars).'}, ensure_ascii=False))
        return 2
    print(json.dumps({'ok': True, **result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
