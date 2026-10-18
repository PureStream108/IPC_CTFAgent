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
  tsecbench/
    __init__.py
    adapter.py
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

TSecBench tasks authenticate with the `BENCHMARK_TOKEN` header from the
workflow secret store (`benchmark_token`). Challenge entries are direct
`ip:port` addresses behind the benchmark VPN, which the deployment connects;
the adapter never dials the VPN itself. The platform caps active containers
at three, published through `instance_limit()`.

## Built-in workflows

The fixed workflow definition and personalization model live with their adapter:
`gzctf/workflow.py` and `tsecbench/workflow.py`. `backend.ops.presets` remains the
shared registry and re-exports its original symbols for compatibility.

Use `POST /api/ops/workflows/gzctf` with the platform origin, game ID and token
or account credentials. Use `POST /api/ops/workflows/tsecbench` with the benchmark
origin and `secrets.benchmark_token`; no game ID is required. The Competition UI
provides both setup forms. Saved configurations can be edited and reused, while
each run retains its original template version and configuration snapshot.

Optional `category_map` overrides classify specific external IDs, source
categories, or `*`; `{"*":"pentest"}` imports tasks into the Pentest workspace.
TSecBench concurrency is bounded to three, with zero inheriting the platform
limit. Hints reduce scores and require explicit `allow_hints: true`; preflight
never requests a hint or starts a container.

TSecBench preflight reports application-side connectivity to any already
available target addresses, plus the separate requirement for VPN connectivity
from task containers. A successful platform API request alone does not establish
target reachability. Partial correct submissions and per-flag duplicate results
retain solver capacity until all platform flags are complete.

`python scripts/check_workflow_package.py` verifies that both workflows can be
loaded from an installed wheel outside the source tree.
