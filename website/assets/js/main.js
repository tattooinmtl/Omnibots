// OmniBots install site.
//  - version badge: read LIVE from GitHub (the default branch's omnibots/__init__.py + its latest commit),
//    so the site shows the newest version as soon as it's pushed; the version in the HTML is the fallback.
//  - copy buttons for the install commands; click-to-zoom screenshots.

const REPO = "tattooinmtl/Omnibots";
const BRANCHES = ["main", "master"];

async function latestVersion() {
  for (const branch of BRANCHES) {
    try {
      const r = await fetch(`https://raw.githubusercontent.com/${REPO}/${branch}/omnibots/__init__.py`, { cache: "no-store" });
      if (!r.ok) continue;
      const m = (await r.text()).match(/__version__\s*=\s*["']([^"']+)["']/);
      if (!m) continue;
      let when = "", sha = "";
      try {
        const c = await fetch(`https://api.github.com/repos/${REPO}/commits/${branch}`, { headers: { Accept: "application/vnd.github+json" } });
        if (c.ok) {
          const j = await c.json();
          sha = (j.sha || "").slice(0, 7);
          when = (j.commit && j.commit.committer && j.commit.committer.date || "").slice(0, 10);
        }
      } catch (_) { /* the commit info is a bonus: the version alone is fine */ }
      return { version: m[1], branch, sha, when };
    } catch (_) { /* offline or blocked: keep the fallback */ }
  }
  return null;
}

async function showVersion() {
  const badge = document.getElementById("version");
  const text = document.getElementById("version-text");
  const v = await latestVersion();
  if (!v) return;
  text.textContent = `v${v.version}`;
  badge.classList.add("live");
  badge.title = `Latest on GitHub (${v.branch}${v.sha ? " @ " + v.sha : ""}${v.when ? ", " + v.when : ""})`;
  if (v.sha) badge.href = `https://github.com/${REPO}/commit/${v.sha}`;
  document.querySelectorAll(".js-version").forEach(el => { el.textContent = `v${v.version}`; });
}

function copyButtons() {
  document.querySelectorAll("[data-copy]").forEach(box => {
    const btn = box.querySelector(".copy");
    const code = box.querySelector("code");
    btn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(code.textContent.trim());
        btn.textContent = "Copied ✓";
      } catch (_) {
        const range = document.createRange();          // no clipboard access: select it for Ctrl+C
        range.selectNodeContents(code);
        const sel = getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        btn.textContent = "Press Ctrl+C";
      }
      btn.classList.add("done");
      setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("done"); }, 1800);
    });
  });
}

function lightbox() {
  const dlg = document.getElementById("lightbox");
  const img = dlg.querySelector("img");
  document.querySelectorAll(".zoom").forEach(b => b.addEventListener("click", () => {
    img.src = b.dataset.src;
    img.alt = b.querySelector("img").alt;
    dlg.showModal();
  }));
  dlg.querySelector(".close").addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close(); });
}

showVersion();
copyButtons();
lightbox();
