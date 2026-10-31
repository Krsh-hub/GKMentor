"""Back up the approved content to GitHub as site/data.js (same file `build-site` writes), from the server itself.

Why: the Vercel site ships site/data.js as its offline/fallback copy, and the backend re-seeds an empty database from it
after a restart. Committing it after each daily run keeps both current without you running anything locally.

Set GK_GITHUB_TOKEN (a fine-grained token with "Contents: Read and write" on this repo only) and
GK_GITHUB_REPO=owner/name (GK_GITHUB_BRANCH defaults to main)."""
import base64, json, os, urllib.error, urllib.request
from .site import build_site_data

PATH = "site/data.js"

def configured():
    return bool(os.getenv("GK_GITHUB_TOKEN") and os.getenv("GK_GITHUB_REPO"))

def _api(method, url, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {os.environ['GK_GITHUB_TOKEN']}", "Accept": "application/vnd.github.object+json" if method == "GET" else "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "gkmentor-admin", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 404 and method == "GET": return None
        raise RuntimeError(f"GitHub {method} failed: HTTP {e.code} {e.read()[:300].decode('utf-8', 'replace')}") from None

def push_data_js(con, message):
    if not configured(): raise RuntimeError("GitHub backup is not set up (GK_GITHUB_TOKEN and GK_GITHUB_REPO).")
    repo, branch = os.environ["GK_GITHUB_REPO"].strip(), os.getenv("GK_GITHUB_BRANCH", "main")
    data = build_site_data(con)
    content = ("window.GK_DATA=" + json.dumps(data, ensure_ascii=False) + ";").encode("utf-8")
    url = f"https://api.github.com/repos/{repo}/contents/{PATH}"
    cur = _api("GET", f"{url}?ref={branch}")
    # files over 1 MB come back without content (only the sha), so this shortcut only works for small sites
    if cur and cur.get("content") and base64.b64decode(cur["content"]) == content:
        print("site/data.js on GitHub is already up to date.", flush=True)
        return {"changed": False, "notes": len(data["notes"])}
    body = {"message": message, "content": base64.b64encode(content).decode(), "branch": branch}
    if cur: body["sha"] = cur["sha"]
    r = _api("PUT", url, body)
    sha = (r.get("commit") or {}).get("sha", "")[:7]
    print(f"Committed {PATH} to {repo}@{branch} ({sha}).", flush=True)
    return {"changed": True, "commit": sha, "notes": len(data["notes"]), "questions": len(data["questions"])}
