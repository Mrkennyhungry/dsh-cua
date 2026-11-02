"""Check GitHub Releases of this project for newer versions."""
import json
import logging
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

GITHUB_REPO = "Mrkennyhungry/dsh-cua"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
# The tray opens html_url with the shell, so only this repository's release
# pages and assets are accepted from the API response.
_RELEASE_URL_PREFIX = f"https://github.com/{GITHUB_REPO}/releases/"


@dataclass
class UpdateInfo:
    """Information about an available update."""

    version: str
    release_notes: str
    html_url: str      # GitHub release page URL
    download_url: str  # direct zip download URL


def _trusted_release_url(value: object) -> str:
    url = value if isinstance(value, str) else ""
    return url if url.startswith(_RELEASE_URL_PREFIX) else ""


def check_for_update(current_version: str) -> UpdateInfo | None:
    """Check GitHub for a newer version.

    Designed to run in a background thread. Returns UpdateInfo if a newer
    version is available, or None if no update or check failed.
    Failures are logged at debug level and never propagated.
    """
    try:
        req = urllib.request.Request(
            GITHUB_RELEASES_API,
            headers={"Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        latest = data.get("tag_name", "").lstrip("v")
        if not latest or not _is_newer(latest, current_version):
            return None

        html_url = _trusted_release_url(data.get("html_url"))
        if not html_url:
            logger.debug("Update check ignored a release URL outside %s", _RELEASE_URL_PREFIX)
            return None

        # Find zip asset
        download_url = ""
        for asset in data.get("assets", []):
            if asset.get("name") == "dsh_desktop.zip":
                download_url = _trusted_release_url(asset.get("browser_download_url"))
                break

        logger.info("Update available: v%s (current: v%s)", latest, current_version)
        return UpdateInfo(
            version=latest,
            release_notes=data.get("body", ""),
            html_url=html_url,
            download_url=download_url,
        )
    except Exception:
        logger.debug("Update check failed", exc_info=True)
        return None


def _is_newer(latest: str, current: str) -> bool:
    """Compare two version strings using packaging.version."""
    from packaging.version import Version

    try:
        return Version(latest) > Version(current)
    except Exception:
        return False
