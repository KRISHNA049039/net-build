#!/bin/bash
# Enables password auth on top of the official cassandra image's stock
# docker-entrypoint.sh, which has env vars for DC/rack/seeds/etc. but none
# for authenticator/authorizer. Uses the same targeted-sed technique that
# script itself uses for its own substitutions, on just these two lines,
# then hands off to it unchanged -- nothing else about how the node boots
# is touched.
set -euo pipefail

CONF=/etc/cassandra/cassandra.yaml
sed -ri 's/^authenticator:.*/authenticator: PasswordAuthenticator/' "$CONF"
sed -ri 's/^authorizer:.*/authorizer: CassandraAuthorizer/' "$CONF"

exec docker-entrypoint.sh "$@"
