"""A10.c.01 hosting and deploy connectors. The hosts' APIs are fakes shaped like the real ones (httpx.MockTransport);
the vault is the real Windows Credential Manager (a separate test service, cleaned up); the approval runs through a
real bot turn against the mock model. The live deploy to a real host is A10.99, with the user's own token."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from mock_provider import MockProviders, sse
from test_a7_orchestrator import build, call

import omnibots.runtime.hosting as hosting
from omnibots.bots.profile import DEFAULT_TOOLS
from omnibots.runtime.core_tools import core_registry
from omnibots.runtime.tools import ToolContext
from omnibots.security.vault import Vault

SERVICE = "omnibots-vault-test-a10c"
TOKEN = "nfp_PLANTED-netlify-token-123456"


def run(coro):
    return asyncio.run(coro)


def site(folder: Path, *, index: bool = True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if index:
        (folder / "index.html").write_text("<html><title>Todo</title><body>hello</body></html>", encoding="utf-8")
    (folder / "app.js").write_text("console.log(1)\n", encoding="utf-8")
    (folder / "__pycache__").mkdir(exist_ok=True)
    (folder / "__pycache__" / "x.pyc").write_bytes(b"junk")
    return folder


class FakeHosts:
    """Netlify, Vercel and GitHub, the calls deploy_site makes, with what the real APIs answer."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.auth: set[str] = set()
        self.netlify_sites: dict[str, dict] = {}
        self.uploaded: dict[str, bytes] = {}
        self.gh = {"repo": False, "ref": None, "pages": False, "trees": []}
        self.polls = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        self.calls.append((m, f"{req.url.host}{p}"))
        self.auth.add(req.headers.get("authorization", ""))
        host = req.url.host
        if host == "api.netlify.com":
            if m == "GET" and p.startswith("/api/v1/sites/"):
                ref = p.rsplit("/", 1)[1]
                s = next((s for s in self.netlify_sites.values() if ref in (s["id"], f"{s['name']}.netlify.app")), None)
                return httpx.Response(200, json=s) if s else httpx.Response(404, json={"message": "Not Found"})
            if m == "POST" and p == "/api/v1/sites":
                name = json.loads(req.content or b"{}").get("name") or "brave-otter-123"
                s = {"id": f"site-{len(self.netlify_sites) + 1}", "name": name, "ssl_url": f"https://{name}.netlify.app",
                     "admin_url": f"https://app.netlify.com/sites/{name}"}
                self.netlify_sites[s["id"]] = s
                return httpx.Response(201, json=s)
            if m == "POST" and p.endswith("/deploys"):
                assert req.headers["content-type"] == "application/zip"
                self.uploaded = {n: zipfile.ZipFile(io.BytesIO(req.content)).read(n) for n in zipfile.ZipFile(io.BytesIO(req.content)).namelist()}
                return httpx.Response(200, json={"id": "dep-1", "state": "uploaded"})
            if m == "GET" and p == "/api/v1/deploys/dep-1":
                self.polls += 1
                return httpx.Response(200, json={"id": "dep-1", "state": "ready" if self.polls >= 2 else "processing"})
        if host == "api.vercel.com":
            if m == "POST" and p == "/v13/deployments":
                body = json.loads(req.content)
                self.uploaded = {f["file"]: base64.b64decode(f["data"]) for f in body["files"]}
                return httpx.Response(200, json={"id": "dpl_1", "url": f"{body['name']}-abc.vercel.app", "readyState": "BUILDING"})
            if m == "GET" and p == "/v13/deployments/dpl_1":
                return httpx.Response(200, json={"id": "dpl_1", "url": "my-site-abc.vercel.app", "readyState": "READY",
                                                 "alias": ["my-site.vercel.app"]})
        if host == "api.github.com":
            if m == "GET" and p == "/repos/me/site":
                return httpx.Response(200, json={"name": "site"}) if self.gh["repo"] else httpx.Response(404, json={"message": "Not Found"})
            if m == "POST" and p == "/user/repos":
                assert json.loads(req.content)["auto_init"] is True
                self.gh["repo"] = True
                return httpx.Response(201, json={"name": "site"})
            if m == "POST" and p.endswith("/git/blobs"):
                data = base64.b64decode(json.loads(req.content)["content"])
                sha = f"blob{len(self.uploaded)}"
                self.uploaded[sha] = data
                return httpx.Response(201, json={"sha": sha})
            if m == "POST" and p.endswith("/git/trees"):
                self.gh["trees"].append(json.loads(req.content)["tree"])
                return httpx.Response(201, json={"sha": "tree1"})
            if m == "GET" and p.endswith("/git/ref/heads/gh-pages"):
                return httpx.Response(200, json={"object": {"sha": self.gh["ref"]}}) if self.gh["ref"] else httpx.Response(404, json={})
            if m == "POST" and p.endswith("/git/commits"):
                return httpx.Response(201, json={"sha": "commit1234567890"})
            if m in ("POST", "PATCH") and "/git/refs" in p:
                self.gh["ref"] = json.loads(req.content)["sha"]
                return httpx.Response(200, json={})
            if p.endswith("/pages"):
                if m == "GET":
                    return httpx.Response(200, json={"html_url": "https://me.github.io/site/"}) if self.gh["pages"] else httpx.Response(404, json={})
                self.gh["pages"] = True
                return httpx.Response(201, json={"html_url": "https://me.github.io/site/"})
        return httpx.Response(500, json={"message": f"fake has no {m} {host}{p}"})


@pytest.fixture
def fake(monkeypatch):
    f = FakeHosts()
    monkeypatch.setattr(hosting, "client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(f)))

    async def no_sleep(_):
        return None
    monkeypatch.setattr(hosting, "sleep", no_sleep)
    checked: list[str] = []

    async def live(url, tries=6, wait=5):
        checked.append(url)
        return "200 OK, title 'Todo'"
    monkeypatch.setattr(hosting, "live_check", live)
    f.checked = checked
    return f


def ctx_for(ws: Path) -> ToolContext:
    return ToolContext(bot_id="bot_x", workspace=ws)


def test_netlify_creates_the_site_uploads_a_zip_and_checks_the_url(tmp_path, fake):
    ws = tmp_path / "proj"
    site(ws / "public")
    (ws / "GOAL.md").write_text("goal", encoding="utf-8")

    async def go():
        t = hosting.hosting_tools()[0]
        first = await t.fn({"host": "netlify", "folder": "public", "site": "my-todo", "token": TOKEN}, ctx_for(ws))
        again = await t.fn({"host": "netlify", "folder": "public", "site": "my-todo", "token": TOKEN}, ctx_for(ws))
        return first, again
    first, again = run(go())
    assert "deployed to netlify: https://my-todo.netlify.app" in first and "(created now)" in first
    assert "live check: 200 OK" in first and fake.checked[0] == "https://my-todo.netlify.app"
    assert set(fake.uploaded) == {"index.html", "app.js"}                   # no __pycache__, no GOAL.md
    assert "(created now)" not in again and len(fake.netlify_sites) == 1   # the second deploy reuses the site
    assert fake.auth == {f"Bearer {TOKEN}"}


def test_vercel_and_github_pages(tmp_path, fake):
    ws = site(tmp_path / "proj")

    async def go():
        t = hosting.hosting_tools()[0]
        v = await t.fn({"host": "vercel", "site": "My Site", "token": TOKEN}, ctx_for(ws))
        vercel_files = set(fake.uploaded)
        fake.uploaded = {}
        g1 = await t.fn({"host": "github_pages", "site": "me/site", "token": TOKEN}, ctx_for(ws))
        g2 = await t.fn({"host": "github_pages", "site": "me/site", "token": TOKEN}, ctx_for(ws))
        bad = await t.fn({"host": "github_pages", "site": "not a repo", "token": TOKEN}, ctx_for(ws))
        return v, vercel_files, g1, g2, bad
    v, vercel_files, g1, g2, bad = run(go())
    assert "deployed to vercel: https://my-site.vercel.app" in v and vercel_files == {"index.html", "app.js"}
    assert "deployed to github_pages: https://me.github.io/site/" in g1 and "(created now)" in g1
    assert {e["path"] for e in fake.gh["trees"][0]} == {"index.html", "app.js", ".nojekyll"}
    assert ("PATCH", "api.github.com/repos/me/site/git/refs/heads/gh-pages") in fake.calls   # the 2nd deploy updates the branch
    assert "(created now)" not in g2 and bad.startswith("ERROR: github_pages needs site='owner/repo'")


def test_what_is_refused_before_anything_is_sent(tmp_path, fake):
    ws = tmp_path / "proj"
    site(ws / "noindex", index=False)
    site(tmp_path / "elsewhere")

    async def go():
        t = hosting.hosting_tools()[0]
        return [await t.fn(a, ctx_for(ws)) for a in (
            {"host": "netlify", "folder": "noindex", "token": TOKEN},
            {"host": "netlify", "folder": "../elsewhere", "token": TOKEN},
            {"host": "netlify", "folder": "nothing-here", "token": TOKEN},
            {"host": "netlify", "folder": "noindex", "token": "{{secret:netlify_token}}"},    # not substituted = no vault
            {"host": "heroku", "token": TOKEN})]
    outs = run(go())
    assert "no index.html" in outs[0] and "outside your project folder" in outs[1] and "no folder" in outs[2]
    assert "vault handle" in outs[3] and "host must be one of" in outs[4]
    assert fake.calls == []


def test_the_vault_sends_the_token_only_to_that_hosts_api(tmp_path, fake):
    ws = site(tmp_path / "proj")

    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path / "home", mock)
            v = Vault(e["db"], service=SERVICE)
            try:
                await v.set("netlify_token", TOKEN, hosts=["api.netlify.com"])
                reg = core_registry()
                reg.vault = v
                ok = await reg.run("deploy_site", {"host": "netlify", "site": "my-todo", "token": "{{secret:netlify_token}}"}, ctx_for(ws))
                wrong = await reg.run("deploy_site", {"host": "vercel", "site": "x", "token": "{{secret:netlify_token}}"}, ctx_for(ws))
                risk = reg.get("deploy_site").risk_for({"host": "netlify", "token": "{{secret:netlify_token}}"}, ctx_for(ws))
            finally:
                await v.delete("netlify_token")
                await e["db"].close()
            return ok, wrong, risk
    ok, wrong, risk = run(go())
    assert "deployed to netlify" in ok and TOKEN not in ok and risk == "R3"
    assert "may only be sent to api.netlify.com" in wrong
    assert fake.auth == {f"Bearer {TOKEN}"} and not any("vercel" in h for _, h in fake.calls)


def test_a_bot_deploys_with_one_approval_on_a_card_that_lists_the_files(tmp_path, fake):
    async def go():
        with MockProviders() as mock:
            e = await build(tmp_path / "home", mock)
            v = Vault(e["db"], service=SERVICE)
            await v.set("netlify_token", TOKEN, hosts=["api.netlify.com"])
            e["runner"].vault = v
            try:
                pid = await e["projects"].create("host the to-do app")
                site(e["projects"].folder(pid))
                bot = await e["reg"].create("Shipper", "devops", chain=["work/m"], tools=[*DEFAULT_TOOLS, "deploy_site"], risk_ceiling="R3")
                mock.script("work", sse("", tool_calls=[call("deploy_site", host="netlify", site="my-todo", token="{{secret:netlify_token}}")]),
                            sse("Live at https://my-todo.netlify.app"))
                cards = []

                async def approver():
                    while True:
                        for info in e["approvals"].list_pending():
                            cards.append(dict(info))
                            await e["approvals"].decide(info["id"], True, "test")
                        await asyncio.sleep(0.02)
                ap = asyncio.create_task(approver())
                out = await e["runner"].run(bot.id, "deploy the site", project_id=pid, workspace=e["projects"].folder(pid))
                ap.cancel()
                tool_msgs = [m["content"] for m in mock.requests[-1]["body"]["messages"] if m.get("role") == "tool"]
            finally:
                await v.delete("netlify_token")
                await e["db"].close()
            return out, cards, tool_msgs
    out, cards, tool_msgs = run(go())
    assert out.status == "completed" and len(cards) == 1
    card = json.dumps(cards[0], default=str)
    assert "R3" in card and "index.html" in card and "api.netlify.com" in card and TOKEN not in card
    assert "deployed to netlify: https://my-todo.netlify.app" in tool_msgs[0] and TOKEN not in tool_msgs[0]


def test_cloudflare_runs_wrangler_with_the_token_in_its_environment_only(tmp_path, fake, monkeypatch):
    ws = site(tmp_path / "proj")
    ran = []

    async def fake_wrangler(args, token, account, cwd, timeout=300):
        ran.append((args, token, account, Path(cwd)))
        if args[:3] == ["pages", "project", "create"]:
            return 1, "A project with this name already exists"
        return 0, "✨ Deployment complete! Take a peek over at https://1a2b3c.my-todo.pages.dev"
    monkeypatch.setattr(hosting, "_wrangler", fake_wrangler)

    async def go():
        t = hosting.hosting_tools()[0]
        no_acct = await t.fn({"host": "cloudflare", "site": "my-todo", "token": TOKEN}, ctx_for(ws))
        ok = await t.fn({"host": "cloudflare", "site": "my-todo", "token": TOKEN, "account_id": "acc1"}, ctx_for(ws))
        return no_acct, ok
    no_acct, ok = run(go())
    assert "needs account_id" in no_acct
    assert "deployed to cloudflare: https://my-todo.pages.dev" in ok and "https://1a2b3c.my-todo.pages.dev" in ok
    assert all(a[1] == TOKEN and a[2] == "acc1" and a[3] == ws.resolve() for a in ran)
    assert all(TOKEN not in " ".join(a[0]) for a in ran)                    # never on the command line


def test_check_domain_reads_dns_and_says_where_it_points(monkeypatch):
    answers = {("my-todo.netlify.app", "A"): [{"type": 1, "data": "18.208.88.157"}],
               ("shop.example.com", "CNAME"): [{"type": 5, "data": "my-todo.netlify.app."}],
               ("shop.example.com", "A"): [{"type": 5, "data": "my-todo.netlify.app."}, {"type": 1, "data": "75.2.60.5"}],
               ("bare.example.com", "A"): []}

    def doh(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "cloudflare-dns.com"
        return httpx.Response(200, json={"Answer": answers.get((req.url.params["name"], req.url.params["type"]), [])})
    monkeypatch.setattr(hosting, "client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(doh)))

    async def live(url, tries=6, wait=5):
        return "200 OK"
    monkeypatch.setattr(hosting, "live_check", live)

    async def go():
        t = hosting.hosting_tools()[1]
        return (await t.fn({"domain": "https://shop.example.com/cart", "expect": "my-todo.netlify.app"}, None),
                await t.fn({"domain": "shop.example.com", "expect": "me.github.io"}, None),
                await t.fn({"domain": "bare.example.com"}, None),
                await t.fn({"domain": "my-todo.netlify.app", "expect": "netlify.app"}, None),   # found live: said NO
                await t.fn({"domain": "not a domain"}, None))
    good, wrong, bare, own, bad = run(go())
    assert "expected netlify.app: yes" in own and "points at: netlify" in own
    assert "CNAME: my-todo.netlify.app" in good and "points at: netlify" in good and "expected my-todo.netlify.app: yes" in good
    assert "expected me.github.io: NO" in wrong
    assert "doesn't point anywhere" in bare and bad.startswith("ERROR")


def test_a_private_netlify_project_is_named_as_such_not_as_down(monkeypatch):
    """Found live (A10.99): the deploy worked, but the account made new projects private, so visitors got Netlify's
    login redirect (401). The bot read "not answering" and redeployed; now it's told what it is and not to redeploy."""
    import omnibots.runtime.web_tools as web_tools
    login = ('<html><title>Login Redirect</title><script>var url = new URL('
             "'https:\/\/app.netlify.com\/edge-access?domain=x.netlify.app');</script></html>")

    def page(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=login) if req.url.host == "x.netlify.app" else httpx.Response(401, text="no")
    monkeypatch.setattr(hosting, "client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(page)))

    async def public(url, allow_loopback=False):
        return None
    monkeypatch.setattr(web_tools, "assert_public_url", public)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(hosting, "sleep", no_sleep)
    private = run(hosting.live_check("https://x.netlify.app"))
    other = run(hosting.live_check("https://y.example.com", tries=2))
    assert private.startswith("PROTECTED") and "Project visibility" in private and "Don't redeploy" in private
    assert other.startswith("NOT answering yet (HTTP 401)")
