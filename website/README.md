# OmniBots install website

The site for **omnibots.globalwarningnetworks.com**: what OmniBots is, real screenshots, and how to install it.
Plain HTML/CSS/JS with no build step: upload this folder as the site's document root.

| File | What it is |
|------|------------|
| `index.html` | The page |
| `assets/css/style.css` | The look (the app's navy/cyan glass) |
| `assets/js/main.js` | The live version badge, copy buttons, screenshot zoom |
| `assets/img/*.png` | Screenshots, made by `python tools/site_screenshots.py` and `python tools/window_preview.py` (demo data, neutral paths) |
| `assets/splash.mp4` | The 7-second intro (same file as the app's splash) |

## The version number

The badge in the header reads `omnibots/__init__.py` from GitHub (`main`, else `master`) when the page loads, so it
updates by itself whenever a new version is pushed. The number written in the HTML is only the fallback when GitHub
can't be reached.

## Try it locally

```bash
python -m http.server 8765 --directory website
```

Then open http://localhost:8765.

## Still to do

- The Windows installer (`.exe`); the "Coming soon" card is its place. (The one-line install command is live.)
