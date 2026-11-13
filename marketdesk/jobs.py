"""Command-line jobs: daily data update, scores, alerts, reports.

Usage:
    python -m marketdesk.jobs update [--universe nifty50|nifty500] [--limit N]
                                     [--force] [--skip-fundamentals]
    python -m marketdesk.jobs scores
    python -m marketdesk.jobs alerts [--test]
    python -m marketdesk.jobs report [--stock SYMBOL]
    python -m marketdesk.jobs daily   # update + scores + alerts + report
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def cmd_update(args) -> None:
    from . import fundamentals as F, market as M, universe as U, db

    db.init_db()
    kind = args.universe
    _log(f"Loading {kind} constituents...")
    univ = U.load(kind)
    symbols = univ["symbol_ns"].tolist()
    if args.limit:
        symbols = symbols[: args.limit]
    _log(f"{len(symbols)} symbols")

    n = M.update_prices(symbols, force_full=args.force, progress=_log)
    _log(f"Saved {n} price rows")

    if not args.skip_fundamentals:
        done = F.refresh(symbols, force=args.force, progress=_log)
        _log(f"Fundamentals refreshed for {done} symbols")


def cmd_scores(args) -> None:
    from . import scores as S, db

    db.init_db()
    _log("Computing DVS scores...")
    df = S.compute_scores()
    _log(f"Scored {len(df)} symbols. Top 10:")
    top = df.head(10)[["rank", "symbol", "dvs"]]
    print(top.to_string(index=False))


def cmd_alerts(args) -> None:
    from . import alerts as A, db

    db.init_db()
    if getattr(args, "test", False):
        res = A.test_channels()
        _log(f"Test send -> {res}")
        if not any(res.values()):
            print("No channels configured. Fill TELEGRAM_* / SMTP_* in .env "
                  "(see .env.example).")
            sys.exit(1)
        return
    fired = A.evaluate()
    if not fired:
        _log("No alerts fired.")
    for f in fired:
        _log(f"FIRED {f['rule']} {f['symbol']}: {f['message']}")


def cmd_report(args) -> None:
    from . import reports as R, db

    db.init_db()
    stock = getattr(args, "stock", None)
    if stock:
        path = R.save_stock_report(stock)
    else:
        path = R.save_daily_report()
    _log(f"Report written: {path}")


def cmd_daily(args) -> None:
    cmd_update(args)
    cmd_scores(args)
    cmd_alerts(args)
    cmd_report(args)
    _log(f"Daily pipeline complete for {date.today()}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="marketdesk", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pu = sub.add_parser("update", help="refresh prices + fundamentals")
    pu.add_argument("--universe", choices=["nifty50", "nifty500"],
                    default=None, help="override config.yaml")
    pu.add_argument("--limit", type=int, default=0, help="only first N symbols")
    pu.add_argument("--force", action="store_true", help="ignore caches")
    pu.add_argument("--skip-fundamentals", action="store_true")
    pu.set_defaults(func=cmd_update)

    ps = sub.add_parser("scores", help="recompute DVS scores")
    ps.set_defaults(func=cmd_scores)

    pa = sub.add_parser("alerts", help="evaluate alert rules")
    pa.add_argument("--test", action="store_true", help="send a test message")
    pa.set_defaults(func=cmd_alerts)

    pr = sub.add_parser("report", help="generate HTML report")
    pr.add_argument("--stock", default=None, help="single-stock one-pager")
    pr.set_defaults(func=cmd_report)

    pd_ = sub.add_parser("daily", help="full pipeline")
    pd_.add_argument("--universe", choices=["nifty50", "nifty500"], default=None)
    pd_.add_argument("--limit", type=int, default=0)
    pd_.add_argument("--force", action="store_true")
    pd_.add_argument("--skip-fundamentals", action="store_true")
    pd_.set_defaults(func=cmd_daily)

    args = p.parse_args(argv)
    if getattr(args, "universe", None) is None and hasattr(args, "universe"):
        from .config import CFG
        args.universe = CFG["universe"]
    args.func(args)


if __name__ == "__main__":
    main()
