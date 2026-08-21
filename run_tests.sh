#!/usr/bin/env bash
# Every test in the repo. Exits non-zero if any suite fails.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-sk-ant-test}"
export SCREENER_DATA="${SCREENER_DATA:-/tmp/screener-test-data}"

SUITES=(
  notify/test_telegram.py
  core/test_screen.py
  core/test_sizing.py
  core/test_regime.py
  core/test_exits.py
  mcp/market_data/test_market_data.py
  mcp/news/test_news.py
  pipeline/test_llm.py
  pipeline/test_preflight.py
  pipeline/test_stage_a.py
  pipeline/test_stage_b.py
  pipeline/test_fill_listener.py
  pipeline/test_weekly_review.py
  backtest/test_backtest.py
  paper/test_paper.py
)

fail=0
for suite in "${SUITES[@]}"; do
  printf '%-44s' "$suite"
  if out=$(python3 "$suite" 2>&1); then
    echo "$(echo "$out" | grep -oE 'Ran [0-9]+ tests' | tail -1)  OK"
  else
    echo "FAILED"; echo "$out" | tail -25; fail=1
  fi
done

echo
[ $fail -eq 0 ] && echo "all suites passed" || echo "SOME SUITES FAILED"
exit $fail
