# n63read cloud

A Cloudflare Worker that keeps a copy of the car's logs and history and
serves it to Claude from anywhere:

- **R2** holds every drive log and fault list the app uploads.
- **D1** holds the drive and fault summaries (the app's History).
- **`/mcp`** is a remote MCP server with read-only tools over that copy:
  `drive_history`, `fault_history`, `trend`, `list_files`, `read_log`.
- **Cloudflare Access** guards the whole hostname, and the Worker checks the
  Access token itself on every request, so it fails closed if Access is
  missing or misconfigured. The app signs in with an Access service token;
  you and MCP portals sign in with your Access login.

Plain JavaScript, no packages: `src/worker.js`. Tests run on Node 24:
`node --test "cloud/test/*.test.mjs"` (from the repo root).

## What it holds

Everything the app logs: drive logs with GPS positions, fault lists with the
VIN where a module reports it, and the summaries. The bucket and database are
private to your account; only requests that pass Access reach them.

## Deploy

You need Node.js and a Cloudflare account with a Zero Trust organization.
Run these in `cloud/`; nothing here is created until you run it.

1. **Storage.**

   ```sh
   npx wrangler login
   npx wrangler r2 bucket create n63read-logs
   npx wrangler d1 create n63read
   ```

   Copy `wrangler.example.jsonc` to `wrangler.jsonc` (git ignores it) and
   put the D1 database ID in it. The tables are made on first use.

2. **Worker.**

   ```sh
   npx wrangler deploy
   ```

   It answers at `https://n63read-cloud.<your-subdomain>.workers.dev`, but
   refuses everything until Access is set up.

3. **Access.** In the Cloudflare dashboard, *Zero Trust > Access >
   Applications > Add an application > Self-hosted*:
   - Domain: the Worker's hostname (or a custom domain routed to it).
   - Policy 1, *Allow*: your email (or your identity provider group).
   - Policy 2, *Service Auth*: a service token made for the app
     (*Access > Service credentials > Service tokens > Create*). Copy its
     client ID and secret; the secret is shown once.
   - *Advanced settings*: turn on **Managed OAuth**, so MCP clients and portals
     can sign in through Access without a browser cookie.
   - Copy the application's *Application Audience (AUD) tag*.

   Then give the Worker the audience and your team domain:

   ```sh
   npx wrangler secret put ACCESS_AUD           # the AUD tag
   npx wrangler secret put ACCESS_TEAM_DOMAIN   # https://<team>.cloudflareaccess.com
   ```

4. **The app.** *Settings > Cloud*: the Worker URL, the service token's
   client ID and secret, *Save*, then *Sync now*. Or from the CLI:

   ```sh
   n63read sync --url https://n63read-cloud.<sub>.workers.dev --client-id <ID> --client-secret <SECRET>
   ```

   The secret is kept encrypted for your Windows user. With *Upload logs
   automatically* on, the app uploads after each log and fault read and every
   ten minutes, so anything logged offline goes up once the PC is online.

5. **Claude.** Add `https://<worker host>/mcp` to your MCP server portal
   (*Zero Trust > Access controls > MCP Portals > MCP servers*, OAuth
   credentials *Automatic*), or to any
   MCP client that can log in through Access. The tools then read the cloud
   copy, with or without the laptop.

## API

All requests need an Access token (a login, or the service token headers
`CF-Access-Client-Id` and `CF-Access-Client-Secret`).

| Request | Does |
|---|---|
| `PUT /api/files/<path>` | store a log (CSV body); replies with its size and SHA-256 |
| `GET /api/files` | list stored files: key, size, SHA-256, upload time |
| `GET /api/files/<path>` | the file back |
| `PUT /api/history` | replace the summaries: `{"drives": [...], "faults": [...]}` |
| `POST /mcp` | MCP (JSON-RPC 2.0, streamable HTTP with JSON replies) |
| `GET /api/status` | who you're signed in as |
