"""GitHub (PLAN.md A17.f.04): repositories, issues and pull requests through the REST API.

The token is the vault secret `github_token` (the same one GitHub Pages deploys use), sent only to api.github.com.
Reading is R2 (like reading the web, with the user's login); anything that writes on GitHub (an issue, a comment, a
pull request, a merge) publishes as the user, so it ALWAYS asks, whatever [approvals] ask_from says, and the card
shows exactly what will be posted. Code goes to GitHub with git_push as before; github_pull_request opens the PR.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

import httpx

from omnibots.runtime.tools import Tool, ToolContext
from omnibots.security.untrusted import wrap

API = "https://api.github.com"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _repo(args: dict[str, Any]) -> str:
    repo = str(args.get("repo") or "").strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not REPO_RE.match(repo):
        raise ValueError("repo must look like owner/name")
    return repo


def github_tools(token: Callable[[], str | None],
                 client_factory=lambda: httpx.AsyncClient(timeout=httpx.Timeout(30.0))) -> list[Tool]:
    def headers() -> dict[str, str]:
        t = token()
        if not t:
            raise RuntimeError("no GitHub token: the user adds one to the vault as github_token (a fine-grained token "
                               "for the repositories the bots may use)")
        return {"Authorization": f"Bearer {t}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "OmniBots"}

    async def call(method: str, path: str, **kw) -> Any:
        async with client_factory() as c:
            r = await c.request(method, f"{API}{path}", headers=headers(), **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", "")
            except ValueError:
                msg = r.text[:200]
            raise RuntimeError(f"GitHub HTTP {r.status_code}: {msg}")
        return r.json() if r.content else {}

    def guard(fn):
        async def run(args: dict[str, Any], ctx: ToolContext) -> str:
            try:
                return await fn(args, ctx)
            except (ValueError, RuntimeError, httpx.HTTPError) as exc:
                return f"ERROR: {exc}"
        return run

    async def repos(args, ctx):
        data = await call("GET", "/user/repos", params={"per_page": min(100, int(args.get("limit") or 30)), "sort": "updated"})
        lines = [f"{r['full_name']}{' (private)' if r.get('private') else ''} · ★{r.get('stargazers_count', 0)} · "
                 f"{r.get('open_issues_count', 0)} open issues/PRs · {r.get('description') or ''}".strip() for r in data]
        ctx.saw_outside = True
        return wrap("\n".join(lines) or "(no repositories)", source="GitHub: your repositories")

    async def repo_info(args, ctx):
        repo = _repo(args)
        r = await call("GET", f"/repos/{repo}")
        out = (f"{r['full_name']} — {r.get('description') or ''}\ndefault branch: {r.get('default_branch')} · "
               f"{'private' if r.get('private') else 'public'} · ★{r.get('stargazers_count')} · forks {r.get('forks_count')} · "
               f"open issues+PRs {r.get('open_issues_count')} · pushed {r.get('pushed_at')}\n{r.get('html_url')}")
        ctx.saw_outside = True
        return wrap(out, source=f"GitHub: {repo}")

    async def issues(args, ctx):
        repo = _repo(args)
        number = args.get("number")
        if number:
            i = await call("GET", f"/repos/{repo}/issues/{int(number)}")
            comments = await call("GET", f"/repos/{repo}/issues/{int(number)}/comments", params={"per_page": 30})
            text = (f"#{i['number']} {i['title']} [{i['state']}] by {i['user']['login']}\n{i.get('body') or ''}\n\n"
                    + "\n\n".join(f"— {c['user']['login']}: {c.get('body') or ''}" for c in comments))
        else:
            state = str(args.get("state") or "open")
            data = await call("GET", f"/repos/{repo}/issues", params={"state": state, "per_page": min(100, int(args.get("limit") or 30))})
            text = "\n".join(f"#{i['number']} {'PR ' if i.get('pull_request') else ''}{i['title']} [{i['state']}] "
                             f"by {i['user']['login']}, {i.get('comments', 0)} comments" for i in data) or f"(no {state} issues)"
        ctx.saw_outside = True
        return wrap(text, source=f"GitHub: {repo} issues")

    async def pulls(args, ctx):
        repo = _repo(args)
        number = args.get("number")
        if number:
            p = await call("GET", f"/repos/{repo}/pulls/{int(number)}")
            files = await call("GET", f"/repos/{repo}/pulls/{int(number)}/files", params={"per_page": 100})
            text = (f"#{p['number']} {p['title']} [{p['state']}{', merged' if p.get('merged') else ''}] "
                    f"{p['head']['ref']} → {p['base']['ref']} by {p['user']['login']}\n{p.get('body') or ''}\n\nfiles:\n"
                    + "\n".join(f"{f['status']:>9} {f['filename']} (+{f['additions']} -{f['deletions']})" for f in files))
        else:
            state = str(args.get("state") or "open")
            data = await call("GET", f"/repos/{repo}/pulls", params={"state": state, "per_page": min(100, int(args.get("limit") or 30))})
            text = "\n".join(f"#{p['number']} {p['title']} [{p['state']}] {p['head']['ref']} → {p['base']['ref']} "
                             f"by {p['user']['login']}" for p in data) or f"(no {state} pull requests)"
        ctx.saw_outside = True
        return wrap(text, source=f"GitHub: {repo} pull requests")

    async def create_issue(args, ctx):
        repo = _repo(args)
        title = str(args.get("title") or "").strip()
        if not title:
            return "ERROR: an issue needs a title"
        i = await call("POST", f"/repos/{repo}/issues", json={"title": title[:256], "body": str(args.get("body") or "")})
        return f"opened issue #{i['number']}: {i['html_url']}"

    async def comment(args, ctx):
        repo = _repo(args)
        body = str(args.get("body") or "").strip()
        if not body or not args.get("number"):
            return "ERROR: a comment needs the issue or PR number and a body"
        c = await call("POST", f"/repos/{repo}/issues/{int(args['number'])}/comments", json={"body": body})
        return f"commented: {c['html_url']}"

    async def open_pr(args, ctx):
        repo = _repo(args)
        head, title = str(args.get("head") or "").strip(), str(args.get("title") or "").strip()
        if not head or not title:
            return "ERROR: a pull request needs head (the pushed branch) and a title"
        base = str(args.get("base") or "").strip()
        if not base:
            base = (await call("GET", f"/repos/{repo}"))["default_branch"]
        p = await call("POST", f"/repos/{repo}/pulls", json={"title": title[:256], "head": head, "base": base,
                                                             "body": str(args.get("body") or ""), "draft": bool(args.get("draft"))})
        return f"opened pull request #{p['number']} ({head} → {base}): {p['html_url']}"

    async def card(kind: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"publishes_on_github": kind, **{k: str(v)[:1500] for k, v in args.items()}}

    s, i = {"type": "string"}, {"type": "integer"}
    T = lambda name, desc, props, fn, risk, req=(), **kw: Tool(name, desc, {"type": "object", "properties": props, "required": list(req)},
                                                              risk, guard(fn), timeout=60, path_arg=None,
                                                              secret_target=lambda a: API, **kw)
    return [
        T("github_repos", "List the user's GitHub repositories (most recently updated first).", {"limit": i}, repos, "R2"),
        T("github_repo", "One GitHub repository: description, default branch, stars, open issues, last push.", {"repo": s},
          repo_info, "R2", ("repo",)),
        T("github_issues", "List a repository's issues (state open/closed/all), or read one issue with its comments (number).",
          {"repo": s, "state": s, "number": i, "limit": i}, issues, "R2", ("repo",)),
        T("github_pull_requests", "List a repository's pull requests, or read one (number) with its changed files.",
          {"repo": s, "state": s, "number": i, "limit": i}, pulls, "R2", ("repo",)),
        T("github_create_issue", "Open an issue on GitHub (publishes as the user: they approve it first).",
          {"repo": s, "title": s, "body": s}, create_issue, "R3", ("repo", "title"), always_ask=True,
          rehearse=lambda a, c: card("a new issue", a), summary=lambda a: f"github_create_issue {a.get('repo')}: {a.get('title')}"),
        T("github_comment", "Comment on a GitHub issue or pull request (publishes as the user: they approve it first).",
          {"repo": s, "number": i, "body": s}, comment, "R3", ("repo", "number", "body"), always_ask=True,
          rehearse=lambda a, c: card("a comment", a), summary=lambda a: f"github_comment {a.get('repo')}#{a.get('number')}"),
        T("github_pull_request", "Open a pull request from a branch you already pushed (git_push) into base (default: the "
          "repository's default branch). Publishes as the user: they approve it first.",
          {"repo": s, "head": s, "base": s, "title": s, "body": s, "draft": {"type": "boolean"}}, open_pr, "R3",
          ("repo", "head", "title"), always_ask=True, rehearse=lambda a, c: card("a pull request", a),
          summary=lambda a: f"github_pull_request {a.get('repo')}: {a.get('head')} → {a.get('base') or 'default'}: {a.get('title')}"),
    ]
