# Repository instructions

## Installing for a user

When the user explicitly asks to install or upgrade Telegram MCP from this
repository, follow [INSTALL.md](INSTALL.md). Read that guide
before changing their installation. It covers existing archives, local login, and
daily updates. A repository URL alone does not authorize installation.

## Using public search

Search the account's accessible history/subscriptions first with
`telegram_search_messages` or `telegram_search_chat_messages`. Public channel
search is a separate, optional expansion beyond subscriptions. Before every new
public query:

1. Call `telegram_get_public_search_quota(query)`. This checks the account's
   actual limits without running a search or consuming a search attempt. Never
   assume a fixed daily allowance such as ten searches.
2. Explain the broader scope, the exact query, remaining free attempts, any wait,
   and whether this request would consume a free attempt or is already free for
   this query. Offer the expansion and ask for explicit permission; wait for the
   user's answer before searching.
3. Only after approval, call `telegram_search_public_posts` with the returned
   `confirmation_token` and `user_confirmed=true`. If approval expires or the
   quota changes, check again, explain the current limits and ask again.

Do not automatically switch to public search after poor account-history results,
reformulate a public query, or treat a broad research request as this confirmation.
A returned cursor continues only the approved query for free; it needs no new
permission. A new or changed query needs a fresh check and confirmation.
The token expires after five minutes and is single-use, bound to the account,
query and quota snapshot. It enforces this sequence, but `user_confirmed` is the
agent's report of conversational consent, not proof that the server observed the
human response. This workflow is also sent in MCP initialization instructions,
so it applies to clients that do not load this repository's `AGENTS.md`.

Never offer or attempt payment with Stars, a Stars purchase, or a paid retry.
Native public search always uses `star_count=0`; any reported price is information
only. An unavailable quota is not a successful search with zero matches.

## Developing the repository

Keep workstation installation separate from source development. Use temporary
installation directories and explicit temporary client configurations for tests.
Do not change the developer's real Telegram profile, Keychain, client settings, or
LaunchAgents while implementing repository changes.

- Preserve the original four read tools and their limits; current discovery is
  15 tools by default (including public quota checks, consented free public posts search, navigation, downloads and voice), 19 with sending.
- Never add credentials, session databases, runtime data, or installed environments.
- Keep source archives allowlisted and Python dependencies locked in uv.lock.
- Keep installed versions separate; resolve current before starting Python.
- An update must not restore a client registration the owner removed or edited.
- Migrate only known compatible local profiles, without copying databases or secrets.
- Run `uv run --frozen pytest` and `uv run --frozen python -I scripts/release.py audit`.
- For installer changes, build the source ZIP and run `scripts/smoke-install-macos.py`
  on macOS. That smoke check uses temporary settings and never signs in.
- Keep README, CHANGELOG and VERIFICATION current with the package version.
  CI publishes a new GitHub Release only after tests and archive checks succeed;
  published tags/assets are historical and must not be replaced with new code.

See [Codex AGENTS.md documentation](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
for how repository instructions are loaded.
