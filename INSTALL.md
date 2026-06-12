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

Two directories with separate access controls:

```
/home/$USER/product-update-digest/   mode 700  $USER:$USER
    Source code, .env (API keys, GitHub token)
    Hermes has NO access — not even directory listing

/home/$USER/digest-data/             mode 2750 $USER:hermes  (setgid)
    product_updates.db    — sqlite-vec database (hermes can read)
    digest_mcp.py         — standalone MCP server (hermes can execute)
    venv/                 — Python 3.13 venv for MCP server
    last_run.log          — overwritten on each cron run
```

The setgid bit on `digest-data/` ensures files created by your cron job (including the
database) automatically inherit group `hermes`, keeping them readable by the hermes gateway
without any manual `chown` after each run.

The hermes system account (no sudo, no login shell) can read the database and run the MCP
server but cannot access any project source, secrets, or `.env`.

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

## Step 2 — Create the data directory

```bash
mkdir -p ~/digest-data
sudo chown $USER:hermes ~/digest-data
sudo chmod 2750 ~/digest-data
```

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
SQLITE_DB_PATH=/home/$USER/digest-data/product_updates.db
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

The MCP server runs as the hermes user and must not import from the project source.
It gets its own minimal venv in the hermes-accessible data directory.

```bash
cd ~/digest-data
uv venv --python 3.13 venv
uv pip install --python venv/bin/python sqlite-vec httpx mcp
sudo chown -R $USER:hermes venv
sudo chmod -R g+rX venv
```

---

## Step 7 — Deploy the MCP server script

The source lives at `src/hermes/digest_mcp.py` in the repository. Deploy it to the
hermes-accessible data directory:

```bash
cd ~/product-update-digest
make deploy-mcp
```

This copies the script to `~/digest-data/digest_mcp.py`, sets ownership to
`$USER:hermes`, and sets mode `750`. Re-run `make deploy-mcp` after any `git pull` that
updates `src/hermes/digest_mcp.py`.

---

## Step 8 — Register the MCP server with Hermes

Add the following block to the top of `/home/hermes/.hermes/config.yaml`:

```yaml
mcp_servers:
  digest-search:
    command: /home/$USER/digest-data/venv/bin/python
    args: [/home/$USER/digest-data/digest_mcp.py]
    env:
      DIGEST_DB_PATH: /home/$USER/digest-data/product_updates.db
      OPENROUTER_EMBEDDING_MODEL: qwen/qwen3-embedding-8b
      EMBEDDING_DIMENSIONS: "4096"
      SEARCH_SCORE_THRESHOLD: "0.10"
```

Replace `$USER` with your actual username. `OPENROUTER_API_KEY` is intentionally absent —
it is already in hermes's `/home/hermes/.hermes/.env` and the MCP subprocess inherits it
automatically.

---

## Step 9 — Set up the daily cron job

```bash
crontab -e
```

Add (replacing `$HOME` with your actual home directory path, e.g. `/home/yourname`):

```
0 6 * * * cd $HOME/product-update-digest && $HOME/.local/bin/uv run digest > $HOME/digest-data/last_run.log 2>&1
```

Uses the full path to `uv` to avoid PATH issues in cron's minimal environment.
Output overwrites `last_run.log` on each run.

---

## Step 10 — Run the pipeline once manually

Populates the database before hermes can search it:

```bash
cd ~/product-update-digest
uv run digest
```

Verify the database was created with correct permissions:

```bash
ls -la ~/digest-data/product_updates.db
# expected: -rw-r----- $USER hermes ...
```

If the group is wrong, fix with:

```bash
sudo chown $USER:hermes ~/digest-data/product_updates.db
chmod 640 ~/digest-data/product_updates.db
```

---

## Step 11 — Restart the Hermes gateway

The hermes gateway runs as a system-level systemd service. Restart it to pick up the new
`mcp_servers` config:

```bash
sudo systemctl restart hermes-gateway
sudo systemctl status hermes-gateway
```

---

## Step 12 — Set Discord bot nickname

In the Discord server → Members → find the bot → Edit Nickname → set to `hai`.

Users can then invoke it with `@hai`. No code change required.

---

## Verification Checklist

```bash
# 1. DB permissions correct
ls -la ~/digest-data/product_updates.db
#    expected: -rw-r----- 1 $USER hermes ...

# 2. Hermes cannot read project source
sudo -u hermes ls ~/product-update-digest/
#    expected: Permission denied

# 3. MCP server starts without errors as hermes user
sudo -u hermes ~/digest-data/venv/bin/python ~/digest-data/digest_mcp.py
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
cat ~/digest-data/last_run.log
```
