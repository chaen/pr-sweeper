#!/usr/bin/env python
"""This script is based on the LCG/SFT sweep_MR from
https://gitlab.cern.ch/sft/lcgcmake
which in turn is based on the ATLAS MR sweeper from
https://gitlab.cern.ch/atlas-sit/librarian


It can be used to automatically create pull requests to other branches based on existing merge
commits. For example to backport fixes to a list of branches chosen by the "alsoTargeting:<Branch>"
label.

"""

import argparse
import logging
import os
import re
import subprocess
import sys
from pprint import pformat
import yaml
import shlex

import github
from github.GithubException import GithubException


def executeCommandWithRetry(cmd, max_attempts=1, logger=logging):
    """
    Execute shell command with possible retry
    """
    logger.debug("working directory: %s", os.getcwd())
    logger.debug("running command '%s' with max attempts %d", cmd, max_attempts)
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        logger.debug("running attempt %d", attempt)
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True
        )  # pylint: disable=R1732
        out, err = process.communicate()
        status = process.returncode
        out = out.strip().decode()
        err = err.strip().decode()
        logger.debug("command returned %d", status)
        if out:
            logger.debug("stdout:")
            for line in out.splitlines():
                logger.debug("  " + line)
        if err:
            logger.debug("stderr:")
            for line in err.splitlines():
                logger.debug("  " + line)

        # break loop if execution was successful
        if status == 0:
            break

    return status, out, err


def listChangedPackages(pr):
    """
    See if this can be useful to automatically determine target branches based on changed files

    pr ... Github pull request object

    return: list of packages
    """
    changed_files = set(c.filename for c in pr.get_files())
    logging.debug("changed files:\n%s", pformat(changed_files, indent=20))
    return changed_files


def getSweepTargetBranchRules(src_branch):
    """
    Load yaml for sweep configuration
    """
    git_cmd = "git show {0}:Sweep/config.yaml".format(src_branch)
    status, out, _ = executeCommandWithRetry(git_cmd)

    # bail out in case of errors
    if status != 0:
        logging.critical("failed to retrieve CI configuration")
        return None

    try:
        CI_config = yaml.safe_load(out)
    except Exception:  # pylint: disable=W0703
        logging.critical("failed to interpret the following text as YAML:\n%s", out)
        return None
    if CI_config is None:
        logging.warning(
            "empty Sweep/config.yaml in %s, hopefully you knew what you were doing.",
            src_branch,
        )
        return None

    # get branch name without remote repository name
    branch_wo_remote = src_branch.split("/")[1]
    # make sure we have a valid sweeping configuration
    has_sweep_targets = "sweep-targets" in CI_config
    has_targets_for_branch = (
        has_sweep_targets and branch_wo_remote in CI_config["sweep-targets"]
    )
    has_target_rules = (
        has_targets_for_branch and CI_config["sweep-targets"][branch_wo_remote]
    )
    if not has_target_rules:
        return None

    target_branch_rules = CI_config["sweep-targets"][branch_wo_remote]
    logging.info(
        "read %d sweeping rules for PRs to '%s", len(target_branch_rules), src_branch
    )
    logging.debug("sweeping rules: %r", target_branch_rules)

    return target_branch_rules


def getListOfMergeCommits(branch, since, until):
    """
    Return list of merge commit in a given time interval
    """
    logging.info("looking for merge commits on '%s' since '%s'", branch, since)
    git_cmd = 'git log --merges --first-parent --oneline --since="{0}" --until="{1}" {2}'.format(
        since, until, branch
    )
    status, out, _ = executeCommandWithRetry(git_cmd)

    # bail out in case of errors
    if status != 0:
        logging.critical("failed to retrieve merge commits")
        return None

    # extract hashes of merge commits
    hash_list = set()
    for line in out.split("\n"):
        # skip empty lines
        if not line:
            continue
        match = re.match(r"[0-9a-f]{6,}", line)
        if not match:
            logging.critical("could not extract merge commit hash from '%s'", line)
            continue
        hash_list.add(match.group())
    logging.info("found %d merge commits", len(hash_list))
    logging.debug("hashes : %s", repr(hash_list))

    return hash_list


def cherryPickPr(
    gh,
    merge_commit,
    source_branch,
    target_branch_rules,
    repo,
    pr_repo,
    strategy,
    project_name,
    pr_project_name,
    dry_run=False,
):
    """
    Try cherry picking into different branch
    """
    logger = logging.getLogger("merge commit %s" % merge_commit)
    good_branches = set()
    failed_branches = set()

    # get merge commit object
    try:
        commit = repo.get_commit(merge_commit)
    except GithubException as e:
        logging.critical(
            "failed to get merge commit '%s' with\n%s", merge_commit, e.data["message"]
        )
        return

    # get pull request ID from commit message
    _, out, _ = executeCommandWithRetry(
        "git show {0}".format(merge_commit), logger=logger
    )
    match = re.search("Merge pull request #(\\d+)", out)
    if match:
        PR_IID = int(match.group(1))
        logger.debug("corresponds to PR ID %d", PR_IID)

        # retrieve github API object
        try:
            pr_handle = repo.get_pull(PR_IID)
        except GithubException:
            logger.critical("failed to retrieve GitHub pull request handle")
            return
        else:
            logger.debug("retrieved Github pull request handle")
    else:
        logger.critical("failed to determine PR IID")
        return

    # save the release notes
    release_notes = ""
    if match := re.search(
        r"(BEGINRELEASENOTES(?:.|\s)+ENDRELEASENOTES)", pr_handle.body or ""
    ):
        release_notes = match.groups()[0]
    logger.debug("release_notes: %s", release_notes)

    # save original author so that we can add as watcher
    if match := re.search(
        r"Adding original author @(.+) as watcher.", pr_handle.body or ""
    ):
        original_pr_author = match.groups()[0]
    else:
        original_pr_author = pr_handle.user.login
    logger.debug("original_pr_author: %s", original_pr_author)

    pr_user = gh.get_user(original_pr_author)
    author_info = repo.get_commits(author=pr_user)[0].commit.author
    pr_author = f"{author_info.name} <{author_info.email}>"
    logger.debug(f"Commits will be made as: {pr_author}")

    orig_pr_title = pr_handle.title
    # Remove any prefixes like [v7r2]
    if match := re.fullmatch(r"\[[^\]]+\]\s+(.+)", orig_pr_title):
        orig_pr_title = match.groups()[0]

    # handle sweep labels
    labels = set(label.name for label in pr_handle.get_labels())
    for label in labels:
        logger.debug("label: %s", label)

    if "sweep:done" in labels:
        logger.info("was already swept -> skipping ......")
        return
    if "sweep:ignore" in labels:
        logger.info("is marked as ignore -> skipping .......")
        return

    target_branches = set()
    target_branches_exclude = set()

    logger.debug("Looking through PR labels .... ")

    for label in labels:
        logger.debug("label: %s", label)
        if re.match("^sweptFrom:", label):
            logger.info(
                "contains %s label -> skipping to prevent sweeping it twice .......\n",
                label,
            )
            return
        if re.match("^sweep:from ", label):
            _s_ = label.split(" ")
            if len(_s_) == 2:
                _ss_ = [_s_[1].encode("ascii", "replace")]
                logger.info(
                    "was swept from branch: %s -> excluding %s from the target list",
                    _ss_,
                    _ss_,
                )
                target_branches_exclude.update(_ss_)
        if re.match("^alsoTargeting:", label):
            _s_ = label.split(":")
            if len(_s_) == 2:
                _ss_ = [_s_[1]]
                logger.info(
                    "also targets the following branches: %s -> add to target list",
                    _ss_,
                )
                target_branches.update(_ss_)
            else:
                logger.warning("has empty 'alsoTargeting:' label -> ignore")

    # get list of affected packages for this PR
    affected_packages = listChangedPackages(pr_handle)
    logger.debug("PR %d affects the following packages: %r", PR_IID, affected_packages)

    # determine set of target branches from rules and affected packages
    for rule, branches in target_branch_rules.items():
        # get pattern expression for affected packages
        pkg_pattern = re.compile(rule)
        # only add target branches if ALL affected packages match the given pattern
        matches = [pkg_pattern.match(pkg_name) for pkg_name in affected_packages]
        if matches and all(matches):
            logging.debug("add branches for rule '%s': %s", rule, branches)
            target_branches.update(branches)
        else:
            logging.debug("skip branches for rule '%s': %s", rule, branches)

    logger.info(
        "PR %d is swept to %d branches: %r",
        PR_IID,
        len(target_branches),
        list(target_branches),
    )

    if len(target_branches) == 0:
        labels.add("sweep:ignore")
        logger.debug(
            "Zero target branches found, adding sweep:ignore label to the PR handle"
        )
    else:
        labels.add("sweep:done")

    if dry_run:
        logger.debug("----- This is a test run, stop with this PR here ----")
        return

    pr_handle.set_labels(*labels)

    # perform cherry-pick to all target branches
    for tbranch in target_branches:

        tbranch_excluded = False
        for t_excl in target_branches_exclude:
            if t_excl == tbranch:
                tbranch_excluded = True
                break
        if tbranch_excluded:
            logger.info(
                "the PR originates from %s -> skip back sweep to %s", tbranch, tbranch
            )
            continue

        failed = False

        # create remote branch for containing the cherry-pick commit
        cherry_pick_branch = "cherry-pick-2-{0}-{1}".format(merge_commit, tbranch)
        try:
            pr_repo.create_git_ref(
                ref="refs/heads/" + cherry_pick_branch,
                sha=repo.get_branch(tbranch).commit.sha,
            )
        except GithubException as e:
            logger.critical(
                "failed to create remote branch '%s' with\n%s",
                cherry_pick_branch,
                e.data["message"],
            )
            failed = True
        else:
            if strategy == "merge":
                try:
                    pr_repo.merge(cherry_pick_branch, commit.sha)
                except GithubException as e:
                    logger.critical(
                        "failed to merge merge commit, error: %s", e.data["message"]
                    )
                    failed = True
            elif strategy == "cherry-pick":
                _, _, _ = executeCommandWithRetry("git fetch upstream")
                _, _, _ = executeCommandWithRetry("git fetch origin")
                _, _, _ = executeCommandWithRetry(f"git checkout {cherry_pick_branch}")
                status, _, err = executeCommandWithRetry(
                    f"git cherry-pick -x -m 1 {commit.sha}"
                )
                if status == 0:
                    status, _, err = executeCommandWithRetry(
                        f"git commit --amend -m 'sweep: #{PR_IID} {orig_pr_title}' --author='{pr_author}'"
                    )
                    if status != 0:
                        logger.critical(f"edit commit message, error: {err}")
                        failed = True
                    else:
                        status, _, err = executeCommandWithRetry(
                            f"git push origin {cherry_pick_branch}"
                        )
                        if status != 0:
                            logger.critical(f"failed to push, error: {err}")
                            failed = True
                else:
                    logger.critical(
                        "failed to cherry pick merge commit, error: %s", err
                    )
                    failed = True
                    status, _, err = executeCommandWithRetry("git cherry-pick --abort")
            else:
                logger.critical(
                    "invalid strategy! Choose merge or cheery-pick for strategy"
                )
                failed = True

        new_pr_title = f"[sweep:{tbranch.replace('rel-', '')}] {orig_pr_title}"
        body_text = (
            f"Sweep #{PR_IID} `{orig_pr_title}` to `{tbranch}`.\n"
            "\n"
            f"Adding original author @{original_pr_author:s} as watcher.\n"
            "\n"
            f"{release_notes}"
        )
        # only create PR if cherry-pick succeeded
        if failed:
            body_text += "\nCloses #@@@FAILED_ISSUE_ID@@@"
            fixer_instructions = [
                f"cherry-pick {merge_commit} into {tbranch} failed",
                f"check merge conflicts on a local copy of this repository",
                f"```bash",
                f"git fetch upstream",
                f"git checkout upstream/{shlex.quote(tbranch)} -b {shlex.quote(cherry_pick_branch)}",
                f"git cherry-pick -x -m 1 {shlex.quote(merge_commit)}",
                f"# Fix the conflicts",
                f"git cherry-pick --continue",
                f"git commit --amend -m {shlex.quote('sweep: #' + str(PR_IID) + ' ' + orig_pr_title)} --author={shlex.quote(pr_author)}",
                f"git push -u origin {shlex.quote(cherry_pick_branch)}",
                f"",
                f"# If you have the GitHub CLI installed the PR can be made with",
                f"gh pr create \\",
                f"     --label {shlex.quote('sweep:from ' + os.path.basename(source_branch))} \\",
                f"     --base {shlex.quote(tbranch)} \\",
                f"     --repo {shlex.quote(project_name)} \\",
                f"     --title {shlex.quote(new_pr_title)} \\",
                f"     --body {shlex.quote(body_text)}",
                f"```",
            ]
            logger.critical("\n".join(fixer_instructions))
            failed_branches.add((tbranch, merge_commit, "\n".join(fixer_instructions)))
        else:
            logger.info("cherry-picked '%s' into '%s'", merge_commit, tbranch)

            # create merge request
            base_fork_name = os.path.dirname(pr_project_name)
            try:
                pr = repo.create_pull(
                    title=new_pr_title,
                    body=body_text,
                    head=f"{base_fork_name}:{cherry_pick_branch}",
                    base=tbranch,
                )
            except GithubException as e:
                logger.critical(
                    "failed to create pull request for '%s' into '%s' with\n%s",
                    cherry_pick_branch,
                    tbranch,
                    e.data["message"],
                )
                failed_branches.add(
                    (
                        tbranch,
                        merge_commit,
                        f"Failed to open the PR, try to open a PR from "
                        f"{base_fork_name}:{cherry_pick_branch} "
                        f"to {tbranch}",
                    )
                )
            else:
                good_branches.add(tbranch)
                pr.add_to_labels(
                    "sweep:from {0}".format(os.path.basename(source_branch))
                )
                logger.debug(
                    f"Sweeping PR {PR_IID} to {tbranch} with a title: '{new_pr_title}'"
                )
                logger.debug(
                    "source_branch:%s: target_branch:%s: title:%s: descr:%s:",
                    cherry_pick_branch,
                    tbranch,
                    new_pr_title,
                    body_text,
                )
                for label in pr.get_labels():
                    logger.debug("label: %s", label.name)

    # compile comment about sweep results
    if len(target_branches) > 0:
        comment_lines = [
            "**Sweep summary**\n",
            "Sweep ran in https://github.com/%s/actions/runs/%s"
            % (
                os.environ.get("GITHUB_REPOSITORY", "GITHUB_REPOSITORY"),
                os.environ.get("GITHUB_RUN_ID", "GITHUB_RUN_ID"),
            ),
        ]
        if good_branches:
            comment_lines += ["\n### Successful:"] + [
                f"* {x}" for x in sorted(good_branches)
            ]
        if failed_branches:
            comment_lines += ["\n### Failed:"]
            for tbranch, merge_commit, failed_branches in failed_branches:
                comment_lines += [
                    f"* **{tbranch}**",
                    f"  " + failed_branches.replace("\n", "\n  "),
                ]
            # add label to original PR indicating cherry-pick problem
            pr_handle.add_to_labels("sweep:failed")

        # add sweep summary to PR in GitHub
        try:
            pr_body = "\n".join(comment_lines)

            if failed_branches:
                issue_title = f"Sweep failed for PR {orig_pr_title}"
                issue_body = f"{issue_title}\nSee {pr_handle.html_url}"
                issue = repo.create_issue(
                    issue_title, body=issue_body, assignee=original_pr_author
                )
                issue.add_to_labels("sweep:failed")
                pr_body = pr_body.replace("@@@FAILED_ISSUE_ID@@@", str(issue.number))

            # If the sweeping failed,
            # create an issue to keep a visible track of the failed sweep
            pr_handle.create_issue_comment(body=pr_body)

        except GithubException as e:
            logger.critical(
                "failed to add comment with sweep summary with\n{0:s}".format(
                    e.data["message"]
                )
            )
    return


def main():
    """
    main
    """
    parser = argparse.ArgumentParser(
        description="GitHub pull request sweeper",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-b",
        "--branch",
        required=True,
        help="remote branch whose merge commits should be swept (e.g. origin/master)",
    )
    parser.add_argument(
        "-d",
        "--dry-run",
        action="store_true",
        help="only perform a test run without actually modifying anything",
    )
    parser.add_argument(
        "-p",
        "--project-name",
        required=True,
        help="GitHub project with namespace (e.g. user/my-project)",
    )
    parser.add_argument(
        "--pr-project-name",
        required=True,
        help="The GitHub project with namespace for creating the PR from",
    )
    parser.add_argument(
        "-s",
        "--since",
        default="1 month ago",
        help="start of time interval for sweeping PR (e.g. 1 week ago)",
    )
    parser.add_argument(
        "-g",
        "--strategy",
        default="cherry-pick",
        help="cheery-pick the merge commit or merge it (options: cherry-pick or merge)",
    )
    parser.add_argument(
        "-t", "--token", required=True, help="GitHub Personal Access Token (PAT)"
    )
    parser.add_argument(
        "-u",
        "--until",
        default="now",
        help="end of time interval for sweeping PR (e.g. 1 hour ago)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        default="DEBUG",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="verbosity level",
    )
    parser.add_argument(
        "--repository-root",
        dest="root",
        default=os.path.dirname(
            os.path.abspath(os.path.join(os.path.realpath(__file__), "../"))
        ),
        help="path to root directory of git repository",
    )

    # get command line arguments
    args = parser.parse_args()

    # configure log output
    logging.basicConfig(
        format="%(asctime)s %(name)-30s %(levelname)-10s %(message)s",
        datefmt="%H:%M:%S",
        level=logging.getLevelName(args.verbose),
        stream=sys.stdout,
    )

    logging.debug("parsed arguments:")
    for name, value in vars(args).items():
        logging.debug("    %12s : %s", name, value)

    if args.dry_run:
        logging.info("running in TEST mode")

    # we only support porting merge commits from remote branches since we expect
    # them to be created through the Github web interface
    # -> branch must contain the name of the remote repository (e.g. upstream/master)
    # -> infer it
    tokens = args.branch.split("/")
    if len(tokens) < 2:
        logging.critical(
            "expect branch to specify a remote branch (e.g. 'upstream/master')"
        )
        logging.critical(
            "received branch '%s' which does not look like a remote branch", args.branch
        )
        logging.critical("--> aborting")
        sys.exit(1)

    # set name of remote repository
    args.remote_name = tokens[0]

    # get Github API handler
    gh = github.Github(args.token)
    try:
        repo = gh.get_repo(args.project_name)
        logging.debug("retrieved Github project handle")
    except GithubException as e:
        logging.critical("error communication with Github API '%s'", e.data["message"])
        sys.exit(1)
    try:
        pr_repo = gh.get_repo(args.pr_project_name)
        logging.debug("retrieved Github PR project handle")
    except GithubException as e:
        logging.critical("error communication with Github API '%s'", e.data["message"])
        sys.exit(1)
    executeCommandWithRetry("git remote rename origin upstream")
    executeCommandWithRetry(
        f"git remote add origin https://{args.token}@github.com/{args.pr_project_name}.git"
    )

    # get top-level directory of git repository (specific to current directory structure)
    workdir = os.path.abspath(args.root)

    logging.info("changing to root directory of git repository '%s'", workdir)
    current_dir = os.getcwd()
    os.chdir(workdir)

    # fetch latest changes
    status, _, _ = executeCommandWithRetry(
        "git fetch --prune {0}".format(args.remote_name)
    )
    if status != 0:
        logging.critical("failed to fetch from '%s'", args.remote_name)
        return None

    # get list of branches PRs should be forwarded to
    # this lets one set which branches to target based on changed files or other criteria
    # currently not used
    target_branch_rules = getSweepTargetBranchRules(args.branch)
    if not target_branch_rules:
        logging.info("no sweeping rules for branch '%s' found", args.branch)
        target_branch_rules = {}

    # get list of PRs in relevant period
    PR_list = getListOfMergeCommits(args.branch, args.since, args.until)
    if not PR_list:
        logging.info(
            "no PRs to '%s' found in period from %s until %s",
            args.branch,
            args.since,
            args.until,
        )
        sys.exit(0)

    # do the actual cherry-picking
    for pr in PR_list:
        logging.debug("")
        logging.debug("===== Next PR: %s ======", pr)
        cherryPickPr(
            gh,
            pr,
            args.branch,
            target_branch_rules,
            repo,
            pr_repo,
            args.strategy,
            dry_run=args.dry_run,
            project_name=args.project_name,
            pr_project_name=args.pr_project_name,
        )

    # change back to initial directory
    os.chdir(current_dir)
    return None


if __name__ == "__main__":
    main()
