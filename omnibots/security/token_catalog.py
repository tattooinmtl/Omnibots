"""The sites Settings → Tokens knows (ADR-7): the vault name the tools look for, the hosts the token
may go to, where the user makes one, and a harmless "who am I" call to test it.

A test sends the token only to its own `hosts` (the same rule the vault enforces for bots)
and returns who the token belongs to, never the token.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx


@dataclass(frozen=True)
class Site:
    key: str                       # vault name (tools use {{secret:<key>}})
    label: str
    hosts: tuple[str, ...]
    get_url: str = ""              # where the user creates the token
    hint: str = ""                 # what to tick when making it
    test_url: str = ""             # a GET that only reads who the token is; {token} = in the URL itself
    auth: str = "Bearer {token}"   # the Authorization header ("" = none: the token is in the URL)
    who: tuple[str, ...] = field(default=())   # JSON path to a name in the test reply
    kind: str = "token"


CATALOG: tuple[Site, ...] = (
    Site("github_token", "GitHub", ("api.github.com",),
         "https://github.com/settings/personal-access-tokens/new",
         "Fine-grained token: pick only the repos bots may touch; Contents, Pull requests and Issues "
         "read/write, Metadata read. Set an expiry.",
         "https://api.github.com/user", who=("login",)),
    Site("vercel_token", "Vercel", ("api.vercel.com",),
         "https://vercel.com/account/settings/tokens",
         "Scope it to the one team the sites go to, with an expiry.",
         "https://api.vercel.com/v2/user", who=("user", "username")),
    Site("netlify_token", "Netlify", ("api.netlify.com",),
         "https://app.netlify.com/user/applications#personal-access-tokens",
         "A personal access token, with an expiry.",
         "https://api.netlify.com/api/v1/user", who=("full_name",)),
    Site("cloudflare_token", "Cloudflare", ("api.cloudflare.com",),
         "https://dash.cloudflare.com/profile/api-tokens",
         "Use a template such as \"Edit Cloudflare Workers\" or Pages; limit it to your account and zones.",
         "https://api.cloudflare.com/client/v4/user/tokens/verify", who=("result", "status")),
    Site("gitlab_token", "GitLab", ("gitlab.com",),
         "https://gitlab.com/-/user_settings/personal_access_tokens",
         "Scopes: api (or read_api to only read), with an expiry.",
         "https://gitlab.com/api/v4/user", who=("username",)),
    Site("telegram_bot_token", "Telegram bot", ("api.telegram.org",),
         "https://t.me/BotFather",
         "Talk to @BotFather, send /newbot, copy the token it gives you.",
         "https://api.telegram.org/bot{token}/getMe", auth="", who=("result", "username")),
    Site("discord_bot_token", "Discord bot", ("discord.com",),
         "https://discord.com/developers/applications",
         "New Application → Bot → Reset Token. Invite the bot only to your own server.",
         "https://discord.com/api/v10/users/@me", auth="Bot {token}", who=("username",)),
    Site("slack_bot_token", "Slack bot", ("slack.com",),
         "https://api.slack.com/apps",
         "Create an app, add only the scopes you need, install it to your workspace, copy the xoxb- token.",
         "https://slack.com/api/auth.test", who=("team",)),
    Site("huggingface_token", "Hugging Face", ("huggingface.co",),
         "https://huggingface.co/settings/tokens",
         "A fine-grained token, read-only unless bots must upload.",
         "https://huggingface.co/api/whoami-v2", who=("name",)),
    Site("npm_token", "npm", ("registry.npmjs.org",),
         "https://www.npmjs.com/settings/~/tokens",
         "A granular access token for the packages bots publish, with an expiry.",
         "https://registry.npmjs.org/-/whoami", who=("username",)),
    Site("supabase_token", "Supabase", ("api.supabase.com",),
         "https://supabase.com/dashboard/account/tokens",
         "An access token for the Management API.",
         "https://api.supabase.com/v1/projects"),
    Site("brave_search_key", "Brave Search API", ("api.search.brave.com",),
         "https://api-dashboard.search.brave.com/app/keys",
         "The free plan is enough to start.",
         "https://api.search.brave.com/res/v1/web/search?q=test&count=1", auth="X-Subscription-Token: {token}"),
    Site("tavily_key", "Tavily search", ("api.tavily.com",),
         "https://app.tavily.com/home",
         "The free plan is enough to start."),
    Site("sftp_password", "SFTP (your own host)", (),
         "", "The password of your host's SFTP account. Type the host name below (e.g. ftp.example.com) "
         "so the password can't be sent anywhere else.", kind="password"),
)

CUSTOM = Site("", "Custom (any other site)", (), "",
              "Give it a name (letters, digits, _ . -), the host(s) it may be sent to, and the value. "
              "Bots use it as {{secret:<name>}}.")


def by_key(key: str) -> Site | None:
    return next((s for s in CATALOG if s.key == key), None)


def _dig(doc, path: tuple[str, ...]):
    for p in path:
        if not isinstance(doc, dict):
            return None
        doc = doc.get(p)
    return doc


async def check(site: Site, value: str, client: httpx.AsyncClient | None = None) -> tuple[bool, str]:
    """Ask the site who this token is. (ok, message); the message never contains the token."""
    if not site.test_url:
        return False, "no automatic test for this one"
    url = site.test_url.replace("{token}", value)
    host = (urlparse(url).hostname or "").lower()
    if site.hosts and not any(host == h or host.endswith("." + h) for h in site.hosts):
        return False, f"refused: {host} is not one of {', '.join(site.hosts)}"
    headers = {"User-Agent": "omnibots", "Accept": "application/json"}
    if site.auth:
        if site.auth.startswith("X-"):
            name, _, fmt = site.auth.partition(": ")
            headers[name] = fmt.replace("{token}", value)
        else:
            headers["Authorization"] = site.auth.replace("{token}", value)
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        r = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return False, f"couldn't reach {host}: {type(exc).__name__}"
    finally:
        if own:
            await client.aclose()
    if r.status_code in (401, 403):
        return False, f"{site.label} refused the token (HTTP {r.status_code})"
    if r.status_code >= 400:
        return False, f"{site.label} answered HTTP {r.status_code}"
    try:
        doc = r.json()
    except ValueError:
        doc = None
    if isinstance(doc, dict) and doc.get("ok") is False:          # Slack/Telegram say no with HTTP 200
        return False, f"{site.label} refused the token ({doc.get('error') or doc.get('description') or 'not ok'})"
    name = _dig(doc, site.who) if site.who else None
    text = f"{site.label} accepts it" + (f": {name}" if name else "")
    return True, text.replace(value, "[secret]")
