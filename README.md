# Simplify MCP

A local, read-only MCP server for your Simplify application history. It runs over stdio using the [official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk).

## Setup

Install uv and run these commands in the repository. [uv documentation](https://docs.astral.sh/uv/)

```sh
uv sync --locked
(umask 077; cp -n .env.example .env)
```

1. Open the Simplify tracker while signed in. Open DevTools, select **Network**, and reload the page. [Simplify tracker](https://simplify.jobs/tracker)
2. Open a successful GET to `https://api.simplify.jobs/v2/candidate/me/tracker/`. In the request's **Cookies** tab, copy the full values for `refresh` and `csrf`.
3. Set `SIMPLIFY_REFRESH` and `SIMPLIFY_CSRF` in the local `.env`. Use the two values from the same browser session. Git ignores this file.

If Simplify returns 401 or 403, repeat these steps with a working browser session and restart the MCP server.

## Connect an agent

Configure your MCP client to launch the server over stdio. For clients with an `mcpServers` config, use this entry with your absolute repository path. Use the absolute path to `uv` if your client cannot find it on its PATH.

```json
{
  "mcpServers": {
    "simplify": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/simplify-mcp",
        "run", "--locked", "--env-file", ".env", "python", "server.py"
      ]
    }
  }
}
```

The `list_applications` tool returns the tracker ID, company, original role title, location, job URL, current status, applied date, status date, and whether each entry is archived. It includes both active and archived applications and skips saved-only jobs. `total` counts matching applications before paging, and `next_offset` tells the client where to start the next page.

- `{}` returns the first 50 applications, ordered by when they were added to the tracker, newest first.
- `{"query": "Datadog"}` searches company names and titles, ignoring case.
- `{"status": "APPLIED", "archived": true}` returns archived applications with that status.
- `{"limit": null}` returns the complete history in one response.

## Tracker endpoint

Each call reads Simplify's first-party `GET /v2/candidate/me/tracker/` JSON endpoint. Omitting the `archived` parameter includes both active and archived records. The server requests up to 500 tracker records per page and derives each current status using the same status code ordering as the tracker board.

The CSV export includes both sets of records and their statuses, but Simplify rewrites some job titles in that export. The JSON list preserves the original titles and record IDs. The chart endpoint returns aggregates, and the applied-posting endpoint returns only a subset of posting IDs.

The server reads the current tracker state on each call. Two browser cookies and the matching CSRF header authenticate the request. This endpoint is used by the Simplify website, so changes to its session or response format may require an update.
