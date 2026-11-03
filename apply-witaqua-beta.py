#!/usr/bin/env python3
#
# Apply WitAqua beta version
#
# Copyright (C) WitAqua
# SPDX-License-Identifier: Apache-2.0
#

import argparse
import calendar
import collections
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

GERRIT_URL = "https://review.witaqua.org"
GERRIT_PROJECT = "WitAqua/manifest"
MANIFEST_URL = "https://github.com/WitAqua/manifest.git"
STEPS = 6


def log(msg):
    print(f">>> {msg}", file=sys.stderr, flush=True)


def step(n, msg):
    print(f"\n=== [{n}/{STEPS}] {msg}", file=sys.stderr, flush=True)


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr, flush=True)


def run(*cmd):
    log(" ".join(cmd))
    subprocess.run(cmd, check=True)


def git(path, *args, check=True):
    return subprocess.run(["git", "-C", path, *args], check=check, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def gerrit_get(host, endpoint, params):
    url = f"{host}/{endpoint}?" + urllib.parse.urlencode(params, doseq=True)
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.loads(r.read().decode().split("\n", 1)[1])


def gerrit_query(host, q, options):
    start = 0
    while True:
        data = gerrit_get(host, "changes/", {"q": q, "o": options, "n": 500, "S": start})
        yield from data
        if not data or not data[-1].get("_more_changes"):
            return
        start += len(data)


def parse_utc(stamp):
    return calendar.timegm(time.strptime(stamp.split(".")[0], "%Y-%m-%d %H:%M:%S"))


def is_sha(s):
    return bool(re.fullmatch(r"[0-9a-f]{40}", s or ""))


def parse_numbers(values):
    nums = set()
    for v in values:
        for part in v.split(","):
            a, _, b = part.partition("-")
            nums.update(range(int(a), int(b or a) + 1))
    return nums


def latest_open_manifest_change():
    q = f"project:{GERRIT_PROJECT} status:open"
    changes = sorted(gerrit_query(GERRIT_URL, q, []), key=lambda c: c["updated"], reverse=True)
    if not changes:
        sys.exit(f"error: no open changes in {GERRIT_PROJECT}; specify a change")
    ready = [c for c in changes if not c.get("work_in_progress")] or changes
    chosen = ready[0]
    if len(changes) > 1:
        log(f"{len(changes)} open manifest changes; using the most recently updated one:")
        for c in changes:
            mark = "*" if c is chosen else " "
            wip = " [WIP]" if c.get("work_in_progress") else ""
            log(f"  {mark} {c['_number']} ({c['branch']}, updated {c['updated'][:19]}){wip} {c['subject']}")
        log("  (pass the change number to choose another one)")
    return str(chosen["_number"])


def lookup_patchset(arg):
    if not arg:
        arg = latest_open_manifest_change()
    m = re.search(r"/\+/(\d+)(?:/(\d+))?/?$", arg) or re.fullmatch(r"(\d+)(?:/(\d+))?", arg)
    if not m:
        sys.exit(f"error: cannot parse change: {arg}")
    change, ps = int(m.group(1)), m.group(2)

    data = gerrit_get(GERRIT_URL, "changes/", {"q": f"change:{change}", "o": "ALL_REVISIONS"})
    data = [c for c in data if c["project"] == GERRIT_PROJECT]
    if not data:
        sys.exit(f"error: change {change} not found in {GERRIT_PROJECT}")
    c = data[0]

    revs = {r["_number"]: r for r in c["revisions"].values()}
    ps = int(ps) if ps else max(revs)
    if ps not in revs:
        sys.exit(f"error: patchset {ps} not found (available: {sorted(revs)})")
    rev = revs[ps]
    return change, ps, rev["ref"], c["branch"], rev["created"].split(".")[0]


def repo_init(ref, branch):
    cmd = ["repo", "init", "-u", MANIFEST_URL, "-b", ref, "--git-lfs"]
    try:
        run(*cmd)
    except subprocess.CalledProcessError:
        log("retrying with --manifest-upstream-branch")
        run(*cmd, f"--manifest-upstream-branch=refs/heads/{branch}")


def branch_of(upstream):
    if not upstream or is_sha(upstream) or upstream.startswith("refs/tags/"):
        return None
    return upstream[len("refs/heads/"):] if upstream.startswith("refs/heads/") else upstream


class Manifest:
    def __init__(self, path):
        self.tree = ET.parse(path)
        self.root = self.tree.getroot()
        self.remotes = {r.get("name"): r for r in self.root.findall("remote")}
        default = self.root.find("default")
        self.default_remote = default.get("remote") if default is not None else None

    def projects(self):
        return self.root.findall("project")

    def remote_of(self, p):
        return p.get("remote") or self.default_remote

    def git_remote(self, p):
        r = self.remotes.get(self.remote_of(p))
        return (r.get("alias") or r.get("name")) if r is not None else self.remote_of(p)

    def revision_expr(self, p):
        r = self.remotes.get(self.remote_of(p))
        default = self.root.find("default")
        return (p.get("revision")
                or (r.get("revision") if r is not None else None)
                or (default.get("revision") if default is not None else None))

    def gerrit_host(self, p):
        r = self.remotes.get(self.remote_of(p))
        review = r.get("review") if r is not None else None
        if not review:
            return None
        return review if review.startswith("http") else "https://" + review

    def gerrit_project(self, p):
        r = self.remotes.get(self.remote_of(p))
        fetch = r.get("fetch", "") if r is not None else ""
        org = urllib.parse.urlparse(fetch).path.strip("/").split("/")[-1]
        return f"{org}/{p.get('name')}" if org else p.get("name")

    def write(self, path):
        self.tree.write(path, encoding="UTF-8", xml_declaration=True)


def path_of(p):
    return p.get("path") or p.get("name")


def resolve_tips(top, manifest, jobs):
    def resolve(p):
        expr = manifest.revision_expr(p)
        if not expr or is_sha(expr):
            return p, expr, expr
        gitdir = os.path.join(top, ".repo", "projects", path_of(p) + ".git")
        if not os.path.isdir(gitdir):
            return p, expr, None
        if expr.startswith("refs/tags/"):
            ref = expr
        else:
            branch = expr[len("refs/heads/"):] if expr.startswith("refs/heads/") else expr
            ref = f"refs/remotes/{manifest.git_remote(p)}/{branch}"
        r = subprocess.run(["git", "--git-dir", gitdir, "rev-parse", "--verify", "--quiet",
                            ref + "^{commit}"], text=True, stdout=subprocess.PIPE)
        return p, expr, r.stdout.strip() if r.returncode == 0 else None

    unresolved = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        for p, expr, sha in ex.map(resolve, manifest.projects()):
            if sha is None:
                unresolved.append(f"{path_of(p)} ({expr})")
                continue
            p.set("revision", sha)
            if expr != sha and not p.get("upstream"):
                p.set("upstream", expr)

    with open(os.path.join(top, ".repo", "project.list")) as f:
        synced = set(f.read().split())
    missing = [u for u in unresolved if u.split(" ")[0] in synced]
    for m in missing:
        warn(f"cannot resolve revision: {m}")
    print(f"    {len(manifest.projects()) - len(unresolved)} projects resolved, "
          f"{len(unresolved) - len(missing)} skipped (not synced)", file=sys.stderr)


def merged_after(queries, ref_epoch):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S +0000", time.gmtime(ref_epoch))
    shas = set()
    for host, branches in sorted(queries.items()):
        for branch in sorted(branches):
            try:
                data = list(gerrit_query(
                    host, f'status:merged branch:{branch} mergedafter:"{stamp}"', ["CURRENT_REVISION"]))
                shas.update(c["current_revision"] for c in data if c.get("current_revision"))
                print(f"    {host} {branch}: {len(data)} changes merged after base time",
                      file=sys.stderr)
            except Exception as e:
                warn(f"{host} {branch}: Gerrit query failed ({e}); "
                     "falling back to committer date only")
    return shas


def first_commit_before(gitdir, tip, ref_epoch, excluded):
    proc = subprocess.Popen(
        ["git", "--git-dir", gitdir, "log", "--first-parent", "--format=%H %ct", tip],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        for line in proc.stdout:
            sha, ct = line.split()
            if int(ct) <= ref_epoch and sha not in excluded:
                return sha
        return None
    finally:
        proc.kill()
        proc.wait()


def pin_revisions(top, manifest, ref_epoch, jobs, report):
    targets = []
    queries = {}
    for p in manifest.projects():
        branch = branch_of(p.get("upstream"))
        if not branch or not is_sha(p.get("revision")):
            continue
        targets.append((p, branch))
        host = manifest.gerrit_host(p)
        if host:
            queries.setdefault(host, set()).add(branch)

    excluded = merged_after(queries, ref_epoch)

    def resolve(item):
        p, branch = item
        gitdir = os.path.join(top, ".repo", "projects", path_of(p) + ".git")
        return p, branch, first_commit_before(gitdir, p.get("revision"), ref_epoch, excluded)

    changed = 0
    report.append("# pinned revisions (path: branch tip -> pinned)")
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        for p, branch, found in ex.map(resolve, targets):
            tip = p.get("revision")
            if found is None:
                msg = f"{path_of(p)} ({branch}): no commit before base time, keeping tip {tip[:12]}"
                warn(msg)
                report.append(f"warning: {msg}")
            elif found != tip:
                p.set("revision", found)
                changed += 1
                report.append(f"{path_of(p)}: {tip[:12]} -> {found[:12]}")

    print(f"    {len(targets)} branch-tracking projects, {changed} pinned to older commits",
          file=sys.stderr)


def auto_exclude_reason(c):
    if c.get("work_in_progress"):
        return "work in progress"
    votes = c.get("labels", {}).get("Code-Review", {}).get("all", [])
    if any(v.get("value") == -2 for v in votes):
        return "Code-Review -2"
    return None


def find_picks(manifest, branch, ref_epoch, include, exclude):
    by_gerrit = {}
    for p in manifest.projects():
        if manifest.gerrit_host(p) == GERRIT_URL and branch_of(p.get("upstream")) == branch:
            by_gerrit[manifest.gerrit_project(p)] = p

    changes = gerrit_query(GERRIT_URL, f"status:open branch:{branch} -project:{GERRIT_PROJECT}",
                           ["ALL_REVISIONS", "ALL_COMMITS", "DETAILED_LABELS"])

    picks = collections.defaultdict(list)
    excluded = []
    for c in changes:
        num = c["_number"]
        if num in exclude:
            continue
        p = by_gerrit.get(c["project"])
        if p is None:
            if num in include:
                warn(f"change {num}: project {c['project']} is not in the manifest, skipping")
            continue
        revs = sorted(c["revisions"].items(), key=lambda kv: kv[1]["_number"])
        if num not in include:
            revs = [kv for kv in revs if parse_utc(kv[1]["created"]) <= ref_epoch]
        if not revs:
            continue
        reason = auto_exclude_reason(c) if num not in include else None
        if reason:
            excluded.append(f"{path_of(p)}: {num} {c['subject']} ({reason})")
            continue
        sha, rev = revs[-1]
        picks[path_of(p)].append({
            "change": num, "ps": rev["_number"], "ref": rev["ref"], "sha": sha,
            "parents": [x["commit"] for x in rev["commit"]["parents"]],
            "subject": c["subject"], "project": p,
        })

    for path, items in picks.items():
        by_sha = {i["sha"]: i for i in items}
        ordered, seen = [], set()

        def visit(i):
            if i["sha"] in seen:
                return
            seen.add(i["sha"])
            for parent in i["parents"]:
                if parent in by_sha:
                    visit(by_sha[parent])
            ordered.append(i)

        for i in sorted(items, key=lambda i: i["change"]):
            visit(i)
        picks[path] = ordered
    return dict(sorted(picks.items())), sorted(excluded)


def verify_checkout(top, manifest, jobs, report):
    with open(os.path.join(top, ".repo", "project.list")) as f:
        synced = set(f.read().split())
    projects = [p for p in manifest.projects() if path_of(p) in synced]

    def check(p):
        path = os.path.join(top, path_of(p))
        r = git(path, "rev-parse", "HEAD", check=False) if os.path.isdir(path) else None
        head = r.stdout.strip() if r and r.returncode == 0 else None
        return p, head

    mismatches = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        for p, head in ex.map(check, projects):
            if head != p.get("revision"):
                mismatches.append(f"{path_of(p)}: expected {p.get('revision')[:12]}, "
                                  f"HEAD {head[:12] if head else '(missing)'}")

    total = len(projects)
    log(f"{total - len(mismatches)}/{total} projects match the pinned manifest")
    report.append("")
    report.append(f"# checkout verification: {total - len(mismatches)}/{total} match")
    for m in mismatches:
        warn(m)
        report.append(f"mismatch: {m}")
    return not mismatches


def apply_picks(top, manifest, picks, report):
    report.append("")
    report.append("# applied changes")
    report.append("# checked out = Gerrit の commit そのもの (SHA 維持)")
    report.append("# rebased     = cherry-pick で付け直した (SHA が変わる: 上に積んで push すると")
    report.append("#               この change にも新しい patchset ができる)")
    failed = []
    conflicts = []
    rebased = []
    for path, items in picks.items():
        wt = os.path.join(top, path)
        p = items[0]["project"]
        refs = [i["ref"] for i in items]

        fetched = False
        for src in (manifest.git_remote(p), f"{GERRIT_URL}/{manifest.gerrit_project(p)}"):
            if git(wt, "fetch", "--quiet", src, *refs, check=False).returncode == 0:
                fetched = True
                break
        if not fetched:
            msg = f"{path}: failed to fetch {', '.join(refs)}"
            warn(msg)
            report.append(f"FAILED {msg}")
            failed.append(path)
            continue

        broken = set()
        for i in items:
            label = f"{path}: {i['change']}/{i['ps']} {i['subject']}"
            if broken.intersection(i["parents"]):
                broken.add(i["sha"])
                warn(f"skipped (depends on a conflicting change): {label}")
                report.append(f"SKIPPED           {label}")
                conflicts.append(f"{label} (depends on a conflicting change)")
                continue
            if git(wt, "merge-base", "--is-ancestor", i["sha"], "HEAD", check=False).returncode == 0:
                log(f"already included  {label}")
                report.append(f"already included  {label}")
                continue
            head = git(wt, "rev-parse", "HEAD").stdout.strip()
            if i["parents"] == [head]:
                git(wt, "checkout", "--quiet", "--detach", i["sha"])
                log(f"checked out       {label}")
                report.append(f"checked out       {label}")
                continue
            r = git(wt, "cherry-pick", "--allow-empty", i["sha"], check=False)
            if r.returncode != 0:
                unmerged = git(wt, "diff", "--name-only", "--diff-filter=U", check=False).stdout
                staged = git(wt, "diff", "--cached", "--quiet", check=False).returncode
                if not unmerged.strip() and staged == 0:
                    git(wt, "cherry-pick", "--skip", check=False)
                    log(f"already applied   {label}")
                    report.append(f"already applied   {label}")
                    continue
                files = " ".join(unmerged.split())
                git(wt, "cherry-pick", "--abort", check=False)
                warn(f"skipped (conflict in {files}): {label}")
                report.append(f"CONFLICT          {label} ({files})")
                broken.add(i["sha"])
                conflicts.append(f"{label} (conflict in {files})")
                continue
            new = git(wt, "rev-parse", "--short", "HEAD").stdout.strip()
            log(f"rebased           {label} -> {new}")
            report.append(f"rebased           {label} -> {new}")
            rebased.append(label)
    return failed, conflicts, rebased


def print_picks(picks, excluded, report):
    if excluded:
        log(f"open changes excluded automatically ({len(excluded)}, use --pick to force):")
        report.append("")
        report.append("# excluded automatically")
        for e in excluded:
            print(f"    {e}", file=sys.stderr)
            report.append(e)
    if not picks:
        log("no open changes to apply")
        return
    log(f"open changes to apply ({sum(map(len, picks.values()))}):")
    for path, items in picks.items():
        for i in items:
            print(f"    {path}: {i['change']}/{i['ps']} {i['subject']}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("change", nargs="?",
                        help="Gerrit change URL, or <change>[/<patchset>] "
                             "(default: latest patchset of the open manifest change)")
    parser.add_argument("--time", metavar='"YYYY-MM-DD HH:MM:SS"',
                        help="base time in UTC (default: patchset upload time)")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count(),
                        help="number of parallel jobs (default: nproc --all)")
    parser.add_argument("--pick", action="append", default=[], metavar="N[-M]",
                        help="also apply these open changes regardless of base time, "
                             "WIP or Code-Review -2 (latest patchset)")
    parser.add_argument("--exclude", action="append", default=[], metavar="N[-M]",
                        help="do not apply these open changes")
    parser.add_argument("--no-picks", action="store_true",
                        help="do not apply any open changes")
    parser.add_argument("--strict", action="store_true",
                        help="treat cherry-pick conflicts as errors")
    parser.add_argument("--dry-run", action="store_true",
                        help="stop after generating the pinned manifest and listing open changes")
    args = parser.parse_args()

    for cmd in ("repo", "git"):
        if not shutil.which(cmd):
            sys.exit(f"error: {cmd} not found")
    if not os.path.isdir(".repo"):
        sys.exit("error: run this at the top of the repo client (where .repo is)")

    top = os.getcwd()
    state_dir = os.path.join(top, ".repo", "witaqua-beta")

    step(1, "querying Gerrit")
    try:
        change, ps, ref, branch, created = lookup_patchset(args.change)
    except OSError as e:
        sys.exit(f"error: failed to query Gerrit: {e}")
    base = args.time or created
    ref_epoch = parse_utc(base)
    log(f"change:    {GERRIT_URL}/c/{GERRIT_PROJECT}/+/{change}/{ps}")
    log(f"ref:       {ref}")
    log(f"base time: {base} UTC (epoch {ref_epoch})")

    os.makedirs(state_dir, exist_ok=True)
    tip_path = os.path.join(state_dir, f"tip-{change}-{ps}.xml")
    pinned_path = os.path.join(state_dir, f"pinned-{change}-{ps}.xml")
    report_path = os.path.join(state_dir, f"report-{change}-{ps}.txt")
    report = [f"change: {GERRIT_URL}/c/{GERRIT_PROJECT}/+/{change}/{ps}",
              f"base time: {base} UTC", ""]

    def save_report():
        with open(report_path, "w") as f:
            f.write("\n".join(report) + "\n")

    try:
        step(2, "repo init")
        repo_init(ref, branch)

        step(3, "fetching all projects (the work tree is NOT switched yet; "
                "projects whose name changed are removed and recreated in step 5)")
        run("repo", "sync", "-n", "--force-sync", f"-j{args.jobs}")

        step(4, f"resolving revisions at {base} UTC")
        run("repo", "manifest", "--no-local-manifests", "-o", tip_path)
        manifest = Manifest(tip_path)
        resolve_tips(top, manifest, args.jobs)
        pin_revisions(top, manifest, ref_epoch, args.jobs, report)
        manifest.write(pinned_path)
        log(f"pinned manifest: {pinned_path}")

        picks, excluded = {}, []
        if not args.no_picks:
            picks, excluded = find_picks(manifest, branch, ref_epoch,
                                         parse_numbers(args.pick), parse_numbers(args.exclude))
        print_picks(picks, excluded, report)

        if args.dry_run:
            save_report()
            log("dry run: stopping before the work tree is switched")
            log(f"to apply: repo sync --force-sync -j{args.jobs} -m {pinned_path}")
            log(f"report: {report_path}")
            return

        step(5, "switching the work tree to the pinned manifest")
        run("repo", "sync", "--force-sync", f"-j{args.jobs}", "-m", pinned_path)
    except subprocess.CalledProcessError as e:
        save_report()
        sys.exit(f"error: command failed ({e.returncode}): {' '.join(e.cmd)}")

    step(6, "verifying the work tree and applying open changes")
    ok = verify_checkout(top, manifest, args.jobs, report)
    failed, conflicts, rebased = apply_picks(top, manifest, picks, report) if picks else ([], [], [])
    save_report()

    if rebased:
        warn(f"{len(rebased)} open changes were re-created by cherry-pick (new SHA).")
        warn("pushing commits on top of them would upload new patchsets for these changes too:")
        for c in rebased:
            print(f"    {c}", file=sys.stderr)

    if conflicts:
        warn(f"{len(conflicts)} open changes were skipped because they do not apply:")
        for c in conflicts:
            print(f"    {c}", file=sys.stderr)
    log(f"report: {report_path}")
    if not ok or failed or (args.strict and conflicts):
        sys.exit("error: finished with problems (see warnings above / report)")
    log(f"done: {GERRIT_URL}/c/{GERRIT_PROJECT}/+/{change}/{ps} @ {base} UTC")


if __name__ == "__main__":
    main()
