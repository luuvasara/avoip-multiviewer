#!/bin/sh
# Login / reverse-proxy checks against a throwaway gateway on the dev host.
# Usage: tools/access-test.sh <docker-context> <host-ip> <ssh-target>
# Runs image kvm-multiviewer:test on port 8821 with a dead video server
# (--mtx rtsp://127.0.0.1:9) so no real feed path is touched; WHEP is relayed
# to the host's real mediamtx (read-only signalling). Prints PASS/FAIL lines.
CTX=$1 HOST=$2 SSH=$3
P=8821 N=kvm-accesstest
start() {  # env args...
  docker --context "$CTX" rm -f $N >/dev/null 2>&1
  docker --context "$CTX" run -d --name $N --network host --entrypoint python3 "$@" \
    kvm-multiviewer:test /app/webclient/gateway.py --listen 0.0.0.0:$P \
    --iface 192.168.167.3 --bind 192.168.167.3 --mtx rtsp://127.0.0.1:9 \
    --webrtc-url http://127.0.0.1:8889 --idle 0 >/dev/null
  i=0; until curl -s -o /dev/null "http://$HOST:$P${BASE:-}/healthz" || [ $i -gt 40 ]; do sleep 0.5; i=$((i+1)); done
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
q() { for a; do printf "'%s' " "$(printf %s "$a" | sed "s/'/'\\\\''/g")"; done; }
rcurl() { ssh "$SSH" "curl -s -m 2 $(q "$@")"; }                       # from the host = trusted proxy
lcode() { rcurl -o /dev/null -w '%{http_code}' "$@"; }
check() { [ "$2" = "$3" ] && echo "PASS $1 ($2)" || echo "FAIL $1 (got $2, want $3)"; }

echo "== basic, no KVM_PASS: generated password"
start
PW=$(docker --context "$CTX" logs $N 2>&1 | sed -n "s/^login: user 'kvm', generated password \([^ ]*\) .*/\1/p")
check "password generated and logged" "$([ -n "$PW" ] && echo yes)" yes
check "page without login" "$(code http://$HOST:$P/)" 401
check "healthz without login" "$(code http://$HOST:$P/healthz)" 200
check "page with login" "$(code -u "kvm:$PW" http://$HOST:$P/)" 200
check "wrong password" "$(code -u kvm:nope http://$HOST:$P/)" 401
check "non-ASCII password" "$(code -u 'kvm:pässi' http://$HOST:$P/)" 401
check "WHEP relayed (mediamtx says no stream: 404)" \
  "$(code -u "kvm:$PW" -X POST -H 'Content-Type: application/sdp' --data 'v=0' http://$HOST:$P/whep/9)" 404
check "WHEP id outside 1-16" "$(code -u "kvm:$PW" -X POST http://$HOST:$P/whep/99)" 404
check "WebSocket from another origin" "$(code -u "kvm:$PW" -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  -H 'Origin: http://evil.example' http://$HOST:$P/input)" 403
check "WebSocket from the page's origin" "$(curl -s -o /dev/null -w '%{http_code}' -m 2 -u "kvm:$PW" \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' -H "Origin: http://$HOST:$P" http://$HOST:$P/input)" 101
docker --context "$CTX" restart $N >/dev/null; sleep 3
check "same password after restart (kept in STATE_DIR)" "$(code -u "kvm:$PW" http://$HOST:$P/)" 200

echo "== bearer token"
start -e KVM_TOKENS=t0ken1,t0ken2
check "token 2" "$(code -H 'Authorization: Bearer t0ken2' http://$HOST:$P/feeds)" 200
check "bad token" "$(code -H 'Authorization: Bearer x' http://$HOST:$P/feeds)" 401

echo "== proxy mode"
start -e KVM_AUTH=proxy -e AUTH_ALLOWED_USERS=alice@example.com
check "direct (not via proxy), with header" "$(code -H 'Remote-User: alice@example.com' http://$HOST:$P/)" 403
check "via proxy, no header" "$(lcode http://127.0.0.1:$P/)" 403
check "via proxy, allowed user" "$(lcode -H 'Remote-User: alice@example.com' http://127.0.0.1:$P/)" 200
check "via proxy, oauth2-proxy header" "$(lcode -H 'X-Forwarded-Email: ALICE@example.com' http://127.0.0.1:$P/)" 200
check "via proxy, other user" "$(lcode -H 'Remote-User: mallory' http://127.0.0.1:$P/)" 403
check "WebSocket, forwarded host matches origin" "$(lcode \
  -H 'Remote-User: alice@example.com' -H 'X-Forwarded-Host: kvm.example.com' \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' -H 'Origin: https://kvm.example.com' http://127.0.0.1:$P/input)" 101
check "WebSocket, X-Forwarded-Host from untrusted peer ignored" "$(code -H 'X-Forwarded-Host: kvm.example.com' \
  -H 'Remote-User: alice@example.com' -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' -H 'Origin: https://kvm.example.com' http://$HOST:$P/input)" 403
docker --context "$CTX" logs $N 2>&1 | grep -q "access refused" && echo "PASS refusals are logged" || echo "FAIL refusals not logged"

echo "== proxy mode with signed JWT (local JWKS)"
docker --context "$CTX" rm -f kvm-jwks >/dev/null 2>&1
docker --context "$CTX" run -d --name kvm-jwks --network host --entrypoint sh kvm-multiviewer:test -c '
mkdir -p /j && cd /j && python3 -I - <<PY
import json, jwt, time
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
def mk(kid):
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = json.loads(RSAAlgorithm.to_jwk(k.public_key())); pub.update(kid=kid, alg="RS256", use="sig")
    return k, pub
good, gpub = mk("good"); bad, _ = mk("bad")
json.dump({"keys": [gpub]}, open("jwks.json", "w"))
now = int(time.time())
c = {"aud": "kvm-app", "iss": "https://idp.example", "email": "alice@example.com", "exp": now + 600}
open("ok", "w").write(jwt.encode(c, good, "RS256", headers={"kid": "good"}))
open("aud", "w").write(jwt.encode(dict(c, aud="other"), good, "RS256", headers={"kid": "good"}))
open("exp", "w").write(jwt.encode(dict(c, exp=now - 60), good, "RS256", headers={"kid": "good"}))
open("forged", "w").write(jwt.encode(c, bad, "RS256", headers={"kid": "good"}))
PY
exec python3 -m http.server 8830 --bind 127.0.0.1' >/dev/null
sleep 3
tok() { docker --context "$CTX" exec kvm-jwks cat /j/$1; }
start -e KVM_AUTH=proxy -e AUTH_JWKS_URL=http://127.0.0.1:8830/jwks.json -e AUTH_JWT_AUDIENCE=kvm-app \
  -e AUTH_JWT_ISSUER=https://idp.example
check "valid JWT, direct (signature, not address, is trusted)" "$(code -H "Cf-Access-Jwt-Assertion: $(tok ok)" http://$HOST:$P/)" 200
check "JWT for another audience" "$(code -H "Cf-Access-Jwt-Assertion: $(tok aud)" http://$HOST:$P/)" 403
check "expired JWT" "$(code -H "Cf-Access-Jwt-Assertion: $(tok exp)" http://$HOST:$P/)" 403
check "JWT signed by another key" "$(code -H "Cf-Access-Jwt-Assertion: $(tok forged)" http://$HOST:$P/)" 403
check "no JWT" "$(code -H 'Remote-User: alice@example.com' http://$HOST:$P/)" 403
docker --context "$CTX" rm -f kvm-jwks >/dev/null

echo "== none + base path"
BASE=/kvm start -e KVM_AUTH=none -e BASE_PATH=/kvm
check "page under prefix" "$(code http://$HOST:$P/kvm/)" 200
check "prefix without slash redirects" "$(code http://$HOST:$P/kvm)" 302
check "static under prefix" "$(code http://$HOST:$P/kvm/static/app.css)" 200
check "root outside prefix" "$(code http://$HOST:$P/)" 404
check "redirect keeps X-Forwarded-Prefix" "$(rcurl -o /dev/null -w '%{redirect_url}' \
  -H 'X-Forwarded-Prefix: /outer' http://127.0.0.1:$P/kvm)" "http://127.0.0.1:$P/outer/kvm/"
docker --context "$CTX" logs $N 2>&1 | grep -q "KVM_AUTH=none" && echo "PASS none mode warns in the log" || echo "FAIL no warning"
docker --context "$CTX" rm -f $N >/dev/null
