#!/bin/bash
#
# Job Dispatch API Testing Script
#
# Tests the complete job dispatch flow:
# 1. Create an agent
# 2. Create a job for that agent
# 3. Check job status
# 4. Simulate agent heartbeat (to pick up job)
# 5. Submit job results
# 6. Verify job completion
#

set -e  # Exit on error

# Configuration
COLLECTOR_URL="https://localhost:9000"
AUTH_TOKEN="yw4MDZVTMHksbGCA"
AGENT_ID="test-agent-$(date +%s)"
CURL_OPTS="-k"  # Allow insecure HTTPS for testing

echo "========================================="
echo "LuxSwirl Job Dispatch API Test"
echo "========================================="
echo "Collector: $COLLECTOR_URL"
echo "Agent ID: $AGENT_ID"
echo ""

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Helper function for API calls
api_call() {
    local method=$1
    local endpoint=$2
    local data=$3

    echo -e "${BLUE}[API] $method $endpoint${NC}"

    if [ -n "$data" ]; then
        curl $CURL_OPTS -s -X "$method" \
            -H "Authorization: Bearer $AUTH_TOKEN" \
            -H "Content-Type: application/json" \
            -d "$data" \
            "$COLLECTOR_URL$endpoint"
    else
        curl $CURL_OPTS -s -X "$method" \
            -H "Authorization: Bearer $AUTH_TOKEN" \
            "$COLLECTOR_URL$endpoint"
    fi

    echo ""  # Newline after response
}

# Test 1: Health Check
echo -e "${YELLOW}Test 1: Health Check${NC}"
response=$(curl $CURL_OPTS -s "$COLLECTOR_URL/health")
echo "$response" | jq .
echo ""

# Test 2: Create Agent
echo -e "${YELLOW}Test 2: Create Agent${NC}"
agent_data='{
  "agent_id": "'$AGENT_ID'",
  "hostname": "test-host",
  "ip_address": "10.0.0.100",
  "version": "1.0.0",
  "tags": "test,api"
}'

agent_response=$(api_call POST "/api/v1/agents" "$agent_data")
echo "$agent_response" | jq .
echo -e "${GREEN}✓ Agent created${NC}"
echo ""

# Test 3: List Agents
echo -e "${YELLOW}Test 3: List Agents${NC}"
agents=$(api_call GET "/api/v1/agents")
echo "$agents" | jq '.agents[] | {agent_id, hostname, is_online}'
echo ""

# Test 4: Create a Network Scan Job
echo -e "${YELLOW}Test 4: Create Network Scan Job${NC}"
job_data='{
  "job_type": "network_scan",
  "agent_id": "'$AGENT_ID'",
  "params": {
    "subnet": "192.168.1.0/24",
    "timeout": 1,
    "ports": [80, 443, 22],
    "max_concurrent": 50
  },
  "priority": 10,
  "tags": ["test", "discovery"]
}'

job_response=$(api_call POST "/api/v1/jobs" "$job_data")
JOB_ID=$(echo "$job_response" | jq -r '.id')
echo "$job_response" | jq .
echo -e "${GREEN}✓ Job created: $JOB_ID${NC}"
echo ""

# Test 5: Get Job Details
echo -e "${YELLOW}Test 5: Get Job Details${NC}"
job_details=$(api_call GET "/api/v1/jobs/$JOB_ID")
echo "$job_details" | jq '{id, job_type, agent_id, status, priority, created_at}'
echo ""

# Test 6: List All Jobs
echo -e "${YELLOW}Test 6: List All Jobs${NC}"
all_jobs=$(api_call GET "/api/v1/jobs")
echo "$all_jobs" | jq '{total, pending_count, running_count, completed_count, jobs: .jobs[] | {id, job_type, status, agent_id}}'
echo ""

# Test 7: Simulate Agent Heartbeat (Job Pickup)
echo -e "${YELLOW}Test 7: Simulate Agent Heartbeat (Job Pickup)${NC}"
heartbeat_data='{
  "timestamp": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'",
  "hostname": "test-host",
  "ip_address": "10.0.0.100",
  "version": "1.0.0",
  "uptime_seconds": 100,
  "status": "online",
  "tags": ["test"],
  "checks_total": 0,
  "checks_active": 0,
  "checks_executed_count": 0,
  "checks_succeeded_count": 0,
  "checks_failed_count": 0,
  "cpu_percent": 5.2,
  "memory_mb": 256,
  "queue_depth": 0,
  "jobs_pending": 0,
  "jobs_running": 0,
  "jobs_completed_since_last": 0,
  "jobs_failed_since_last": 0
}'

heartbeat_response=$(api_call POST "/api/v1/agents/$AGENT_ID/heartbeat" "$heartbeat_data")
echo "$heartbeat_response" | jq .
JOBS_COUNT=$(echo "$heartbeat_response" | jq '.jobs | length')
echo -e "${GREEN}✓ Heartbeat sent, received $JOBS_COUNT jobs${NC}"

if [ "$JOBS_COUNT" -gt 0 ]; then
    echo "Jobs to execute:"
    echo "$heartbeat_response" | jq '.jobs[] | {job_id, job_type, priority}'
fi
echo ""

# Test 8: Check Job Status (should be 'assigned' now)
echo -e "${YELLOW}Test 8: Check Job Status After Heartbeat${NC}"
job_status=$(api_call GET "/api/v1/jobs/$JOB_ID")
STATUS=$(echo "$job_status" | jq -r '.status')
echo "$job_status" | jq '{id, status, assigned_at}'
echo -e "${GREEN}✓ Job status: $STATUS${NC}"
echo ""

# Test 9: Submit Job Results
echo -e "${YELLOW}Test 9: Submit Job Results${NC}"
result_data='{
  "status": "completed",
  "result": {
    "discovered_hosts": [
      {
        "ip": "192.168.1.10",
        "hostname": "printer.local",
        "responds_to_ping": true,
        "open_ports": [80, 443]
      },
      {
        "ip": "192.168.1.20",
        "hostname": "nas.local",
        "responds_to_ping": true,
        "open_ports": [22, 80, 443]
      }
    ],
    "scan_duration_seconds": 12.5,
    "hosts_scanned": 254,
    "hosts_responding": 2,
    "subnet": "192.168.1.0/24"
  },
  "error": null
}'

result_response=$(api_call POST "/api/v1/jobs/$JOB_ID/results" "$result_data")
echo "$result_response" | jq '{id, status, completed_at, duration_seconds}'
echo -e "${GREEN}✓ Job results submitted${NC}"
echo ""

# Test 10: Get Final Job Details with Results
echo -e "${YELLOW}Test 10: Get Final Job Details with Results${NC}"
final_job=$(api_call GET "/api/v1/jobs/$JOB_ID")
echo "$final_job" | jq '{id, job_type, status, duration_seconds, result}'
echo ""

# Test 11: Get Job Statistics
echo -e "${YELLOW}Test 11: Get Job Statistics${NC}"
job_stats=$(api_call GET "/api/v1/jobs/stats/summary")
echo "$job_stats" | jq .
echo ""

# Test 12: Create High Priority Job
echo -e "${YELLOW}Test 12: Create High Priority Job${NC}"
priority_job='{
  "job_type": "network_scan",
  "agent_id": "'$AGENT_ID'",
  "params": {
    "subnet": "10.0.0.0/24",
    "timeout": 2
  },
  "priority": 50,
  "tags": ["urgent"]
}'

priority_response=$(api_call POST "/api/v1/jobs" "$priority_job")
PRIORITY_JOB_ID=$(echo "$priority_response" | jq -r '.id')
echo "$priority_response" | jq '{id, priority, status}'
echo -e "${GREEN}✓ High priority job created: $PRIORITY_JOB_ID${NC}"
echo ""

# Test 13: Cancel a Job
echo -e "${YELLOW}Test 13: Cancel the High Priority Job${NC}"
cancel_response=$(api_call DELETE "/api/v1/jobs/$PRIORITY_JOB_ID")
echo -e "${GREEN}✓ Job cancelled${NC}"
echo ""

# Test 14: Verify Job was Cancelled
echo -e "${YELLOW}Test 14: Verify Job Cancellation${NC}"
cancelled_job=$(api_call GET "/api/v1/jobs/$PRIORITY_JOB_ID")
CANCELLED_STATUS=$(echo "$cancelled_job" | jq -r '.status')
echo "$cancelled_job" | jq '{id, status, completed_at}'
echo -e "${GREEN}✓ Job status: $CANCELLED_STATUS${NC}"
echo ""

# Test 15: Filter Jobs by Agent
echo -e "${YELLOW}Test 15: Filter Jobs by Agent${NC}"
agent_jobs=$(api_call GET "/api/v1/jobs?agent_id=$AGENT_ID")
echo "$agent_jobs" | jq '{total, jobs: .jobs[] | {id, job_type, status}}'
echo ""

# Test 16: Filter Jobs by Status
echo -e "${YELLOW}Test 16: Filter Jobs by Status (completed)${NC}"
completed_jobs=$(api_call GET "/api/v1/jobs?status=completed")
echo "$completed_jobs" | jq '{total, completed_count, jobs: .jobs[0:3] | .[] | {id, status, duration_seconds}}'
echo ""

# Summary
echo ""
echo "========================================="
echo -e "${GREEN}All Tests Completed Successfully!${NC}"
echo "========================================="
echo ""
echo "Summary:"
echo "- Agent ID: $AGENT_ID"
echo "- Job ID (completed): $JOB_ID"
echo "- Job ID (cancelled): $PRIORITY_JOB_ID"
echo ""
echo "You can now:"
echo "1. View jobs: curl -H 'Authorization: Bearer $AUTH_TOKEN' $COLLECTOR_URL/api/v1/jobs"
echo "2. View specific job: curl -H 'Authorization: Bearer $AUTH_TOKEN' $COLLECTOR_URL/api/v1/jobs/$JOB_ID"
echo "3. View stats: curl -H 'Authorization: Bearer $AUTH_TOKEN' $COLLECTOR_URL/api/v1/jobs/stats/summary"
echo ""
