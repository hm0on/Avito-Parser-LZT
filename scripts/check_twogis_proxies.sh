#!/usr/bin/env bash
# check_twogis_proxies.sh — Quick operational check for 2GIS proxy pool.
#
# Usage:
#   ./scripts/check_twogis_proxies.sh [proxy_file] [sample_size]
#
# Defaults:
#   proxy_file  = storage/proxies/twogis.txt
#   sample_size = 10
#
# Output: summary counts (ok, 407, timeout, error) + CSV report.

set -euo pipefail

PROXY_FILE="${1:-storage/proxies/twogis.txt}"
SAMPLE_SIZE="${2:-10}"
TEST_URL="https://2gis.ru/omsk"
TIMEOUT=12
REPORT_DIR="storage/reports"
REPORT_FILE="${REPORT_DIR}/twogis_proxy_check_$(date +%Y%m%d_%H%M%S).csv"

if [ ! -f "$PROXY_FILE" ]; then
    echo "ERROR: Proxy file not found: $PROXY_FILE"
    exit 1
fi

# Read proxies, skip comments and empty lines
mapfile -t ALL_PROXIES < <(grep -v '^\s*#' "$PROXY_FILE" | grep -v '^\s*$' | sed 's/^[[:space:]]*//')

TOTAL=${#ALL_PROXIES[@]}
if [ "$TOTAL" -eq 0 ]; then
    echo "ERROR: No proxies found in $PROXY_FILE"
    exit 1
fi

# Sample
if [ "$SAMPLE_SIZE" -gt "$TOTAL" ]; then
    SAMPLE_SIZE=$TOTAL
fi

# Shuffle and take sample
SAMPLE=($(printf '%s\n' "${ALL_PROXIES[@]}" | shuf -n "$SAMPLE_SIZE"))

echo "=== 2GIS Proxy Check ==="
echo "File:   $PROXY_FILE"
echo "Total:  $TOTAL proxies"
echo "Sample: $SAMPLE_SIZE"
echo "URL:    $TEST_URL"
echo ""

mkdir -p "$REPORT_DIR"
echo "proxy,status,http_code,latency_ms" > "$REPORT_FILE"

OK=0
AUTH_FAIL=0
TIMEOUT_COUNT=0
ERROR_COUNT=0

for proxy in "${SAMPLE[@]}"; do
    # Ensure scheme
    if [[ ! "$proxy" =~ :// ]]; then
        proxy="http://$proxy"
    fi

    START_MS=$(($(date +%s%N) / 1000000))

    HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
        --proxy "$proxy" \
        --connect-timeout "$TIMEOUT" \
        --max-time "$TIMEOUT" \
        "$TEST_URL" 2>/dev/null || echo "000")

    END_MS=$(($(date +%s%N) / 1000000))
    LATENCY=$((END_MS - START_MS))

    if [ "$HTTP_CODE" = "407" ] || [ "$HTTP_CODE" = "401" ]; then
        STATUS="auth_fail"
        AUTH_FAIL=$((AUTH_FAIL + 1))
    elif [ "$HTTP_CODE" = "000" ]; then
        STATUS="timeout"
        TIMEOUT_COUNT=$((TIMEOUT_COUNT + 1))
    elif [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "301" ] || [ "$HTTP_CODE" = "302" ]; then
        STATUS="ok"
        OK=$((OK + 1))
    else
        STATUS="error_${HTTP_CODE}"
        ERROR_COUNT=$((ERROR_COUNT + 1))
    fi

    # Mask credentials in report
    MASKED=$(echo "$proxy" | sed -E 's|://([^:]+):([^@]+)@|://***:***@|')
    echo "$MASKED,$STATUS,$HTTP_CODE,${LATENCY}ms" >> "$REPORT_FILE"
    printf "  %-12s  HTTP %s  %4sms  %s\n" "$STATUS" "$HTTP_CODE" "$LATENCY" "$MASKED"
done

echo ""
echo "=== Summary ==="
echo "  OK:        $OK"
echo "  Auth fail: $AUTH_FAIL"
echo "  Timeout:   $TIMEOUT_COUNT"
echo "  Other err: $ERROR_COUNT"
echo ""
echo "Report: $REPORT_FILE"

if [ "$OK" -eq 0 ]; then
    echo ""
    echo "WARNING: No working proxies! Pipeline will fail."
    echo "Action: Update $PROXY_FILE with valid proxy credentials."
    exit 2
fi
