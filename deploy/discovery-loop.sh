#!/bin/bash
# Discovery loop for the coordinator. Runs one full discovery pass, then re-attempts
# any store that came back blocked/empty every 30 minutes. eBay challenges the
# listing-pagination surface, so a store that is blocked now usually succeeds on a
# later pass once the rotating residential pool's exit IPs have cooled. The rq worker
# fetches discovered items continuously, so this loop only feeds the queue.
#
# Run under systemd (deploy/ebay-discovery.service) so it restarts on crash/reboot.
set -u

REPO_DIR="${REPO_DIR:-/root/ebay-scraper}"
SCRAPER="${SCRAPER:-/usr/local/bin/scraper}"
CAP="${CAP_PER_STORE:-5000}"
RETRY_INTERVAL="${RETRY_INTERVAL:-1800}"

cd "$REPO_DIR" || exit 1

echo "[discovery-loop] initial pass $(date -u)"
"$SCRAPER" scrape start --us-only --cap-per-store "$CAP"

while true; do
  sleep "$RETRY_INTERVAL"
  echo "[discovery-loop] retry pass $(date -u)"
  "$SCRAPER" scrape retry --us-only --cap-per-store "$CAP"
done
