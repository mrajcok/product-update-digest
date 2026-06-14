# Full Installation Guide

Complete, ordered steps to install product-update-digest on a VPS alongside a Hermes
system account, with Hermes exposing semantic search and RAG via Discord.

---

## Prerequisites

- Ubuntu 24.04 VPS
- Hermes already installed as a system account (`hermes` user, `/usr/sbin/nologin`)
- Hermes gateway running with Discord connected (`DISCORD_BOT_TOKEN` set in
  `/home/hermes/.hermes/.env`)
- `sudo` access from your regular user account

---

## Security Architecture

```
/home/$USER/product-update-digest/   mode 700  $USER:$USER
    Source code, .env (API keys, GitHub token)
    Hermes has NO access — not even directory listing

/opt/digest/                         mode 2775  root:digest  (setgid)
    product_updates.db    — sqlite-vec database
    digest_mcp.py         — standalone MCP server
    venv/                 — Python 3.12 venv for MCP server (uses system Python)
    last_run.log          — overwritten on each cron run
```

A dedicated `digest` group bridges the two users: `$USER` (runs the cron scraper) and
`hermes` (runs the MCP server). The setgid bit on `/opt/digest/` ensures files created
by the cron job automatically inherit group `digest`.

Using `/opt/digest/` rather than `~/digest-data/` avoids a subtle trap: a venv created
with `uv venv` uses a Python binary symlinked into `~/.local/`, which the `hermes` user
cannot traverse because `/home/$USER/` is mode `750`. System Python at
`/usr/bin/python3.12` has no such restriction.

---

## Step 1 — Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Add to `~/.bashrc` or `~/.profile` if not already added by the installer:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Verify:

```bash
uv --version
```

---

## Step 2 — Create the digest group and data directory

```bash
sudo groupadd digest
sudo usermod -aG digest $USER
sudo usermod -aG digest hermes

sudo mkdir -p /opt/digest
sudo chown root:digest /opt/digest
sudo chmod 2775 /opt/digest
```

> **Note:** The new group membership takes effect in your next login session. For the
> current session, prefix commands that write to `/opt/digest/` with `sudo` (or use
> `newgrp digest`).

---

## Step 3 — Clone the repo and lock it down

```bash
git clone https://github.com/mrajcok/product-update-digest.git ~/product-update-digest
chmod 700 ~/product-update-digest
cd ~/product-update-digest
cp .env.example .env
chmod 600 .env
```

---

## Step 4 — Fill in .env

Edit `~/product-update-digest/.env`. Required values:

```
SQLITE_DB_PATH=/opt/digest/product_updates.db
OPENROUTER_API_KEY=<your OpenRouter key>
GITHUB_TOKEN=<GitHub PAT with repo + pages write scope>
GITHUB_REPO=<your-github-username>/product-update-digest
```

All other values can remain at their defaults from `.env.example`.

---

## Step 5 — Install project dependencies

```bash
cd ~/product-update-digest
make sync
```

This creates `.venv` inside the project directory (inside the mode 700 tree — hermes
cannot access it).

---

## Step 6 — Create the MCP server venv

The MCP server runs as the hermes user and needs its own venv in `/opt/digest/`.
Use system Python 3.12 (not uv's managed Python) so the binary path doesn't pass
through `/home/$USER/`.

```bash
sudo apt-get install -y python3.12-venv   # if not already installed
sudo python3.12 -m venv /opt/digest/venv
sudo /opt/digest/venv/bin/pip install --quiet sqlite-vec httpx mcp
sudo chown -R root:digest /opt/digest/venv
sudo chmod -R g+rX /opt/digest/venv
```

Verify the python symlink resolves to system Python (not `~/.local`):

```bash
ls -la /opt/digest/venv/bin/python*
# expected: -> /usr/bin/python3.12
```

---

## Step 7 — Deploy the MCP server script

The source lives at `src/hermes/digest_mcp.py` in the repository. Deploy it to
`/opt/digest/`:

```bash
cd ~/product-update-digest
make deploy-mcp
```

Re-run `make deploy-mcp` after any `git pull` that updates `src/hermes/digest_mcp.py`.

---

## Step 8 — Register the MCP server with Hermes

Add the following block to the top of `/home/hermes/.hermes/config.yaml`:

```yaml
mcp_servers:
  digest-search:
    command: /opt/digest/venv/bin/python
    args: [/opt/digest/digest_mcp.py]
    env:
      DIGEST_DB_PATH: /opt/digest/product_updates.db
      OPENROUTER_EMBEDDING_MODEL: qwen/qwen3-embedding-8b
      EMBEDDING_DIMENSIONS: "4096"
      SEARCH_SCORE_THRESHOLD: "0.10"
      OPENROUTER_API_KEY: "<your OpenRouter key>"
```

Replace `<your OpenRouter key>` with the same key used in your `.env`.
Hermes does not automatically forward its own `.env` to MCP subprocesses, so the
key must be listed explicitly here.

---

## Step 9 — Set up the daily cron job

```bash
crontab -e
```

Add (replacing `$HOME` with your actual home directory path, e.g. `/home/yourname`):

```
0 6 * * * cd $HOME/product-update-digest && $HOME/.local/bin/uv run digest > /opt/digest/last_run.log 2>&1
```

Uses the full path to `uv` to avoid PATH issues in cron's minimal environment.
Output overwrites `last_run.log` on each run.

---

## Step 10 — GitHub Pages — First-Time Setup

The digest pipeline pushes the generated HTML to a `gh-pages` branch. Create it once
as an orphan branch:

```bash
git clone https://<token>@github.com/<your-repo>.git /tmp/pages-init
cd /tmp/pages-init
git checkout --orphan gh-pages
git rm -rf .
echo "<h1>Coming soon</h1>" > index.html
git add index.html
git commit -m "Init gh-pages"
git push origin gh-pages
rm -rf /tmp/pages-init
```

Then enable GitHub Pages in the repo settings → **Pages** → Source: `gh-pages` branch,
`/ (root)`.

---

## Step 11 — Run the pipeline once manually

Populates the database before hermes can search it:

```bash
cd ~/product-update-digest
uv run digest --site cribl    # one vendor, full pipeline — quick sanity check
uv run digest                  # full run
```

Verify the database was created with correct permissions:

```bash
ls -la /opt/digest/product_updates.db
# expected: -rw-rw-r-- root digest ...
```

---

## Step 12 — Restart the Hermes gateway

```bash
sudo systemctl restart hermes-gateway
sudo systemctl status hermes-gateway
```

---

## Step 13 — Set Discord bot nickname

In the Discord server → Members → find the bot → Edit Nickname → set to `hai`.

Users can then invoke it with `@hai`. No code change required.

---

## Verification Checklist

```bash
# 1. DB permissions correct
ls -la /opt/digest/product_updates.db
#    expected: -rw-rw-r-- root digest ...

# 2. Hermes cannot read project source
sudo -u hermes ls ~/product-update-digest/
#    expected: Permission denied

# 3. MCP server starts without errors as hermes user
sudo -u hermes /opt/digest/venv/bin/python /opt/digest/digest_mcp.py
#    expected: blocks on stdin waiting for MCP JSON-RPC (no errors)
#    Ctrl-C to exit

# 4. Hermes discovers MCP tools
#    In hermes TUI: ask "what tools do you have?"
#    digest-search tools (semantic_search, rag_query) should appear

# 5. Semantic search via Discord
#    @hai what's new with Cribl?

# 6. RAG via Discord
#    @hai does Cribl support HIPAA compliance?

# 7. Cron log after 6 am
cat /opt/digest/last_run.log
```

---

## Logs

Two log destinations:

- **`/opt/digest/last_run.log`** — overwritten on each cron run (stdout + stderr)
- **`logs/agent.log`** inside the project directory — rotating file written by `setup_logging()` (5 MB max, 3 backups); persists across runs

```bash
tail -f /opt/digest/last_run.log
tail -f ~/product-update-digest/logs/agent.log
```

---

## Updating

```bash
cd ~/product-update-digest
git pull
make sync          # reinstall deps if pyproject.toml/uv.lock changed
make deploy-mcp    # redeploy MCP server if src/hermes/digest_mcp.py changed
```

No database migrations needed — `CREATE TABLE IF NOT EXISTS` is idempotent, and schema
column renames are handled automatically on first run.
