"""zpilot — Mission control for AI coding sessions."""

__version__ = "0.2.0"


def get_version_info() -> dict:
    """Get version + git SHA for health/status reporting."""
    import subprocess
    info = {"version": __version__, "git_sha": "unknown"}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=__file__.rsplit("/", 2)[0],  # zpilot package dir
        )
        if result.returncode == 0:
            info["git_sha"] = result.stdout.strip()
    except Exception:
        pass
    return info
