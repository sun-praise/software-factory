#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "Starting stress test for GitHub webhook endpoint..."
echo "Target: http://localhost:8000"
echo ""

echo "1. Single PR rapid comments (ReviewCommentUser)"
echo "   - Tests debounce on single PR"
echo "   - Rapid successive comments"
locust -f scripts/locustfile.py \
    --host http://localhost:8000 \
    --users 5 \
    --spawn-rate 1 \
    --run-time 30s \
    --headless \
    --only ReviewCommentUser \
    --html results_review_comment.html

echo ""
echo "2. Multiple PR concurrent comments (MultiPRUser)"
echo "   - Tests concurrent PR processing"
echo "   - Multiple PRs in parallel"
locust -f scripts/locustfile.py \
    --host http://localhost:8000 \
    --users 10 \
    --spawn-rate 2 \
    --run-time 30s \
    --headless \
    --only MultiPRUser \
    --html results_multi_pr.html

echo ""
echo "3. Burst traffic (BurstUser)"
echo "   - Tests high load handling"
echo "   - Random repos and PRs"
locust -f scripts/locustfile.py \
    --host http://localhost:8000 \
    --users 20 \
    --spawn-rate 5 \
    --run-time 30s \
    --headless \
    --only BurstUser \
    --html results_burst.html

echo ""
echo "4. Combined load test"
echo "   - All user types together"
locust -f scripts/locustfile.py \
    --host http://localhost:8000 \
    --users 30 \
    --spawn-rate 3 \
    --run-time 60s \
    --headless \
    --html results_combined.html

echo ""
echo "Stress test completed!"
echo "Results saved to:"
echo "  - results_review_comment.html"
echo "  - results_multi_pr.html"
echo "  - results_burst.html"
echo "  - results_combined.html"
