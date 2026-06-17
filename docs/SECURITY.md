# Security

FinSight's security model focuses on two layers: **network authentication** (service-to-service and user-facing) and the **Python sandbox** (`execute_python` MCP tool), which is the primary attack surface — user-supplied code is executed on the host machine.

## Authentication

### Bearer Token Auth

All HTTP endpoints are protected by `AuthMiddleware` (Starlette ASGI middleware in `src/shared/auth/middleware.py`). Auth is enabled via `AUTH_ENABLED=true` in `.env`.

| Principal Kind | Token Source | Where It Applies |
|---|---|---|
| **User** | JWT from `/auth/login` (Argon2 password) | Orchestrator API routes, AG-UI bridge, reports |
| **Service** | Static `SERVICE_AUTH_TOKEN` env var | A2A inter-agent calls, MCP client→server, sandbox |

Tokens are validated on every request. Unauthenticated requests receive `401 Unauthorized` immediately.

### JWT Authentication (User)

- Login at `POST /auth/login` with `username` + `password`
- Server returns an httpOnly `refresh_token` cookie + JSON body with `access_token` (short-lived JWT) and `expires_in` seconds
- Access tokens are signed with `HS256` using `AUTH_JWT_SECRETS` (comma-separated, rotation-friendly)
- Default TTL: 15 minutes (access), 7 days (refresh)
- Refresh at `POST /auth/refresh` — refresh token cookie is rotated on each use (old token invalidated)
- Logout at `POST /auth/logout` — deletes refresh token from DB
- Rate-limited lockout: 5 failed attempts → 60s cooldown (per username+IP). IP is taken from the socket address unless the direct peer is in `TRUSTED_PROXIES`, in which case `X-Forwarded-For` is used. In Docker Compose, set `TRUSTED_PROXIES` to the Next.js container IP so per-IP lockout works correctly behind the proxy. **Known tradeoff:** username-keyed lockout allows an attacker to DoS a known username; this is accepted given the single-host deployment model.

### Service-to-Service Authentication

- Sub-agents bind their A2A + eval endpoints to `accept=frozenset({"service"})` — only valid service tokens pass
- The orchestrator allows both user and service tokens on different paths, with per-route principal-kind checks
- MCP server (`finsight_server`) wraps its SSE mount with `AuthMiddleware(accept={"service"})`
- MCP client (`src/shared/mcp_client.py`) injects `SERVICE_AUTH_TOKEN` into every outbound SSE connection
- Agent cards include `securitySchemes` + `securityRequirements` for automatic A2A SDK negotiation

### Trusted Proxies (IP Lockout)

The rate-limited lockout key combines username + IP. By default, the IP is read from the direct socket address (`request.client.host`). In Docker Compose, the Next.js proxy forwards requests — the socket address is the proxy's IP, not the browser's. Set `TRUSTED_PROXIES` to the proxy's IP so `X-Forwarded-For` is used:

```ini
TRUSTED_PROXIES=172.18.0.5   # Next.js proxy container IP
```

Empty list (default) always uses socket address — `X-Forwarded-For` is never trusted.

### Token Generation

```bash
# User JWT signing secret (32+ hex characters)
openssl rand -hex 32

# Service-to-service shared secret (16+ characters)
openssl rand -hex 16
```

Add these to `.env`:
```ini
AUTH_ENABLED=true
AUTH_JWT_SECRETS=<32-byte-hex>
SERVICE_AUTH_TOKEN=<16-byte-hex>
```

### Creating Users

```bash
python scripts/create_user.py <username>
```

Prompts for password (interactive). Passwords hashed with Argon2id. User records stored in `db/users.db` (SQLite).

## Python Sandbox

### Three-Layer Defense

| Layer | Mechanism | Scope |
|---|---|---|
| **1. AST static analysis** | `_check_code_safety()` in `src/shared/sandbox.py` | Pre-execution gate — rejects forbidden constructs before any subprocess is spawned |
| **2. Subprocess isolation** | User code runs in a separate `subprocess.run()` with `-I -S` flags, or Docker container | Crash/memory error/segfault cannot affect the parent server |
| **3. OS resource limits** | `RLIMIT_CPU` (25s), `RLIMIT_AS` (512 MB), `RLIMIT_NOFILE` (0) | Unix only — kills infinite loops, memory bombs, and filesystem writes |

### Sandbox Modes

Controlled by `SANDBOX_MODE` env var (`ast` | `container` | `disabled`):

| Mode | How It Runs | Use Case |
|---|---|---|
| `ast` | AST gate + subprocess with resource limits (the existing mode) | Local development, Windows (Docker not available) |
| `container` | `docker run --rm --network none --memory 512m --cpus 1 --read-only --tmpfs /tmp:size=64m --user 65534:65534 python:3.12-slim` | Production — full OS isolation |
| `disabled` | Returns error: "Sandbox is disabled" | Controlled deployments where code execution is unnecessary |

- In `container` mode: if Docker is not available at runtime, falls back to `ast` with a loud warning
- Production startup (`PRODUCTION=true`) refuses to start with `ast` on Windows or `container` without Docker
- Every invocation is logged via `log_sandbox_execution()` (principal, mode, exit code, truncated output)

### Restricted Imports

The AST gate blocks imports of modules that provide filesystem, network, process, or reflection capabilities:

| Category | Blocked Modules |
|---|---|
| OS / process | `os`, `subprocess`, `shutil`, `shlex`, `signal`, `threading`, `multiprocessing`, `concurrent`, `pty`, `tty`, `termios`, `fcntl`, `mmap`, `resource`, `pwd`, `grp`, `crypt` |
| Networking | `socket`, `ssl`, `http`, `urllib`, `requests`, `ftplib`, `poplib`, `smtplib`, `telnetlib`, `xmlrpc`, `socketserver` |
| Filesystem | `pathlib`, `io`, `glob`, `fnmatch`, `tempfile`, `zipfile`, `tarfile`, `gzip`, `bz2`, `lzma` |
| Reflection | `ctypes`, `importlib`, `pickle`, `inspect`, `sys`, `builtins`, `gc`, `weakref`, `atexit` |
| Encoding | `base64`, `codecs` |

### Restricted Builtins

The AST gate blocks direct calls to:

`exec`, `eval`, `open`, `__import__`, `compile`, `globals`, `locals`, `vars`, `dir`, `delattr`, `setattr`

### Restricted Attributes

The AST gate blocks attribute access and method calls on:

`system`, `popen`, `execv`, `execve`, `execl`, `execvp`, `spawn`, `spawnl`, `fork`, `forkpty`, `exec`, `eval`, `__class__`, `bases__`, `__subclasses__`, `__mro__`, `__globals__`, `__builtins__`, `__code__`, `__closure__`, `__wrapped__`

### Subprocess Hardening

The child process runs with:

- **`-I` flag**: Isolated mode — ignores `PYTHONPATH`, `PYTHONHOME`, and user site-packages
- **`-S` flag**: No `site` module — prevents `usercustomize.py` and `sitecustomize.py` injection
- **Whitelisted `__builtins__`**: The child's `__builtins__` dict is replaced with a safe subset (print, len, range, int, float, str, bool, list, dict, etc.) — no `open`, `exec`, `__import__`
- **No filesystem FDs**: `RLIMIT_NOFILE` set to 0 on Unix — blocks filesystem writes even if the AST gate is bypassed
- **Cleaned up**: Temporary runner script deleted via `os.unlink()` in `finally` block

### Container Hardening

When `SANDBOX_MODE=container`:

- `--network none` — no network access
- `--memory 512m` — RAM limit
- `--cpus 1` — single CPU
- `--read-only` — read-only root filesystem
- `--tmpfs /tmp:size=64m` — writable temp space
- `--user 65534:65534` — nobody user
- `--rm` — container auto-removed on exit
- Timeout via `docker kill` with container label matching on timeout

### Allowed Libraries

The following libraries are available in the sandbox:

`pandas`, `numpy`, `math`, `json`, `datetime`, `random`, `statistics`, `itertools`, `collections`, `functools`, `typing`

### Test Coverage

`src/tests/security/test_sandbox.py` contains **60+ parametrized test cases**:

- **Negative cases**: Every restricted import, builtin call, dunder attribute, getattr-with-dunder, and subscript-with-dunder pattern
- **Positive cases**: Safe code (math, json, list comprehensions, `isinstance`, `str`) is not blocked
- **Integration tests** (marked `@pytest.mark.integration`): Spawn actual subprocess to verify runtime enforcement, timeout handling, and runtime import blocking

## Network Security

### CORS

The orchestrator allows cross-origin requests from `http://localhost:3000` and `http://127.0.0.1:3000` only. No other origins are permitted.

### Internal Rate Limiting

The MCP server applies rate limits to upstream data sources:

| Source | Rate | Burst |
|---|---|---|
| SEC EDGAR | 8 req/s | 10 |
| yfinance | 4 req/s | 8 |
| RSS feeds | 2 req/s | 4 |

### Secrets in Environment

All secrets and API keys are loaded from environment variables via `src/shared/settings.py`. No hardcoded secrets exist in source code. The `.env.example` file contains placeholder values.

### Service Binding

- MCP server binds to `127.0.0.1` by default (localhost-only for Docker host access)
- Docker Compose overrides to `0.0.0.0` for inter-container communication
- Agent HTTP servers bind to `0.0.0.0` (container-required) but accept only authenticated requests

### TLS Termination

In production, deploy behind a reverse proxy (Caddy, nginx) for TLS termination. See `deploy/Caddyfile.example`.

## Known Limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| No mTLS | Service-to-service bearer tokens are sent in plaintext between containers | Run on isolated Docker network (Compose default); deploy behind TLS proxy |
| SQLite databases unencrypted at rest | `db/` files on disk are readable | `db/` is gitignored; deploy with appropriate file permissions |
| AST sandbox on Windows | `RLIMIT_*` not enforced; relies only on AST gate + subprocess isolation | Avoid running `execute_python` on Windows in untrusted environments; use `container` mode |
| Single-host design | Agents run on one machine; no horizontal scaling | Acceptable for current deployment scale; revisit for multi-host |
| No input sanitization on MCP tools | Tool parameters passed directly to yfinance/SEC | yfinance and SEC are public APIs with their own rate limits |

## Responsible Disclosure

If you discover a security vulnerability in FinSight:

1. **Do not** open a public GitHub issue
2. Email the maintainers directly (see git log for contact info)
3. Include: description, reproduction steps, affected component, potential impact
4. Allow 90 days for a fix before public disclosure

## Hardening History

| Version | Change |
|---|---|
| v2.1 | `TRUSTED_PROXIES` setting closes IP-spoofing lockout bypass (EC5). `proxy.ts.disabled` — middleware that forced login redirect even with `AUTH_ENABLED=false` removed. `sub_agent_client.py` NameError fix (`__get_data_parts` → `_get_data_parts`). `SECURITY.md` fixed stale `shared/config.py` reference. |
| v2.7 | Frontend auth bypass via `NEXT_PUBLIC_AUTH_ENABLED=false` — separate frontend toggle from backend `AUTH_ENABLED`. Inline confidence clamping in report generation (bounded to 0-1 range). Shared agent output store — cross-process data sharing between orchestrator and reviewer without inline payload bloat. |
| v2.5 | Case-insensitive username matching — usernames normalized to lowercase at creation and lookup, preventing login failures from casing variations. Test isolation fix for `_schema_v4_ensured` flag. |
| v2.0 | Phase 3 auth audit — contract tests, parametrized auth × route matrix, `trace_with_user()` for Langfuse user_id propagation. |
| v1.43 | Bearer auth middleware for all HTTP endpoints. JWT user auth (Argon2 passwords, refresh token rotation, rate-limited lockout). Service-to-service A2A + MCP authentication. Sandbox container mode (Docker). Audit logging for all sandbox invocations. `TRUSTED_PROXIES` prevents IP-spoofing lockout bypass. |
| v1.41 | Centralized settings (`src/shared/settings.py` pydantic-settings) replaces `src/shared/config.py`. `src/shared/bootstrap.py` centralises process-level side-effects. MCP server module split (reduces attack surface per tool). Non-root USER in Dockerfiles. |
| v1.40 | Deferred Eval Gate (`src/shared/eval_gate.py`) — sub-agent evals held until orchestrator releases them. Prevents concurrent eval-LLM competition. |
| v1.38 | LLM Priority Queue (`src/shared/llm_queue.py`) — 3-tier async semaphore prevents RAGAS eval starvation of production LLM inference. |
| v1.36 | `_ALLOWED_TABLES` whitelist in `prune_old_records()` prevents SQL injection via table name. |
| v1.27 | Sandbox extracted to `src/shared/sandbox.py` (was `shared/sandbox.py` before src/ layout). Expanded `_RESTRICTED_IMPORTS` from 12 to 50+ modules. Added `shlex`, `concurrent`, `ssl`, `http`, `urllib`, `requests`, `ftplib`, `poplib`, `smtplib`, `telnetlib`, `xmlrpc`, `socketserver`, `pathlib`, `io`, `glob`, `fnmatch`, `tempfile`, `zipfile`, `tarfile`, `gzip`, `bz2`, `lzma`, `base64`, `codecs`. 60 AST-gate tests added. |
| v1.27 | `preexec_fn` moved inside `_sandbox_preexec()` with try/except for Windows compatibility. Subprocess runs with `-I -S` isolation flags. |
| v1.25 | `SEC_USER_AGENT` and `LLM_API_KEY` moved to env vars. No hardcoded secrets. |
| v1.25 | SQLite WAL mode + busy_timeout set once at singleton init. |
| v1.25 | `_ALLOWED_TABLES` whitelist in `prune_old_records()` prevents SQL injection via table name. |
