# Platform adapters

Shared contracts and helpers stay at this level. Every platform owns a package
under this directory:

```text
backend/platform/
  adapter.py
  factory.py
  mapping.py
  gzctf/
    __init__.py
    adapter.py
  ret2shell/
    __init__.py
    adapter.py
    mcp.py
```

Platform packages expose their client and adapter through `__init__.py`.
`factory.py` imports those public exports. URLs, authentication, response
parsing, attachment handling, and instance lifecycle code stay inside the
matching platform package.

To add a platform, create `backend/platform/<name>/`, expose its public types
through that package's `__init__.py`, and register it in `factory.py`.

GZCTF participant sessions use the `GZCTF_Token` ASP.NET Identity cookie.
Set `IPC_GZ_BASE_URL` and `IPC_GZ_TOKEN` for IPC to use an existing browser
session. `IPC_GZ_USERNAME` and `IPC_GZ_PASSWORD` remain a fallback when no
cookie is supplied. Workflow credentials take precedence over deployment
defaults. Secrets are installed only in the private HTTP session.
