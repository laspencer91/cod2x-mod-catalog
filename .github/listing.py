"""Prepare a catalog-only pull request from a submitted GitHub issue form."""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def api(path, payload=None, method="POST"):
    args = ["gh", "api", path]
    if payload is not None:
        args += ["--method", method, "--input", "-"]
    result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout) if result.stdout else None


def parse_request(body):
    fields = re.split(r"^### ([^\n]+)\n", body.replace("\r\n", "\n"), flags=re.M)
    values = dict(zip(fields[1::2], (v.strip() for v in fields[2::2])))
    mod = values.get("Mod ID", "")
    repo = values.get("Public mod repository", "").removeprefix("https://github.com/").rstrip("/")
    if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", mod)
            or ".." in mod or mod == "official-cod2x"
            or re.fullmatch(r"(con|prn|aux|nul|com[1-9]|lpt[1-9])", mod.split(".")[0])):
        raise ValueError("Enter the stable mod ID from your release descriptor.")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}", repo) or repo.split("/")[1] in {".", ".."}:
        raise ValueError("Enter a public GitHub repository as owner/repository.")
    return mod, repo


def matching_release(repo, mod):
    for release in api(f"repos/{repo}/releases?per_page=12"):
        assets = {a["name"]: a for a in release["assets"]}
        meta, archive = assets.get("mod.json"), assets.get("mod.zip")
        if release["draft"] or not meta or not archive or meta["size"] > 65536:
            continue
        raw = subprocess.check_output(["gh", "api", f"repos/{repo}/releases/assets/{meta['id']}",
                                       "-H", "Accept: application/octet-stream"])
        if "sha256:" + hashlib.sha256(raw).hexdigest() != meta.get("digest"):
            continue
        try:
            descriptor = json.loads(raw)
            manifest = descriptor["manifest"]
            if (manifest["id"] == mod and release["tag_name"] == "v" + manifest["version"]
                    and descriptor["asset"] == "mod.zip" and 0 < descriptor["size"] == archive["size"] <= 512 * 1024 * 1024
                    and "sha256:" + descriptor["sha256"] == archive.get("digest")):
                return release["html_url"]
        except (ValueError, KeyError, TypeError):
            continue
    raise ValueError("Publish a matching version tag with both mod.json and mod.zip, then reopen this request.")


def prepare(registry, issue):
    mod, repo = parse_request(issue["body"])
    repo = api(f"repos/{repo}")["full_name"]
    endpoint = f"repos/{registry}"
    number = issue["number"]
    branch = f"listing/{number}"
    owner = registry.split("/")[0]
    # Read a single snapshot so the file and branch start at the same commit.
    base = api(f"{endpoint}/git/ref/heads/main")["object"]["sha"]
    path = f"{endpoint}/contents/catalog.json"
    original = api(f"{path}?ref={base}")
    catalog = json.loads(base64.b64decode(original["content"]))
    entry = {"id": mod, "repository": repo}
    for existing in catalog["mods"]:
        if existing["id"] == mod or existing["repository"].lower() == repo.lower():
            if existing["id"] == mod and existing["repository"].lower() == repo.lower():
                return "This mod is already listed. Publish future releases in the same repository."
            raise ValueError("That mod ID or repository already belongs to another catalog entry.")
    if len(catalog["mods"]) >= 50:
        raise ValueError("The catalog has reached the launcher's current limit of 50 repositories.")
    release = matching_release(repo, mod)
    catalog["mods"].append(entry)
    catalog["mods"].sort(key=lambda item: item["id"])
    content = (json.dumps(catalog, indent=2, ensure_ascii=False) + "\n").encode()
    prs = api(f"{endpoint}/pulls?head={owner}:{branch}&base=main&state=all")
    if prs:
        pr = prs[0]
        if pr["state"] != "open":
            return f"The previous review is closed: {pr['html_url']}. Submit a new request for a revised listing."
    else:
        refs = api(f"{endpoint}/git/matching-refs/heads/{branch}")
        if not any(ref["ref"] == "refs/heads/" + branch for ref in refs):
            api(f"{endpoint}/git/refs", {"ref": "refs/heads/" + branch, "sha": base})
        current = api(f"{path}?ref={branch}")
        if base64.b64decode(current["content"]) != content:
            if current["sha"] != original["sha"]:
                raise ValueError("The pending branch has diverged. A maintainer can review it or start a fresh request.")
            api(path, {"message": f"List {mod}", "branch": branch, "sha": original["sha"],
                       "content": base64.b64encode(content).decode()}, "PUT")
        pr = api(f"{endpoint}/pulls", {"title": f"List {mod}", "head": branch, "base": "main",
                 "body": f"Publisher repository: https://github.com/{repo}\n\nRelease: {release}\n\nCloses #{number}"})
    # This mutation only enables auto-merge. GitHub enforces the required review.
    if not pr.get("auto_merge"):
        api("graphql", {"query": "mutation($id:ID!){enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:SQUASH}){clientMutationId}}",
                        "variables": {"id": pr["node_id"]}})
    return f"Ready for maintainer approval: {pr['html_url']}. GitHub will merge the listing once the required review passes."


if __name__ == "__main__":
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    registry = os.environ["GITHUB_REPOSITORY"]
    issue = event["issue"]
    try:
        message = prepare(registry, issue)
    except ValueError as error:
        message = str(error)
    except subprocess.CalledProcessError as error:
        print(error.stderr or error, file=sys.stderr)
        api(f"repos/{registry}/issues/{issue['number']}/comments",
            {"body": "GitHub could not finish preparing the listing. Check that the release repository is public. A maintainer can inspect the workflow run and retry."})
        raise
    api(f"repos/{registry}/issues/{issue['number']}/comments", {"body": message})
