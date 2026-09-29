"""The install site keeps the Omni partnership and the real home folders.

The public page is uploaded by hand from website/. This locks the copy in git
so a later edit cannot send people to the old install locations.
"""

from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "website" / "index.html").read_text(encoding="utf-8")


def test_omnibots_site_tells_you_to_install_omni_first():
    assert 'id="omni"' in HTML
    assert "Works with Omni" in HTML
    assert "https://omni.globalwarningnetworks.com" in HTML
    assert "irm https://omni.globalwarningnetworks.com/install.ps1 | iex" in HTML
    assert "irm https://raw.githubusercontent.com/tattooinmtl/Omnibots/master/install.ps1 | iex" in HTML
    assert "%USERPROFILE%\\.omnibots" in HTML
    assert "~\\.omni" in HTML
    assert "/omnibots status" in HTML
    assert "/omnibots stop" in HTML
    assert "Install Omni first" in HTML
