# Platform adapters

Shared contracts and helpers stay at this level. Every platform owns a package
under this directory:

```text
backend/platform/
  adapter.py
  errors.py
  factory.py
  mapping.py
  presets.py
  gzctf/
    __init__.py
    adapter.py
    workflow.py
  ret2shell/
    __init__.py
    adapter.py
    mcp.py
    workflow.py
  tsecbench/
    __init__.py
    adapter.py
    workflow.py
  ichunqiu/
    __init__.py
    adapter.py
    browser_setup.py
    workflow.py
  nestctf/
    __init__.py
    adapter.py
    workflow.py
```

`nestctf` is the 强网拟态 console: the race list endpoint only publishes the
score board, so the adapter reads each checkpoint detail to learn `with_scene`
(the container/non-container split), the description and the handout URL.
`attachments_only` therefore imports exactly the challenges a team can solve
without waiting for a scene instance.

Platform packages expose their client and adapter through `__init__.py`.
`factory.py` imports those public exports. URLs, authentication, response
parsing, attachment handling, and instance lifecycle code stay inside the
matching platform package.

Each native package also owns its workflow preset in `workflow.py`: the
template metadata, the IPC tool schema and description, the input model, its
secret names, its identity validation, and a `configure()` hook.
`backend/platform/presets.py` is the only preset registry the shared layers
use; the ops service, IPC tools, API routes, `AppState` and the orchestrator
must not branch on a platform name. A package may also expose
`build_deployment(environ)` when environment-configured clients and Member
MCPs should be registered at startup.

To add a platform, create `backend/platform/<name>/` with `adapter.py` and
`workflow.py`, expose its public types through the package, and register it in
`factory.py` and `presets.py`.

## Workflow lifecycle

A proposed workflow is usable immediately: `POST /api/workflows/{id}/preflight`
reads the platform challenge list, and the operator presses Start in the IPC
panel to create the run. Neither call needs a CONFIRM phrase - Start is the
authorization. The capability token from
`POST /api/ops/workflows/{id}/confirm` is only required by the legacy
single-shot `POST /api/ops/workflows/{id}/execute` API.

## Operator CLIs

Commands that only touch one platform's HTTP API live in that package, next to
the client they exercise, and run as modules so nothing has to be installed
twice:

```bash
python -m backend.platform.gzctf.check        --game 2
python -m backend.platform.gzctf.fetch_targets --game 2
python -m backend.platform.ret2shell.check         --base-url https://ctf.example --game 12
python -m backend.platform.ret2shell.fetch_targets --base-url https://ctf.example --game 12
python -m backend.platform.ret2shell.submit       --base-url https://ctf.example --game 12 --challenge 42 --flag flag{...}
python -m backend.platform.tsecbench.run      --token ... --base-url ...
python -m backend.platform.tsecbench.browser_setup
python -m backend.platform.ichunqiu.browser_setup --url-key 2026qwbs10
```

Each module imports its heavy dependency inside `main()` (`tsec_benchmark`,
`playwright`), so `pip install ipc-ctfagent` and every `import backend...` keep
working without the evaluation SDK or a browser. Those extras are declared as
`[project.optional-dependencies]` in `pyproject.toml`.

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

i春秋 (ichunqiu) matches authenticate with the account `token` captured from a
logged-in browser session into the workflow secret store (`ichunqiu_token`);
every call is signed with the site's `SIGN` header, so no cookie jar is needed.
The login form is captcha-protected, so `browser_setup` opens a persistent Edge
profile and saves the token the page itself received. Container entries arrive as
either an `https://…` URL or an `nc host port` hint, normalised to `host:port`
before they reach a task container. The team container limit reported by the
match (`team_docker_limit`) becomes `instance_limit()`. Every mapping accepts a
generic `attachments_only` flag: a workflow started with it imports only
challenges that are solved from a download (container challenges are skipped,
`supports_instances` turns false and `start_instance` refuses), and the imported
list is ordered by the platform's solve counter so the softest target is planned
first. The same mapping also accepts `exclude`, a list of challenge ids or titles
the run must never import — useful for a task whose download is too large to be
worth pulling.

## Built-in workflows

The fixed workflow definition and personalization model live with their adapter:
`gzctf/workflow.py`, `tsecbench/workflow.py` and `ichunqiu/workflow.py`.
`backend.ops.presets` remains the shared registry and re-exports its original
symbols for compatibility.

Use `POST /api/ops/workflows/gzctf` with the platform origin, game ID and token
or account credentials. Use `POST /api/ops/workflows/tsecbench` with the benchmark
origin and `secrets.benchmark_token`; no game ID is required. Use
`POST /api/ops/workflows/ret2shell` with the platform origin, game ID and
`secrets.ret2shell_token` (or `ret2shell_username` / `ret2shell_password`).
Use `POST /api/ops/workflows/ichunqiu` with the match slug as `url_key` and
`secrets.ichunqiu_token`. The ret2shell origin and game ID have no built-in
defaults: IPC asks the operator for both before it creates the draft. The
Competition UI provides the GZCTF and TSecBench setup forms plus one IPC-guided
preset path for every template. Saved configurations can be edited and reused,
while each run retains its original template version and configuration snapshot.

Every template also exposes an `ipc_setup` contract: the fields IPC must collect,
the native tool to call, and a setup prompt. IPC chat can therefore start from the
preset and ask the operator for only the missing personalization. Credentials are
collected through the structured question flow and stored in the workflow secret
store; they never appear in workflow settings or chat history.

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
