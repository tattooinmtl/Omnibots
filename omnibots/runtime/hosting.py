"""Hosting and deploy connectors (PLAN.md A10.c.01; ADR-9: API/CLI first).

`deploy_site` puts a folder of the bot's workspace on the web:

  netlify       zip upload to the Netlify API (the site is created when it doesn't exist)
  vercel        a static deployment through the Vercel API (files inline, production target)
  cloudflare    Cloudflare Pages through the `wrangler` CLI (Node; the project is created when missing)
  github_pages  the folder becomes the `gh-pages` branch of a GitHub repository (git data API), Pages turned on
  sftp          the user's own host: files copied over SFTP (paramiko), then the public URL checked

The token is a vault handle (`{{secret:netlify_token}}`): the tool layer substitutes it only for that host's API
(a secret scoped to api.netlify.com can't go anywhere else), and using it makes the call R3, so the user approves
each deploy on a card that lists the files and the target (the rehearsal). After a deploy the live URL is fetched,
so the answer says whether the site really answers.

`check_domain` looks a domain up (DNS over HTTPS: A, AAAA, CNAME, NS) and fetches it over HTTPS, and says which host
it points at, and whether that's the one you expected.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Callable

import httpx

from omnibots.runtime.tools import Tool, ToolContext

HOSTS = ("netlify", "vercel", "cloudflare", "github_pages", "sftp")
API = {"netlify": "https://api.netlify.com", "vercel": "https://api.vercel.com",
       "cloudflare": "https://api.cloudflare.com", "github_pages": "https://api.github.com"}
TOKEN_NAME = {"netlify": "netlify_token", "vercel": "vercel_token", "cloudflare": "cloudflare_token",
              "github_pages": "github_token", "sftp": "sftp_password"}
SKIP_DIRS = {".git", "__pycache__", ".tmp", "node_modules", ".venv"}
SKIP_ROOT = {"GOAL.md", "REPORT.md"}                 # OmniBots' own notes, not part of a site
MAX_FILES, MAX_BYTES = 2000, 50 * 1024 * 1024
POLL_SECONDS = 180
WRANGLER = "wrangler@4"

# tests swap these for fakes
client_factory: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(timeout=60)
sleep = asyncio.sleep


class DeployError(RuntimeError):
    pass


# ── the files ─────────────────────────────────────────────────────────
def site_files(workspace: Path, folder: str) -> tuple[Path, list[tuple[str, Path]]]:
    ws = workspace.resolve()
    root = (ws / (folder or ".")).resolve()
    if not root.is_relative_to(ws):
        raise DeployError(f"{folder!r} is outside your project folder")
    if not root.is_dir():
        raise DeployError(f"no folder {folder!r} in your project")
    out: list[tuple[str, Path]] = []
    total = 0
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if not p.is_file() or any(part in SKIP_DIRS for part in rel.parts) or p.suffix in (".pyc", ".pyo"):
            continue
        if root == ws and len(rel.parts) == 1 and rel.name in SKIP_ROOT:
            continue
        total += p.stat().st_size
        out.append((rel.as_posix(), p))
        if len(out) > MAX_FILES or total > MAX_BYTES:
            raise DeployError(f"too big for a static deploy (over {MAX_FILES} files or {MAX_BYTES // 2**20} MB)")
    if not out:
        raise DeployError(f"{folder or '.'} has no files to deploy")
    if not any(rel == "index.html" for rel, _ in out):
        raise DeployError(f"{folder or '.'} has no index.html at its top: a static host would show nothing")
    return root, out


def _zip(files: list[tuple[str, Path]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, p in files:
            z.write(p, rel)
    return buf.getvalue()


def _check(r: httpx.Response, what: str) -> dict[str, Any]:
    if r.status_code >= 400:
        try:
            detail = r.json()
            detail = detail.get("message") or detail.get("error") or detail
        except ValueError:
            detail = r.text[:300]
        if isinstance(detail, dict):
            detail = detail.get("message") or json.dumps(detail)[:300]
        hint = " (check the token in the vault)" if r.status_code in (401, 403) else ""
        raise DeployError(f"{what}: HTTP {r.status_code}: {detail}{hint}")
    try:
        return r.json() if r.content else {}
    except ValueError:
        return {}


# ── Netlify ───────────────────────────────────────────────────────────
async def deploy_netlify(c: httpx.AsyncClient, token: str, site: str, files: list[tuple[str, Path]]) -> dict[str, Any]:
    base, h = API["netlify"] + "/api/v1", {"Authorization": f"Bearer {token}"}
    info = None
    if site:
        ref = site if ("." in site or re.fullmatch(r"[0-9a-f-]{36}", site)) else f"{site}.netlify.app"
        r = await c.get(f"{base}/sites/{ref}", headers=h)
        if r.status_code != 404:
            info = _check(r, "Netlify site lookup")
    created = info is None
    if created:
        name = site.split(".")[0] if site else None
        info = _check(await c.post(f"{base}/sites", headers=h, json={"name": name} if name else {}), "creating the Netlify site")
    d = _check(await c.post(f"{base}/sites/{info['id']}/deploys", headers={**h, "Content-Type": "application/zip"},
                            content=_zip(files)), "uploading to Netlify")
    for _ in range(POLL_SECONDS // 3):
        if d.get("state") in ("ready", "error"):
            break
        await sleep(3)
        d = _check(await c.get(f"{base}/deploys/{d['id']}", headers=h), "Netlify deploy status")
    if d.get("state") != "ready":
        raise DeployError(f"Netlify deploy {d.get('id')} ended {d.get('state')}: {d.get('error_message') or 'not ready in time'}")
    url = info.get("ssl_url") or d.get("ssl_url") or info.get("url") or d.get("deploy_ssl_url")
    return {"url": url, "deploy_id": d["id"], "site": info.get("name") or info["id"], "created": created,
            "admin": info.get("admin_url")}


# ── Vercel ────────────────────────────────────────────────────────────
async def deploy_vercel(c: httpx.AsyncClient, token: str, site: str, files: list[tuple[str, Path]]) -> dict[str, Any]:
    base, h = API["vercel"], {"Authorization": f"Bearer {token}"}
    name = re.sub(r"[^a-z0-9-]", "-", (site or "omnibots-site").lower()).strip("-")[:100] or "omnibots-site"
    body = {"name": name, "target": "production", "projectSettings": {"framework": None},
            "files": [{"file": rel, "data": base64.b64encode(p.read_bytes()).decode(), "encoding": "base64"} for rel, p in files]}
    d = _check(await c.post(f"{base}/v13/deployments?skipAutoDetectionConfirmation=1", headers=h, json=body), "creating the Vercel deployment")
    for _ in range(POLL_SECONDS // 3):
        if d.get("readyState") in ("READY", "ERROR", "CANCELED"):
            break
        await sleep(3)
        d = _check(await c.get(f"{base}/v13/deployments/{d['id']}", headers=h), "Vercel deployment status")
    if d.get("readyState") != "READY":
        raise DeployError(f"Vercel deployment {d.get('id')} ended {d.get('readyState')}: {d.get('errorMessage') or 'not ready in time'}")
    alias = (d.get("alias") or [None])[0]
    return {"url": "https://" + (alias or d["url"]), "deploy_id": d["id"], "site": name, "created": None}


# ── GitHub Pages ──────────────────────────────────────────────────────
async def deploy_github_pages(c: httpx.AsyncClient, token: str, site: str, files: list[tuple[str, Path]]) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", site or ""):
        raise DeployError("github_pages needs site='owner/repo'")
    base = API["github_pages"]
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    repo = f"{base}/repos/{site}"
    r = await c.get(repo, headers=h)
    created = r.status_code == 404
    if created:                                            # auto_init: the git data API can't write to an empty repo
        _check(await c.post(f"{base}/user/repos", headers=h, json={"name": site.split("/")[1], "auto_init": True,
                                                                     "description": "Deployed by OmniBots"}), "creating the repository")
    else:
        _check(r, "GitHub repository lookup")
    tree = []
    for rel, p in [*files, (".nojekyll", None)]:           # .nojekyll: serve files as they are (folders starting with _)
        data = base64.b64encode(p.read_bytes() if p else b"").decode()
        blob = _check(await c.post(f"{repo}/git/blobs", headers=h, json={"content": data, "encoding": "base64"}), f"uploading {rel}")
        tree.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    t = _check(await c.post(f"{repo}/git/trees", headers=h, json={"tree": tree}), "writing the tree")
    ref = await c.get(f"{repo}/git/ref/heads/gh-pages", headers=h)
    parent = [_check(ref, "reading gh-pages")["object"]["sha"]] if ref.status_code != 404 else []
    commit = _check(await c.post(f"{repo}/git/commits", headers=h,
                                 json={"message": "Deploy by OmniBots", "tree": t["sha"], "parents": parent}), "committing")
    if parent:
        _check(await c.patch(f"{repo}/git/refs/heads/gh-pages", headers=h, json={"sha": commit["sha"], "force": True}), "updating gh-pages")
    else:
        _check(await c.post(f"{repo}/git/refs", headers=h, json={"ref": "refs/heads/gh-pages", "sha": commit["sha"]}), "creating gh-pages")
    pages = await c.get(f"{repo}/pages", headers=h)
    if pages.status_code == 404:
        info = _check(await c.post(f"{repo}/pages", headers=h, json={"source": {"branch": "gh-pages", "path": "/"}}), "turning on Pages")
    else:
        info = _check(pages, "reading the Pages settings")
    owner, name = site.split("/")
    url = info.get("html_url") or f"https://{owner.lower()}.github.io/{name}/"
    return {"url": url, "deploy_id": commit["sha"][:12], "site": site, "created": created}


# ── Cloudflare Pages (CLI) ────────────────────────────────────────────
async def _wrangler(args: list[str], token: str, account: str, cwd: Path, timeout: float = 300) -> tuple[int, str]:
    import os
    import shutil
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise DeployError("cloudflare needs Node.js (npx) on this PC")
    env = {k: v for k, v in os.environ.items() if not re.search(r"KEY|TOKEN|SECRET|PASSWORD", k, re.I)}
    env.update(CLOUDFLARE_API_TOKEN=token, CLOUDFLARE_ACCOUNT_ID=account, WRANGLER_SEND_METRICS="false", CI="1")
    proc = await asyncio.create_subprocess_exec(npx, "--yes", WRANGLER, *args, cwd=str(cwd), env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise DeployError(f"wrangler took longer than {timeout:.0f}s")
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def deploy_cloudflare(token: str, account: str, site: str, root: Path) -> dict[str, Any]:
    if not account:
        raise DeployError("cloudflare needs account_id (your Cloudflare account id)")
    name = re.sub(r"[^a-z0-9-]", "-", (site or "omnibots-site").lower()).strip("-")[:58] or "omnibots-site"
    code, out = await _wrangler(["pages", "project", "create", name, "--production-branch", "main"], token, account, root)
    created = code == 0
    if code != 0 and "already exists" not in out.lower():
        raise DeployError(f"creating the Pages project failed: {out[-600:]}")
    code, out = await _wrangler(["pages", "deploy", ".", "--project-name", name, "--branch", "main", "--commit-dirty=true"],
                                token, account, root)
    if code != 0:
        raise DeployError(f"wrangler pages deploy failed (exit {code}): {out[-800:]}")
    urls = re.findall(r"https://[A-Za-z0-9.-]+\.pages\.dev", out)
    return {"url": f"https://{name}.pages.dev", "preview": urls[-1] if urls else None, "deploy_id": (urls[-1] if urls else ""),
            "site": name, "created": created}


# ── SFTP (the user's own host) ────────────────────────────────────────
def _sftp_upload(server: str, port: int, username: str, password: str, remote: str, files: list[tuple[str, Path]]) -> int:
    try:
        import paramiko
    except ImportError:
        raise DeployError("sftp needs the paramiko package (pip install paramiko)")
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())   # an unknown server is refused, never trusted blindly
    try:
        ssh.connect(server, port=port, username=username, password=password or None, timeout=30,
                    allow_agent=not password, look_for_keys=not password)
    except paramiko.SSHException as exc:
        if "not found in known_hosts" in str(exc):
            raise DeployError(f"{server} isn't a known host: connect once with `ssh {username}@{server}` to trust its key")
        raise DeployError(f"SFTP login to {server} failed: {exc}")
    try:
        sftp = ssh.open_sftp()
        made: set[str] = set()

        def mkdirs(path: str) -> None:
            parts, cur = path.strip("/").split("/"), "/" if path.startswith("/") else ""
            for part in parts:
                cur = f"{cur}{part}" if cur in ("", "/") else f"{cur}/{part}"
                if cur in made:
                    continue
                try:
                    sftp.stat(cur)
                except OSError:
                    sftp.mkdir(cur)
                made.add(cur)
        for rel, p in files:
            dest = f"{remote.rstrip('/')}/{rel}"
            mkdirs(dest.rsplit("/", 1)[0])
            sftp.put(str(p), dest)
        return len(files)
    finally:
        ssh.close()


# ── after the deploy: is it really live? ──────────────────────────────
async def live_check(url: str, tries: int = 6, wait: float = 5) -> str:
    from omnibots.runtime.web_tools import html_to_text, safe_get
    last = ""
    async with client_factory() as c:
        for i in range(tries):
            try:
                r = await safe_get(c, url)
                title = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.I | re.S)
                if r.status_code < 400:
                    words = html_to_text(r.text)[:160].replace("\n", " ")
                    return f"{r.status_code} OK" + (f", title {title.group(1).strip()[:80]!r}" if title else "") + f"; starts: {words!r}"
                last = f"HTTP {r.status_code}"
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            if i < tries - 1:
                await sleep(wait)                          # new sites take a few seconds (DNS, certificate)
    return f"NOT answering yet ({last})"


# ── the tools ─────────────────────────────────────────────────────────
async def deploy_site(args: dict[str, Any], ctx: ToolContext) -> str:
    host = str(args.get("host") or "").strip().lower()
    if host not in HOSTS:
        return f"ERROR: host must be one of {', '.join(HOSTS)}"
    token = str(args.get("token") or "")
    if not token or "{{secret:" in token:
        return f"ERROR: give token as a vault handle, e.g. {{{{secret:{TOKEN_NAME[host]}}}}} (the user adds it to the vault)"
    site = str(args.get("site") or "").strip()
    try:
        root, files = site_files(ctx.workspace, str(args.get("folder") or "."))
        await ctx.event("terminal", f"$ deploy {len(files)} files to {host}{' ' + site if site else ''}")
        if host == "cloudflare":
            res = await deploy_cloudflare(token, str(args.get("account_id") or ""), site, root)
        elif host == "sftp":
            server, remote = str(args.get("server") or ""), str(args.get("remote_path") or "")
            if not server or not remote or not args.get("url"):
                return "ERROR: sftp needs server, remote_path and url (the public address of that folder)"
            n = await asyncio.to_thread(_sftp_upload, server, int(args.get("port") or 22), str(args.get("username") or ""),
                                        token, remote, files)
            res = {"url": str(args["url"]), "deploy_id": f"{n} files", "site": f"{server}:{remote}", "created": None}
        else:
            async with client_factory() as c:
                res = await {"netlify": deploy_netlify, "vercel": deploy_vercel, "github_pages": deploy_github_pages}[host](c, token, site, files)
    except DeployError as exc:
        return f"ERROR: {exc}"
    live = await live_check(res["url"])
    await ctx.event("terminal", f"deployed: {res['url']} ({live})")
    size = sum(p.stat().st_size for _, p in files)
    lines = [f"deployed to {host}: {res['url']}",
             f"site {res['site']}{' (created now)' if res.get('created') else ''}, deploy {res['deploy_id']}, {len(files)} files, {size / 1024:.0f} KB",
             f"live check: {live}"]
    if res.get("preview"):
        lines.append(f"this deploy's own address: {res['preview']}")
    if res.get("admin"):
        lines.append(f"dashboard: {res['admin']}")
    return "\n".join(lines)


def _target(args: dict[str, Any]) -> str | None:
    host = str(args.get("host") or "").lower()
    return (str(args.get("server") or "") or None) if host == "sftp" else API.get(host)


async def rehearse_deploy(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    host = str(args.get("host") or "").lower()
    out: dict[str, Any] = {"host": host, "site": args.get("site") or "(a new site)", "api": _target(args),
                           "replaces": "what is live on that site now is replaced by this folder"}
    try:
        root, files = site_files(ctx.workspace, str(args.get("folder") or "."))
        out.update(folder=str(root), files=len(files), kb=round(sum(p.stat().st_size for _, p in files) / 1024),
                   listing=[rel for rel, _ in files[:30]] + ([f"… {len(files) - 30} more"] if len(files) > 30 else []))
    except DeployError as exc:
        out["problem"] = str(exc)
    if host == "cloudflare":
        out["runs"] = f"npx {WRANGLER} pages deploy (downloads wrangler from npm if needed)"
    if host == "github_pages":
        out["replaces"] = "the gh-pages branch of that repository is replaced by this folder (the repository is created if missing)"
    return out


def _txt(v: Any) -> str:
    return str(v).rstrip(".")


async def dns_lookup(c: httpx.AsyncClient, name: str, rtype: str) -> list[str]:
    r = await c.get("https://cloudflare-dns.com/dns-query", params={"name": name, "type": rtype},
                    headers={"Accept": "application/dns-json"})
    if r.status_code != 200:
        raise DeployError(f"DNS lookup failed: HTTP {r.status_code}")
    want = {"A": 1, "NS": 2, "CNAME": 5, "AAAA": 28}[rtype]
    return [_txt(a["data"]) for a in r.json().get("Answer", []) if a.get("type") == want]


KNOWN_TARGETS = {"netlify": ("netlify.app", "netlify.com", "75.2.60.5"), "vercel": ("vercel.app", "vercel-dns.com", "76.76.21.21"),
                 "cloudflare": ("pages.dev",), "github_pages": ("github.io", "185.199.108.153", "185.199.109.153",
                                                                 "185.199.110.153", "185.199.111.153")}


async def check_domain(args: dict[str, Any], ctx: ToolContext) -> str:
    domain = re.sub(r"^https?://", "", str(args.get("domain") or "").strip().lower()).split("/")[0]
    if not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", domain):
        return "ERROR: give a domain like example.com"
    expect = str(args.get("expect") or "").strip().lower()
    lines = [f"{domain}:"]
    try:
        async with client_factory() as c:
            recs = {t: await dns_lookup(c, domain, t) for t in ("CNAME", "A", "AAAA", "NS")}
    except (DeployError, httpx.HTTPError) as exc:
        return f"ERROR: {exc}"
    for t, vals in recs.items():
        if vals:
            lines.append(f"  {t}: {', '.join(vals[:6])}")
    if not recs["A"] and not recs["AAAA"] and not recs["CNAME"]:
        lines.append("  no address records: the domain doesn't point anywhere yet")
    found = [h for h, marks in KNOWN_TARGETS.items() if any(m in v for v in recs["CNAME"] + recs["A"] for m in marks)]
    if found:
        lines.append(f"  points at: {', '.join(found)}")
    if expect:
        seen = recs["CNAME"] + recs["A"] + recs["AAAA"]
        ok = any(expect == v or v.endswith("." + expect) or expect in v for v in seen) or expect in found
        lines.append(f"  expected {expect}: {'yes' if ok else 'NO — the DNS points elsewhere (changes can take up to a day)'}")
    lines.append(f"  https: {await live_check('https://' + domain, tries=1)}")
    return "\n".join(lines)


def hosting_tools() -> list[Tool]:
    s = {"type": "string"}
    return [
        Tool("deploy_site",
             "Put a folder of your project on the web (a static site: it needs index.html at its top). host: netlify, vercel, "
             "cloudflare (also account_id), github_pages (site='owner/repo') or sftp (the user's own server: server, username, "
             "remote_path, url). token: a vault handle, e.g. {{secret:netlify_token}}, {{secret:vercel_token}}, "
             "{{secret:cloudflare_token}}, {{secret:github_token}}, {{secret:sftp_password}} — never a value. site: the site/project "
             "name (made if it doesn't exist). The user approves each deploy; the live URL is checked after.",
             {"type": "object", "properties": {"host": {"type": "string", "enum": list(HOSTS)}, "folder": s, "site": s, "token": s,
                                               "account_id": s, "server": s, "port": {"type": "integer"}, "username": s,
                                               "remote_path": s, "url": s},
              "required": ["host", "token"]},
             "R3", deploy_site, timeout=900, path_arg=None, secret_args=("token",), secret_target=_target,
             rehearse=rehearse_deploy,
             summary=lambda a: f"deploy_site {a.get('folder') or '.'} → {a.get('host')} {a.get('site') or a.get('server') or ''}".rstrip()),
        Tool("check_domain",
             "Look a domain up (DNS: CNAME, A, AAAA, NS) and fetch it over HTTPS: where it points (netlify, vercel, cloudflare, "
             "github_pages) and whether it answers. expect: the address it should point at (e.g. mysite.netlify.app).",
             {"type": "object", "properties": {"domain": s, "expect": s}, "required": ["domain"]},
             "R2", check_domain, timeout=90, path_arg=None, summary=lambda a: f"check_domain {a.get('domain')}"),
    ]
