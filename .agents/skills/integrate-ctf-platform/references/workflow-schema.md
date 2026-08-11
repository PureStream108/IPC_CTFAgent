# IPC platform workflow reference

Use this reference when drafting or debugging `PlatformWorkflowSpec` JSON.

## Lifecycle

1. Create or update a draft at `/api/ops/workflows`.
2. Store credential values separately in the request's `secrets` object.
3. Inspect `/api/ops/workflows/{id}/confirmation`.
4. Require the human to submit the exact confirmation phrase.
5. Keep the returned execution token private; edit/revoke invalidates it.
6. Execute `preview` before `import`; execute `submit` only for a confirmed flag.

Never use the legacy `/api/platform` mapping endpoint for agent-created integrations. It accepts
literal headers and does not provide the confirmed capability lifecycle.

## Schema

```json
{
  "name": "Example CTF",
  "challenges": {
    "list_url": "https://ctf.example/api/challenges",
    "list_path": "data.challenges",
    "id_field": "id",
    "title_field": "name",
    "category_field": "category",
    "description_field": "description",
    "attachments_field": "files",
    "category_map": {"Web Exploitation": "web"},
    "attachment_base_url": "https://ctf.example/assets/",
    "headers": [
      {
        "name": "Authorization",
        "secret_name": "platform_token",
        "prefix": "Bearer "
      }
    ],
    "attachment_headers": []
  },
  "submit": {
    "url": "https://ctf.example/api/challenges/{{external_id}}/submit",
    "method": "POST",
    "headers": [
      {
        "name": "Authorization",
        "secret_name": "platform_token",
        "prefix": "Bearer "
      }
    ],
    "json_template": {"flag": "{{flag}}"},
    "success_statuses": [200],
    "success_path": "success",
    "success_values": [true]
  },
  "allow_private_networks": false,
  "max_attachment_bytes": 104857600
}
```

Omit `submit` when its endpoint is unknown. Do not invent one.

## Path and attachment rules

- Paths use dot-separated object keys and zero-based list indices; escaping dots in keys is not supported.
- `list_path` resolves from the response root to the challenge array.
- Other field paths resolve relative to each challenge object.
- Attachments may be strings, arrays, filename-to-URL maps, or objects using `url`,
  `download_url`, `download`, `href`, `path`, or `file_url`. Object keys are normalized from
  camelCase to snake_case, so `downloadUrl` and `fileUrl` are accepted equivalents.
- Set `attachment_base_url` to the confirmed attachment/CDN origin when references are relative or
  use another origin. Absolute URLs returned at runtime still have to match a confirmed origin.
- Challenge `headers` are inherited by attachments only when the attachment has the same origin as
  `list_url`. Cross-origin attachments receive no challenge credentials by default. Configure
  `attachment_headers` explicitly only when the confirmed attachment origin requires its own secret.
- Map unknown categories to one of IPC's configured categories; otherwise IPC uses `misc`.

## Secret rules

- Put only aliases such as `platform_token` in workflow JSON.
- Send values through the API's separate `secrets` object. Never write values into URLs, workflow
  files, chat history, logs, headers, or `json_template` literals.
- Use `{{secret.NAME}}` only in submit JSON values. Header secrets use `secret_name` plus `prefix`.
- URL query credentials, URL userinfo, fragments, backslashes, invalid ports, and transport-control
  headers such as `Host`, `Content-Length`, and `Transfer-Encoding` are rejected.

## Success semantics

- `success_statuses` is necessary but may not be sufficient.
- When the response has a reliable result field, set `success_path` and enumerate exact
  `success_values`; compare booleans as booleans, not strings.
- If the platform returns success in plain text or an unstable envelope, leave submission disabled
  until the contract is known instead of treating every HTTP 200 as flag acceptance.

## Diagnostic order

1. Validate the saved JSON with `scripts/validate_workflow.py`.
2. Confirm list and field paths against at least two challenge objects.
3. Check the displayed origins, methods, header names, and required secret aliases.
4. Run `preview`; do not import on a mapping guess.
5. For attachment failures, verify `attachment_base_url`, returned object keys, redirects, and size.
6. For submit failures, verify URL substitution, JSON types, status code, success path, and values.
