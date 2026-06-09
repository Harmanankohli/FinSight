# Security

FinSight's security model focuses on the Python sandbox (`execute_python` MCP tool), which is the primary attack surface — user-supplied code is executed on the host machine.

## Python Sandbox

### Three-Layer Defense

| Layer | Mechanism | Scope |
|---|---|---|
| **1. AST static analysis** | `_check_code_safety()` in `shared/sandbox.py` | Pre-execution gate — rejects forbidden constructs before any subprocess is spawned |
| **2. Subprocess isolation** | User code runs in a separate `subprocess.run()` with `-I -S` flags | Crash/memory error/segfault cannot affect the parent server |
| **3. OS resource limits** | `RLIMIT_CPU` (25s), `RLIMIT_AS` (512 MB), `RLIMIT_NOFILE` (0) | Unix only — kills infinite loops, memory bombs, and filesystem writes |

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

### Allowed Libraries

The following libraries are available in the sandbox:

`pandas`, `numpy`, `math`, `json`, `datetime`, `random`, `statistics`, `itertools`, `collections`, `functools`, `typing`

### Test Coverage

`tests/security/test_sandbox.py` contains **60+ parametrized test cases**:

- **Negative cases**: Every restricted import, builtin call, dunder attribute, getattr-with-dunder, and subscript-with-dunder pattern
- **Positive cases**: Safe code (math, json, list comprehensions, `isinstance`, `str`) is not blocked
- **Integration tests** (marked `@pytest.mark.integration`): Spawn actual subprocess to verify runtime enforcement, timeout handling, and runtime import blocking

## Network Security

### No Authentication

FinSight does not implement user authentication. The `X-FinSight-User-Id` header is a convention for multi-user differentiation, not a security mechanism. The system is designed for local/single-user deployment.

### CORS

The orchestrator allows cross-origin requests from `http://localhost:3000` and `http://127.0.0.1:3000` only. No other origins are permitted.

### Internal Rate Limiting

The MCP server applies rate limits to upstream data sources:

| Source | Rate | Burst |
|---|---|---|
| SEC EDGAR | 8 req/s | 10 |
| yfinance | 4 req/s | 8 |
| RSS feeds | 2 req/s | 4 |

### No Secrets in Code

All secrets and API keys are loaded from environment variables via `shared/config.py`. No hardcoded secrets exist in source code. The `.env.example` file contains placeholder values.

## Known Limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| No HTTPS | Traffic is unencrypted | System is local-only; not exposed to the internet |
| No authentication | Anyone with network access can query | Deploy behind a firewall or VPN |
| No input sanitization on MCP tools | Tool parameters are passed directly to yfinance/SEC | yfinance and SEC are public APIs with their own rate limits |
| SQLite databases are unencrypted | DB files on disk are readable | `db/` is gitignored; deploy with appropriate file permissions |
| `RLIMIT_*` not enforced on Windows | Sandbox on Windows relies on AST gate + subprocess isolation only | Avoid running `execute_python` on Windows in untrusted environments |

## Responsible Disclosure

If you discover a security vulnerability in FinSight:

1. **Do not** open a public GitHub issue
2. Email the maintainers directly (see git log for contact info)
3. Include: description, reproduction steps, affected component, potential impact
4. Allow 90 days for a fix before public disclosure

## Hardening History

| Version | Change |
|---|---|
| v1.27 | Sandbox extracted to `shared/sandbox.py`. Expanded `_RESTRICTED_IMPORTS` from 12 to 50+ modules. Added `shlex`, `concurrent`, `ssl`, `http`, `urllib`, `requests`, `ftplib`, `poplib`, `smtplib`, `telnetlib`, `xmlrpc`, `socketserver`, `pathlib`, `io`, `glob`, `fnmatch`, `tempfile`, `zipfile`, `tarfile`, `gzip`, `bz2`, `lzma`, `base64`, `codecs`. 60 AST-gate tests added. |
| v1.27 | `preexec_fn` moved inside `_sandbox_preexec()` with try/except for Windows compatibility. Subprocess runs with `-I -S` isolation flags. |
| v1.25 | `SEC_USER_AGENT` and `LLM_API_KEY` moved to env vars. No hardcoded secrets. |
| v1.25 | SQLite WAL mode + busy_timeout set once at singleton init. |
| v1.36 | `_ALLOWED_TABLES` whitelist in `prune_old_records()` prevents SQL injection via table name. |
