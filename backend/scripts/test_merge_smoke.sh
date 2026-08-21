#!/usr/bin/env bash
# Smoke test for the net-merging alert/approve/reject workflow, against a
# REAL running server + Cassandra cluster (no mocking). Complements
# apps/ff_net/tests/test_net_merging.py's pytest suite -- use this one for a
# quick manual sanity check, or in an environment without the Python
# dev-dependencies (pytest) installed.
#
# Usage:
#   ./scripts/test_merge_smoke.sh
#
# Env overrides:
#   BASE_URL              default http://127.0.0.1:8000
#   CASSANDRA_CONTAINER    default cassandra-database
#
# Uses net_id 999201-999206 (reserved test range, distinct from the pytest
# suite's 999100s range) so it never collides with real data. Cleans up
# after itself even on failure (trap).

set -u

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
CASSANDRA_CONTAINER="${CASSANDRA_CONTAINER:-cassandra-database}"
KEYSPACE="ans_transformed"

PASS=0
FAIL=0

cql() {
    docker exec -i "$CASSANDRA_CONTAINER" cqlsh -e "USE $KEYSPACE; $1" 2>&1
}

cleanup() {
    echo
    echo "== cleanup =="
    cql "DELETE FROM ff_net_building WHERE net_id = 999201;" >/dev/null
    cql "DELETE FROM ff_net_building WHERE net_id = 999202;" >/dev/null
    cql "DELETE FROM ff_net_building WHERE net_id = 999203;" >/dev/null
    cql "DELETE FROM ff_net_building WHERE net_id = 999204;" >/dev/null
    cql "DELETE FROM ff_net_building WHERE net_id = 999205;" >/dev/null
    cql "DELETE FROM ff_net_building WHERE net_id = 999206;" >/dev/null
    cql "DELETE FROM ff_net_merge_candidates WHERE net_a = 999201 AND net_b = 999202;" >/dev/null
    cql "DELETE FROM ff_net_merge_candidates WHERE net_a = 999204 AND net_b = 999205;" >/dev/null
    echo "done."
}
trap cleanup EXIT

check() {
    # check "description" "actual" "expected substring"
    local desc="$1" actual="$2" expected="$3"
    if echo "$actual" | grep -qF "$expected"; then
        echo "  PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $desc"
        echo "        expected to find: $expected"
        echo "        actual: $actual"
        FAIL=$((FAIL + 1))
    fi
}

check_status() {
    # check_status "description" "actual_code" "expected_code"
    local desc="$1" actual="$2" expected="$3"
    if [ "$actual" = "$expected" ]; then
        echo "  PASS: $desc (status $actual)"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $desc (expected $expected, got $actual)"
        FAIL=$((FAIL + 1))
    fi
}

echo "== 0. server + cassandra reachable =="
health=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health/")
check_status "GET /health/" "$health" "200"

echo
echo "== 1. seed two matching nets (999201, 999202) + one non-matching (999203) =="
cql "INSERT INTO ff_net_building (net_id, my_net_id, frequency, bandwidth, lti, latitude, longitude, rdfs, nti) VALUES (999201, 201, 1000.0, 10.0, '2026-01-01', 10.0, 20.0, 'RDFS1', 1);" >/dev/null
cql "INSERT INTO ff_net_building (net_id, my_net_id, frequency, bandwidth, lti, latitude, longitude, rdfs, nti) VALUES (999202, 202, 1000.0, 10.0, '2026-01-01', 10.0, 20.0, 'RDFS2', 1);" >/dev/null
cql "INSERT INTO ff_net_building (net_id, my_net_id, frequency, bandwidth, lti, latitude, longitude, rdfs, nti) VALUES (999203, 203, 9000.0, 10.0, '2026-01-01', 10.0, 20.0, 'RDFS3', 1);" >/dev/null
echo "seeded."

echo
echo "== 2. GET /merge/candidates/ detects the matching pair, not the non-matching one =="
candidates=$(curl -s "$BASE_URL/api/ff_net/merge/candidates/")
check "detects (999201, 999202)" "$candidates" '"net_a":999201,"net_b":999202'
if echo "$candidates" | grep -qF '"net_a":999201,"net_b":999203'; then
    echo "  FAIL: 999203 (out of band) should NOT be a candidate"
    FAIL=$((FAIL + 1))
else
    echo "  PASS: 999203 (out of band) correctly not proposed"
    PASS=$((PASS + 1))
fi

echo
echo "== 3. re-detect is idempotent (newly_detected excludes the already-seen pair) =="
second=$(curl -s "$BASE_URL/api/ff_net/merge/candidates/")
check "pair still pending on re-poll" "$second" '"net_a":999201,"net_b":999202'

echo
echo "== 4. approve (999201, 999202) =="
approve_resp_file="$(dirname "$0")/.approve_resp.json"
approve_code=$(curl -s -o "$approve_resp_file" -w "%{http_code}" -X POST \
    "$BASE_URL/api/ff_net/merge/candidates/999201__999202/approve/")
check_status "POST approve" "$approve_code" "200"
approve_body=$(cat "$approve_resp_file" 2>/dev/null || true)
rm -f "$approve_resp_file"
check "survivor is the lower my_net_id (201)" "$approve_body" '"survivor_my_net_id":201'
check "absorbed is the higher my_net_id (202)" "$approve_body" '"absorbed_my_net_id":202'

echo
echo "== 5. verify ff_net_building.my_net_id actually merged =="
row202=$(cql "SELECT my_net_id FROM ff_net_building WHERE net_id = 999202;")
check "999202 now shares my_net_id 201" "$row202" "201"

echo
echo "== 6. approving the same pair again -> 400 =="
redo_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "$BASE_URL/api/ff_net/merge/candidates/999201__999202/approve/")
check_status "POST approve (already decided)" "$redo_code" "400"

echo
echo "== 7. approving a pair that was never a candidate -> 404 =="
missing_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "$BASE_URL/api/ff_net/merge/candidates/1__2/approve/")
check_status "POST approve (never proposed)" "$missing_code" "404"

echo
echo "== 8. reject path: seed another matching pair (999204, 999205), reject it =="
cql "INSERT INTO ff_net_building (net_id, my_net_id, frequency, bandwidth, lti, latitude, longitude, rdfs, nti) VALUES (999204, 204, 2000.0, 10.0, '2026-01-01', 10.0, 20.0, 'RDFS1', 1);" >/dev/null
cql "INSERT INTO ff_net_building (net_id, my_net_id, frequency, bandwidth, lti, latitude, longitude, rdfs, nti) VALUES (999205, 205, 2000.0, 10.0, '2026-01-01', 10.0, 20.0, 'RDFS2', 1);" >/dev/null
curl -s "$BASE_URL/api/ff_net/merge/candidates/" >/dev/null   # detect

reject_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "$BASE_URL/api/ff_net/merge/candidates/999204__999205/reject/")
check_status "POST reject" "$reject_code" "200"

row205=$(cql "SELECT my_net_id FROM ff_net_building WHERE net_id = 999205;")
check "999205's my_net_id untouched by reject" "$row205" "205"

after_reject=$(curl -s "$BASE_URL/api/ff_net/merge/candidates/")
if echo "$after_reject" | grep -qF '"net_a":999204,"net_b":999205'; then
    echo "  FAIL: rejected pair was re-proposed as pending"
    FAIL=$((FAIL + 1))
else
    echo "  PASS: rejected pair never re-proposed"
    PASS=$((PASS + 1))
fi

echo
echo "======================================"
echo "  $PASS passed, $FAIL failed"
echo "======================================"
[ "$FAIL" -eq 0 ]
