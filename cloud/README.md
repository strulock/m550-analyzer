# n63read cloud

A Cloudflare Worker that keeps a copy of the car's logs and history and
serves it to Claude from anywhere:

- **R2** holds every drive log and fault list the app uploads.
- **D1** holds the drive and fault summaries (the app's History).
- **`/mcp`** is a remote MCP server with read-only tools over that copy:
  `drive_history`, `fault_history` (with each fault's stored details on
  request), `trend`, `list_files`, `read_log`, `compare_banks` and
  `find_events` (over one stored log), `list_channels`, `channel_series`,
  `list_templates`.
- **Templates:** the app's saved channel sets, merged both ways on every sync.
- **Time series:** every value of every drive log with its UTC time, one row
  per channel per drive in D1 (`series`), so `list_channels` and
  `channel_series` can follow a channel across every drive, averaged into
  time buckets when long, or as per-drive min, mean and max.
- **The logger board** (`firmware/`) uploads its drive logs under `logger/`,
  two files a drive: `<start>.csv` (the fast and medium channels, every
  cycle) and `<start>.sweep.csv` (the slow channels, one row a pass).
  The Worker works out their time series and drive summary itself
  (`src/drive.js`, a port of the app's `series.py` and `history.summarize`),
  both files counting as one drive under the first's name, so they show in
  `drive_history`, `trend` and the series tools without the PC. The app's
  history sync replaces only its own drives, and each PC's sync only its own
  drives and faults (the app sends its PC's name).
  Its fault reads during a drive (`<start>.faults.csv`) go into fault
  history under source `logger`, merged as the app's own reads are: first
  and last seen, reads listing it, present, stored or gone. The board sends
  codes only; the texts come from n63read's table, which the app uploads once
  to `ref/BMW_fault_code_texts.csv` (again when it changes).
- **Cloudflare Access** guards the whole hostname, and the Worker checks the
  Access token itself on every request, so it fails closed if Access is
  missing or misconfigured. The app signs in with an Access service token;
  you and MCP portals sign in with your Access login.

Plain JavaScript, no packages: `src/worker.js` and `src/drive.js`. Tests run
on Node 24: `node --test "cloud/test/*.test.mjs"` (from the repo root).
`cloud/test/parity` holds two logs and what the Python code makes of them;
both test suites check against it, so the port can't drift. After changing
a summary or the series on purpose: `python tests/test_cloud_parity.py`.

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
| `PUT /api/files/<path>` | store a log (CSV body); replies with its size and SHA-256. Under `logger/`, a drive log also gets its series and drive row, and the reply says so: `processed`, `channels`, `approx_time` (no Clock), `drive` (start, minutes, km), or `processed: false` and a `reason`. A `logger/…faults.csv` goes into fault history instead: `fault_reads` applied, `modules`, `present`, `stored` |
| `GET /api/files` | list stored files: key, size, SHA-256, upload time |
| `GET /api/files/<path>` | the file back |
| `PUT /api/history` | replace one installation's summaries: `{"source": "<PC name>", "drives": [...], "faults": [...]}`. Only that source's drives and faults are replaced; other PCs', the board's (`logger/`) and those from apps that send no source (kept as `legacy`) stay. History lists a log once and each fault as the installation that saw it last has it |
| `PUT /api/series/<path>` | replace one drive's values: `{"start", "approx", "chunks": [{"name", "unit", "t0", "t1", "n", "min", "max", "mean", "data"}]}` |
| `GET /api/series` | channels; `?channel=X` its series (`start`, `end`, `points`), `&by_drive=1` per-drive stats |
| `GET /api/templates` | the templates, deleted ones marked |
| `PUT /api/templates` | apply changes: `{"changes": [{"name": ..., "channels": [...] or null}]}` |
| `POST /api/logger/heartbeat` | the logger board's check-in, at power-on (`"event": "boot"`) and every 10 s while it has Wi-Fi: a JSON object (`boot`, `reset`, `state`, `speed_kmh`, `rpm`, `battery_v`, `pending`, ...) of at most 4 KB, kept 30 days |
| `GET /api/logger` | whether the board is on (a check-in in the last 30 s), its last check-in, and each power-on in the last `?hours=` (default 24) |
| `GET /logger` | the logger channel page: every channel the board can decode, what the car answers, a tier for each, presets |
| `PUT /api/logger/catalog` | the app's channel catalog (`n63read loggerchannels.catalog_payload`): `{"columns", "rows", "answers", "modules", "defaults"}`. Answers merge with the stored ones (an identifier that answered "ok" stays "ok"); the first one makes the preset "Default" (`defaults`, everything else slow) and makes it active |
| `GET /api/logger/catalog` | the stored catalog |
| `GET /api/logger/presets` | `{"active", "presets": [{"name", "rest", "tiers", "updated"}], "file": {"preset", "etag", "counts"}}`. `tiers` is `{channel: tier}` (fast, medium, slow, drive, daily, weekly, monthly, off); `rest` ("slow" or "off") is the tier of every channel not named that the car answers |
| `PUT /api/logger/presets` | `{"changes": [{"name", "rest", "tiers"} or {"name", "deleted": true}], "active": name}`, both optional; the active preset can't be deleted |
| `GET /api/logger/channels` | the board's `channels.csv` from the active preset, with an `ETag` (`If-None-Match` gets a 304) and `X-Preset` |
| `POST /mcp` | MCP (JSON-RPC 2.0, streamable HTTP with JSON replies) |
| `GET /api/status` | who you're signed in as |
