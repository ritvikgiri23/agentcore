#!/usr/bin/env sh
# Plays the flagship scenario against a running stack with curl: sign in, create the
# Research Assistant session, submit the prompt, stream its steps, print the answer.
set -eu

API="${API_URL:-http://localhost:${API_PORT:-8000}}/api/v1"
EMAIL="${DEMO_EMAIL:-demo@example.com}"
PASSWORD="${DEMO_PASSWORD:-demo-password}"
PROMPT="What is 15% of the current population of Dubai, and remember that fact for me?"

# The API returns compact JSON, so a string field can be picked out without jq.
field() { sed -n "s/.*\"$1\":\"\([^\"]*\)\".*/\1/p"; }

# Responses are captured before parsing: a failed curl in an assignment stops the script,
# in a pipe it wouldn't. The error body goes to stderr.
call() {
  if ! out=$(curl -sS --fail-with-body "$@"); then
    echo "$out" >&2
    exit 1
  fi
  echo "$out"
}

echo "==> Signing in as $EMAIL"
body=$(curl -sS -w '\n%{http_code}' -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" "$API/auth/register")
if [ "$(echo "$body" | tail -n1)" = "409" ]; then
  # Already registered: log in instead.
  body=$(call \
    --data-urlencode "username=$EMAIL" --data-urlencode "password=$PASSWORD" "$API/auth/token")
fi
TOKEN=$(echo "$body" | field access_token)
[ -n "$TOKEN" ] || { echo "Sign-in failed: $body" >&2; exit 1; }
AUTH="Authorization: Bearer $TOKEN"

post_json() { call -H "$AUTH" -H 'Content-Type: application/json' -d "$2" "$API$1"; }

echo "==> Creating the Research Assistant session"
body=$(post_json /sessions \
  '{"name":"Research Assistant","system_prompt":"You are a careful research assistant.","tools_enabled":["web_search","calculator","remember_fact"]}')
SESSION=$(echo "$body" | field id)

echo "==> Submitting: $PROMPT"
body=$(post_json "/sessions/$SESSION/run" "{\"message\":\"$PROMPT\"}")
RUN=$(echo "$body" | field run_id)

echo "==> Streaming run $RUN"
curl -sS -N --fail-with-body "$API/runs/$RUN/stream?access_token=$TOKEN"

echo "==> Final status"
curl -sS --fail-with-body -H "$AUTH" "$API/runs/$RUN/status"
echo
