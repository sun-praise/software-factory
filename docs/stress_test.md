# Stress Test Documentation

## Overview

This stress test suite validates the GitHub webhook endpoint under various load scenarios using Locust.

## Prerequisites

Install dependencies:

```bash
pip install locust
```

## Running Tests

### Quick Start

Run all stress tests:

```bash
./scripts/run_stress_test.sh
```

### Individual Test Scenarios

1. **Review Comment Test** - Single PR rapid comments
```bash
locust -f scripts/locustfile.py --host http://localhost:8000 --only ReviewCommentUser --headless --users 5 --run-time 30s
```

2. **Multi-PR Test** - Multiple PR concurrent comments
```bash
locust -f scripts/locustfile.py --host http://localhost:8000 --only MultiPRUser --headless --users 10 --run-time 30s
```

3. **Burst Test** - High load handling
```bash
locust -f scripts/locustfile.py --host http://localhost:8000 --only BurstUser --headless --users 20 --run-time 30s
```

### Interactive Mode

Run with web UI for real-time monitoring:

```bash
locust -f scripts/locustfile.py --host http://localhost:8000
```

Then open http://localhost:8089 in your browser.

## Test Scenarios

### 1. ReviewCommentUser

**Purpose**: Test debounce logic on single PR

**Behavior**:
- Single PR with rapid successive review comments
- Wait time: 0.1-0.5 seconds between requests
- Simulates developer leaving multiple comments quickly

**What it validates**:
- Debounce window (default 60s) prevents duplicate processing
- Event deduplication works correctly
- Database insert performance for rapid events

**Expected metrics**:
- Response time < 100ms
- No duplicate task enqueues
- Successful debounce window application

### 2. MultiPRUser

**Purpose**: Test concurrent PR processing

**Behavior**:
- 50 different PRs in single repo
- Wait time: 0.5-1.5 seconds between requests
- Simulates multiple developers working on different PRs

**What it validates**:
- PR-level lock acquisition
- Concurrent PR processing
- No lock contention between different PRs

**Expected metrics**:
- Response time < 200ms
- Successful lock acquisition per PR
- No lock timeouts

### 3. BurstUser

**Purpose**: Test high load handling

**Behavior**:
- 20 different repos, 200+ PRs
- Wait time: 0.01-0.1 seconds between requests
- Simulates sudden spike in webhook events

**What it validates**:
- System handles burst traffic
- Database connection pooling
- Memory usage under load
- Event queue doesn't overflow

**Expected metrics**:
- Response time < 500ms
- No 5xx errors
- Graceful degradation under load

## Key Metrics

### Response Time

- **p50 (median)**: Should be < 50ms
- **p95**: Should be < 200ms
- **p99**: Should be < 500ms

### Throughput

- **Requests per second**: Should handle > 100 RPS
- **Failure rate**: Should be < 1%

### Resource Usage

Monitor during tests:
- CPU usage
- Memory consumption
- Database connections
- Open file descriptors

## Test Data

The test generates realistic GitHub webhook payloads:

- **Event types**: 
  - `pull_request_review_comment`
  - `pull_request`
  - `pull_request_review`

- **Repositories**: `test-org/repo-{1-20}`

- **PRs**: Random PR numbers 1-200

- **Users**: `test-user`, `reviewer`

## Configuration

### Webhook Secret

Tests use `test-webhook-secret` as the webhook secret.

Ensure your test environment has:

```bash
export GITHUB_WEBHOOK_SECRET="test-webhook-secret"
```

### Environment Variables

```bash
export GITHUB_WEBHOOK_DEBOUNCE_SECONDS=60
export MAX_RETRY_ATTEMPTS=3
```

## Interpreting Results

### Success Indicators

- All requests return 200 status
- No duplicate task enqueues (check `queue_status` in response)
- Debounce window correctly applied
- Lock acquisition succeeds for different PRs

### Warning Signs

- Increasing response times
- 5xx errors (service unavailable)
- Database lock errors
- Memory growth

### Common Issues

1. **High response times**
   - Check database indexes
   - Verify connection pooling
   - Review lock contention

2. **Duplicate tasks**
   - Verify debounce window configuration
   - Check idempotency key generation

3. **Lock failures**
   - Review lock TTL settings
   - Check for stale locks

## Continuous Integration

Add to CI pipeline:

```yaml
- name: Run stress tests
  run: |
    pip install locust
    ./scripts/run_stress_test.sh
  artifacts:
    paths:
      - results_*.html
```

## Monitoring During Tests

### Application Logs

```bash
tail -f logs/app.log | grep -E "webhook|debounce|lock"
```

### Database

```sql
-- Check running tasks
SELECT status, COUNT(*) FROM autofix_runs GROUP BY status;

-- Check PR locks
SELECT repo, pr_number, lock_owner, lock_expires_at 
FROM pull_requests 
WHERE lock_owner IS NOT NULL;

-- Check event count
SELECT repo, pr_number, COUNT(*) 
FROM review_events 
GROUP BY repo, pr_number 
ORDER BY COUNT(*) DESC 
LIMIT 10;
```

### System Resources

```bash
# CPU and memory
top -p $(pgrep -f uvicorn)

# Network connections
netstat -an | grep 8000 | wc -l

# File descriptors
lsof -p $(pgrep -f uvicorn) | wc -l
```

## Performance Tuning

Based on test results:

1. **Increase workers** if CPU is low but response times are high
2. **Add database indexes** if queries are slow
3. **Adjust connection pool** if running out of connections
4. **Tune debounce window** if seeing too many duplicate events

## Clean Up

After testing, clean up test data:

```sql
DELETE FROM review_events WHERE repo LIKE 'test-org/%';
DELETE FROM autofix_runs WHERE repo LIKE 'test-org/%';
DELETE FROM pull_requests WHERE repo LIKE 'test-org/%';
```
