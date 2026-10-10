"""Read the exact main revision's CI status without exposing credentials."""
import json
import os
from pathlib import Path
import re
import stat
import sys
import urllib.error
import urllib.request


def main():
    sha, repo, token_file = sys.argv[1:]
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        print("CI check: invalid revision or UPDATE_REPO configuration.")
        return 1
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "aion2bot-updater",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        path = Path(token_file)
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and info.st_mode & 0o077):
                print("CI check: token file must be a regular file accessible only to its owner (chmod 600).")
                return 1
            token = path.read_text(encoding="utf-8").strip()
            if not token or any(c.isspace() for c in token):
                print("CI check: token file is empty or malformed.")
                return 1
            headers["Authorization"] = "Bearer " + token
        url = f"https://api.github.com/repos/{repo}/actions/runs?head_sha={sha}&event=push&per_page=100"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
        runs = payload.get("workflow_runs")
        if not isinstance(runs, list):
            raise ValueError("Invalid workflow response")
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 404):
            print(f"CI check: GitHub HTTP {error.code}. Private repositories require a valid token with repository Actions read access in UPDATE_GITHUB_TOKEN_FILE (default /etc/aion2bot-github-token). Also check UPDATE_REPO and token expiry/rate limits.")
        else:
            print(f"CI check: GitHub HTTP {error.code}; retry on the next timer run.")
        return 1
    except (OSError, ValueError, TypeError) as error:
        print(f"CI check: metadata unavailable ({type(error).__name__}); retry on the next timer run.")
        return 1
    matching = [run for run in runs if isinstance(run, dict) and run.get("name") == "CI"
                and run.get("head_branch") == "main" and run.get("head_sha") == sha
                and run.get("event") == "push"]
    # A newer rerun supersedes earlier success for the same revision.
    latest = max(matching, key=lambda run: (run.get("run_number", 0), run.get("run_attempt", 0)), default=None)
    if latest and latest.get("status") == "completed" and latest.get("conclusion") == "success":
        print(f"CI verified for main revision {sha}.")
        return 0
    print("CI check: latest exact-main CI is pending, failed or absent; retry on the next timer run.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
