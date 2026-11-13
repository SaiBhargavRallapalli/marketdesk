#!/bin/sh
# MarketDesk container entrypoint:
#   1. background loop keeps prices/scores/alerts/reports fresh (daily)
#   2. Streamlit dashboard runs in the foreground
set -e

mkdir -p /app/data /app/reports

echo "[entrypoint] MarketDesk starting (TZ=${TZ:-UTC})"

# Give the UI a moment before the first data pull competes for CPU
(
    sleep 15
    while true; do
        echo "[entrypoint] $(date '+%F %T') running daily pipeline..."
        # first run populates the DB from scratch (~10-15 min for Nifty 500);
        # later runs are incremental (a few minutes)
        python -m marketdesk.jobs daily >> /app/data/pipeline.log 2>&1 \
            || echo "[entrypoint] pipeline failed (see data/pipeline.log), retrying in 1h"
        # retry sooner if it failed, otherwise sleep a full day
        sleep 21600
    done
) &

exec streamlit run app.py
