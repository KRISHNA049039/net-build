#!/usr/bin/env bash
# One-time bootstrap, run ONCE against a fresh cluster after auth is
# enabled and the ring is fully formed (nodetool status -> all UN).
# Creates a dedicated, non-superuser role per service -- catalog-service
# and ff-net-service should NEVER connect as the 'cassandra' superuser.
#
# Passwords are read from the environment, never written to a file or
# committed anywhere -- set them, run this, then unset them.
#
# Usage:
#   CATALOG_APP_PASSWORD='...' FF_NET_APP_PASSWORD='...' \
#     ./create-app-roles.sh <any-node-container-name> [superuser] [superuser-password]
set -euo pipefail

NODE="${1:?usage: create-app-roles.sh <container-name> [superuser] [superuser-password]}"
SUPERUSER="${2:-cassandra}"
SUPERUSER_PASSWORD="${3:-cassandra}"

: "${CATALOG_APP_PASSWORD:?set CATALOG_APP_PASSWORD}"
: "${FF_NET_APP_PASSWORD:?set FF_NET_APP_PASSWORD}"

cql() {
  docker exec -i "$NODE" cqlsh -u "$SUPERUSER" -p "$SUPERUSER_PASSWORD" <<CQL
$1
CQL
}

echo "Creating catalog_app role (owns django_platform only)..."
cql "CREATE ROLE IF NOT EXISTS catalog_app WITH PASSWORD = '${CATALOG_APP_PASSWORD}' AND LOGIN = true;"
cql "GRANT ALL PERMISSIONS ON KEYSPACE django_platform TO catalog_app;"

echo "Creating ff_net_app role (owns ans_transformed only)..."
cql "CREATE ROLE IF NOT EXISTS ff_net_app WITH PASSWORD = '${FF_NET_APP_PASSWORD}' AND LOGIN = true;"
cql "GRANT ALL PERMISSIONS ON KEYSPACE ans_transformed TO ff_net_app;"

echo "Done. Set CASSANDRA_USERNAME=catalog_app / CASSANDRA_USERNAME=ff_net_app"
echo "(with the matching password) in each service's .env -- never the superuser."
echo
echo "Reminder -- also do these two (not scripted, one-time, need your own judgement):"
echo "  1. Rotate the default superuser password:"
echo "     ALTER ROLE cassandra WITH PASSWORD = '<new password>';"
echo "  2. Fix system_auth's replication factor (default RF=1 -- losing that"
echo "     one node locks out ALL authentication cluster-wide):"
echo "     ALTER KEYSPACE system_auth WITH replication ="
echo "       {'class': 'NetworkTopologyStrategy', 'datacenter-1': 3};"
echo "     then run nodetool repair on system_auth on every node."
echo "  See ../AUTH.md."
