# Telegram MCP: instructions for Gemini CLI

Read [AGENTS.md](AGENTS.md) for the shared repository and public-search rules.
When the user gives this repository URL and explicitly asks to install or upgrade,
follow [INSTALL.md](INSTALL.md). It detects the platform, downloads the published
release, verifies its SHA-256, preserves the login and other client settings, and
checks the connection. On macOS, select `--clients gemini` for this client; Codex
is not required. Gemini web/mobile and a Windows Gemini installer are not covered.

The installation request authorizes routine setup; do not ask for the same approval
again. First Telegram authorization and all secrets stay in the owner's local
Terminal, outside the AI chat. Do not install into a personal profile while only
reviewing or developing this repository.

For normal use, search accessible history/subscriptions first. Before any new
public query, check `telegram_get_public_search_quota`, explain the actual remaining
free attempts and possible cost, ask the user and wait. Only then use its
`confirmation_token` with `user_confirmed=true`. Never offer or use paid Stars
search. The complete consent, cursor and development rules remain in AGENTS.md;
MCP initialization instructions also carry this workflow to connected clients.
