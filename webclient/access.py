"""Access control and reverse-proxy support for the multiviewer gateway.

Login is ON by default. The built-in login (HTTP Basic) is a fallback: the
intended setup is a reverse proxy with an authentication gateway in front
(Authelia, authentik, oauth2-proxy, Pomerium, Cloudflare Access, Tailscale
Serve, ...), which passes the signed-in user to this gateway in a header.

Environment (all optional):

  KVM_AUTH          basic (default) | proxy | none
  KVM_USER          basic: user name (default "kvm")
  KVM_PASS          basic: password. Unset = one is generated on first start,
                    saved to STATE_DIR/kvm-password (mode 600) and printed to
                    the log, so a fresh install is never open.
  KVM_TOKENS        comma-separated bearer tokens accepted in every mode
                    ("Authorization: Bearer <token>"), for scripts
  TRUSTED_PROXIES   comma-separated addresses/CIDRs of your reverse proxies
                    (default 127.0.0.1/32,::1/128). Only requests from these
                    may set X-Forwarded-* / Forwarded or an identity header.
  AUTH_USER_HEADER  proxy: header carrying the user. Unset = the first present
                    of PROXY_USER_HEADERS below.
  AUTH_ALLOWED_USERS  proxy: comma-separated users allowed in (case-insensitive);
                    unset = anyone the proxy lets through
  AUTH_JWKS_URL     proxy: verify a signed JWT instead of trusting the source
                    address (ex. https://<team>.cloudflareaccess.com/cdn-cgi/access/certs)
  AUTH_JWT_HEADER   header holding that JWT (default Cf-Access-Jwt-Assertion)
  AUTH_JWT_AUDIENCE expected "aud" (required with AUTH_JWKS_URL)
  AUTH_JWT_ISSUER   expected "iss" (optional)
  AUTH_JWT_USER_CLAIM  claim to use as the user name (default: email, then sub)
  ALLOWED_ORIGINS   extra origins allowed to open the keyboard/mouse socket
                    (ex. https://kvm.example.com), on top of the page's own host
"""
import asyncio, base64, hmac, ipaddress, os, secrets, sys

from aiohttp import web

# Identity headers set by common auth gateways, in lookup order.
PROXY_USER_HEADERS = (
    "Remote-User",                         # Authelia, Caddy forward_auth, generic
    "X-Remote-User",
    "X-Forwarded-User",                    # oauth2-proxy (--pass-user-headers), nginx auth_request
    "X-Auth-Request-User",                 # oauth2-proxy (--set-xauthrequest)
    "X-Auth-Request-Email",
    "X-Forwarded-Email",
    "X-Authentik-Username",                # authentik proxy outpost
    "X-Pomerium-Claim-Email",              # Pomerium
    "Tailscale-User-Login",                # Tailscale Serve
    "Cf-Access-Authenticated-User-Email",  # Cloudflare Access
)
OPEN_PATHS = ("/healthz",)                 # health checks work without a login


def _split(v):
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _same(a, b):
    """Constant-time string compare that never raises on non-ASCII input."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class Access:
    def __init__(self, env=None, state_dir=""):
        env = os.environ if env is None else env
        self.mode = (env.get("KVM_AUTH") or "basic").strip().lower()
        if self.mode not in ("basic", "proxy", "none"):
            sys.exit(f"KVM_AUTH={self.mode!r}: use basic, proxy or none")
        self.user = env.get("KVM_USER") or "kvm"
        self.password = env.get("KVM_PASS") or ""
        self.generated = False
        if self.mode == "basic" and not self.password:
            self.password, self.generated = self._generated_password(state_dir)
        self.tokens = _split(env.get("KVM_TOKENS"))
        try:
            self.trusted = [ipaddress.ip_network(x, strict=False)
                            for x in _split(env.get("TRUSTED_PROXIES") or "127.0.0.1/32,::1/128")]
        except ValueError as e:
            sys.exit(f"TRUSTED_PROXIES: {e} (use addresses or CIDRs, ex. 10.0.0.5,172.18.0.0/16)")
        self.user_header = (env.get("AUTH_USER_HEADER") or "").strip()
        self.allowed_users = {u.lower() for u in _split(env.get("AUTH_ALLOWED_USERS"))}
        self.origins = {o.rstrip("/").lower() for o in _split(env.get("ALLOWED_ORIGINS"))}
        self.jwks_url = (env.get("AUTH_JWKS_URL") or "").strip()
        self.jwt_header = env.get("AUTH_JWT_HEADER") or "Cf-Access-Jwt-Assertion"
        self.jwt_aud = env.get("AUTH_JWT_AUDIENCE") or ""
        self.jwt_iss = env.get("AUTH_JWT_ISSUER") or None
        self.jwt_claim = env.get("AUTH_JWT_USER_CLAIM") or ""
        self.jwks = None
        if self.jwks_url:
            if not self.jwt_aud:
                sys.exit("AUTH_JWKS_URL is set but AUTH_JWT_AUDIENCE is not: "
                         "without an audience check a token for another app would pass")
            import jwt  # PyJWT, only needed for JWT verification
            self.jwks = jwt.PyJWKClient(self.jwks_url, cache_keys=True, lifespan=3600)

    @staticmethod
    def _generated_password(state_dir):
        path = os.path.join(state_dir, "kvm-password") if state_dir else ""
        if path:
            try:
                with open(path) as f:
                    pw = f.read().strip()
                if pw:
                    return pw, True
            except OSError:
                pass
        pw = secrets.token_urlsafe(18)
        if path:
            try:
                os.makedirs(state_dir, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(pw + "\n")
            except OSError as e:
                print(f"generated password not saved ({e}); a new one is made on "
                      f"every start until STATE_DIR is writable", flush=True)
        return pw, True

    def startup_notice(self):
        """Lines for the log at start: what protects the page, and the advice."""
        out = []
        if self.mode == "none":
            out.append("WARNING: KVM_AUTH=none -- the page, the video and the keyboard/mouse "
                       "relay are open to anyone who can reach this port.")
        elif self.mode == "basic":
            if self.generated:
                out.append(f"login: user {self.user!r}, generated password {self.password} "
                           f"(set KVM_PASS to choose one)")
            else:
                out.append(f"login: user {self.user!r}, password from KVM_PASS")
        else:
            how = (f"signed JWT in {self.jwt_header}" if self.jwks
                   else f"identity header from {', '.join(map(str, self.trusted))}")
            out.append(f"login: delegated to the reverse proxy ({how})")
        if self.mode != "proxy":
            out.append("NOTE: the built-in login is a fallback. For real use put this behind a "
                       "reverse proxy with an authentication gateway (Authelia, authentik, "
                       "oauth2-proxy, ...) and set KVM_AUTH=proxy; see README, 'Reverse proxy and login'.")
        return out

    # -- proxy trust ----------------------------------------------------------
    def from_proxy(self, request):
        peer = request.transport.get_extra_info("peername") if request.transport else None
        if not peer:
            return False
        try:
            ip = ipaddress.ip_address(peer[0])
        except ValueError:
            return False
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in net for net in self.trusted)

    def forwarded(self, request):
        """Client address, scheme, host and path prefix as the browser sees them.
        X-Forwarded-* (or RFC 7239 Forwarded) count only from a trusted proxy."""
        out = {"for": request.remote, "proto": request.scheme,
               "host": request.host, "prefix": ""}
        if not self.from_proxy(request):
            return out
        h = request.headers
        fwd = h.get("Forwarded")
        if fwd:
            first = fwd.split(",")[0]
            for part in first.split(";"):
                k, _, v = part.strip().partition("=")
                k, v = k.lower(), v.strip('"')
                if k in ("for", "proto", "host") and v:
                    out[k] = v
        if h.get("X-Forwarded-For"):
            out["for"] = h["X-Forwarded-For"].split(",")[0].strip()
        if h.get("X-Forwarded-Proto"):
            out["proto"] = h["X-Forwarded-Proto"].split(",")[0].strip()
        if h.get("X-Forwarded-Host"):
            out["host"] = h["X-Forwarded-Host"].split(",")[0].strip()
        prefix = h.get("X-Forwarded-Prefix", "").split(",")[0].strip().rstrip("/")
        if prefix.startswith("/") and "//" not in prefix:
            out["prefix"] = prefix
        return out

    def origin_ok(self, request):
        """The keyboard/mouse socket accepts only this page: a browser always sends
        Origin on a WebSocket, so another site open in the viewer's browser cannot
        drive the consoles."""
        origin = request.headers.get("Origin", "").rstrip("/").lower()
        if not origin:
            return False
        if origin in self.origins:
            return True
        return origin.split("://", 1)[-1] == self.forwarded(request)["host"].lower()

    # -- authentication ---------------------------------------------------------
    def _bearer(self, request):
        hdr = request.headers.get("Authorization", "")
        if not hdr.startswith("Bearer ") or not self.tokens:
            return None
        got = hdr[7:].strip()
        ok = False
        for t in self.tokens:              # check all: timing does not reveal which
            ok |= _same(got, t)
        return "token" if ok else None

    def _basic(self, request):
        hdr = request.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return None
        try:
            got = base64.b64decode(hdr[6:], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        gu, _, gp = got.partition(":")
        return gu if (_same(gu, self.user) & _same(gp, self.password)) else None

    def _proxy_user(self, request):
        if self.jwks:
            token = request.headers.get(self.jwt_header, "")
            if not token:
                return None, "no signed token from the proxy"
            import jwt
            try:
                key = self.jwks.get_signing_key_from_jwt(token)
                claims = jwt.decode(token, key.key, algorithms=["RS256", "ES256", "PS256", "EdDSA"],
                                    audience=self.jwt_aud, issuer=self.jwt_iss,
                                    options={"require": ["exp", "aud"]})
            except Exception as e:          # bad signature, expired, wrong aud, JWKS down
                return None, f"token rejected ({type(e).__name__})"
            user = (claims.get(self.jwt_claim) if self.jwt_claim
                    else claims.get("email") or claims.get("sub"))
            if not user:
                return None, "token has no user claim"
        else:
            if not self.from_proxy(request):
                return None, "not from a trusted proxy (TRUSTED_PROXIES)"
            names = (self.user_header,) if self.user_header else PROXY_USER_HEADERS
            user = next((request.headers[n].strip() for n in names
                         if request.headers.get(n, "").strip()), None)
            if not user:
                return None, "the proxy sent no user header"
        if self.allowed_users and user.lower() not in self.allowed_users:
            return None, f"user {user!r} is not in AUTH_ALLOWED_USERS"
        return user, None

    def identify(self, request):
        """(user or None, reason). In mode none everyone is 'anonymous'."""
        tok = self._bearer(request)
        if tok:
            return tok, None
        if self.mode == "none":
            return "anonymous", None
        if self.mode == "basic":
            u = self._basic(request)
            return (u, None) if u else (None, "login required")
        return self._proxy_user(request)

    def middleware(self, base):
        @web.middleware
        async def mw(request, handler):
            path = request.path[len(base):] if base and request.path.startswith(base) else request.path
            if path in OPEN_PATHS:
                return await handler(request)
            # JWKS fetches block (urllib), so JWT checks run off the event loop.
            user, why = (await asyncio.to_thread(self.identify, request) if self.jwks
                         else self.identify(request))
            if user is None:
                if self.mode == "basic":
                    return web.Response(status=401, text="authentication required", headers={
                        "WWW-Authenticate": 'Basic realm="KVM multiviewer", charset="UTF-8"'})
                print(f"access refused for {self.forwarded(request)['for']}: {why}", flush=True)
                return web.Response(status=403, text="access denied: sign in through the reverse proxy")
            request["user"] = user
            return await handler(request)
        return mw
