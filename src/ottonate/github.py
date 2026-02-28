"""GitHub integration via gh CLI."""

from __future__ import annotations

import asyncio
import json
import re

import structlog

from ottonate.models import CIStatus, Label, ReviewComment, ReviewStatus

log = structlog.get_logger()


class GitHubClient:
    # -- Issue operations --

    async def search_issues(self, owner: str, label: str) -> list[dict]:
        """Search for issues with a label across all repos in the org."""
        stdout = await self._gh(
            "search",
            "issues",
            "--owner",
            owner,
            "--label",
            label,
            "--state",
            "open",
            "--json",
            "repository,number,labels,title",
            "--limit",
            "100",
        )
        if not stdout:
            return []
        return json.loads(stdout)

    async def list_issues(self, owner: str, repo: str, label: str) -> list[dict]:
        stdout = await self._gh(
            "issue",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--label",
            label,
            "--state",
            "open",
            "--json",
            "number,labels,title",
            "--limit",
            "50",
        )
        if not stdout:
            return []
        return json.loads(stdout)

    async def get_issue(self, owner: str, repo: str, number: int) -> dict:
        stdout = await self._gh(
            "issue",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "number,title,body,labels,state",
        )
        if not stdout:
            return {}
        return json.loads(stdout)

    async def get_issue_body(self, owner: str, repo: str, number: int) -> str:
        data = await self.get_issue(owner, repo, number)
        title = data.get("title", "")
        body = data.get("body", "") or ""
        return f"# {title}\n\n{body}"

    async def create_issue(
        self, owner: str, repo: str, title: str, body: str, labels: list[str] | None = None
    ) -> int:
        args = [
            "issue",
            "create",
            "--repo",
            f"{owner}/{repo}",
            "--title",
            title,
            "--body",
            body,
        ]
        for lbl in labels or []:
            args.extend(["--label", lbl])
        stdout = await self._gh(*args)
        match = re.search(r"/issues/(\d+)", stdout or "")
        if match:
            number = int(match.group(1))
            log.info("issue_created", owner=owner, repo=repo, number=number)
            return number
        raise RuntimeError(f"Failed to parse issue number from: {stdout}")

    async def close_issue(self, owner: str, repo: str, number: int) -> None:
        await self._gh(
            "issue",
            "close",
            str(number),
            "--repo",
            f"{owner}/{repo}",
        )

    # -- Label operations --

    async def add_label(self, owner: str, repo: str, number: int, label: str) -> None:
        await self._gh(
            "issue",
            "edit",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--add-label",
            label,
        )
        log.info("label_added", repo=f"{owner}/{repo}", number=number, label=label)

    async def remove_label(self, owner: str, repo: str, number: int, label: str) -> None:
        await self._gh(
            "issue",
            "edit",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--remove-label",
            label,
        )
        log.info("label_removed", repo=f"{owner}/{repo}", number=number, label=label)

    async def swap_label(
        self, owner: str, repo: str, number: int, remove: Label, add: Label
    ) -> None:
        await self._gh(
            "issue",
            "edit",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--remove-label",
            remove.value,
            "--add-label",
            add.value,
        )
        log.info(
            "label_swap",
            repo=f"{owner}/{repo}",
            number=number,
            removed=remove.value,
            added=add.value,
        )

    async def get_issue_labels(self, owner: str, repo: str, number: int) -> list[str]:
        data = await self.get_issue(owner, repo, number)
        return [lbl.get("name", "") for lbl in data.get("labels", [])]

    # -- Comment operations --

    async def add_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        await self._gh(
            "issue",
            "comment",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--body",
            body,
        )

    async def get_issue_timeline(self, owner: str, repo: str, number: int) -> list[dict]:
        """Fetch label events from the issue timeline API."""
        stdout = await self._gh(
            "api",
            f"repos/{owner}/{repo}/issues/{number}/timeline",
            "--paginate",
        )
        if not stdout:
            return []
        events = json.loads(stdout)
        result = []
        for e in events:
            event_type = e.get("event")
            if event_type not in ("labeled", "unlabeled"):
                continue
            label_data = e.get("label")
            if not label_data or "name" not in label_data:
                continue
            result.append(
                {
                    "event": event_type,
                    "label": label_data["name"],
                    "created_at": e.get("created_at", ""),
                }
            )
        return result

    async def get_comments(self, owner: str, repo: str, number: int) -> list[str]:
        stdout = await self._gh(
            "issue",
            "view",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "comments",
        )
        if not stdout:
            return []
        data = json.loads(stdout)
        return [c.get("body", "") for c in data.get("comments", [])]

    # -- PR operations --

    async def find_pr(self, owner: str, repo: str, issue_key: str) -> tuple[int | None, str | None]:
        stdout = await self._gh(
            "pr",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--state",
            "all",
            "--search",
            issue_key,
            "--json",
            "number,headRefName,state",
        )
        if not stdout:
            return None, None
        prs = json.loads(stdout)
        for pr in prs:
            if issue_key.lower() in pr.get("headRefName", "").lower():
                return pr["number"], pr.get("state")
        if prs:
            return prs[0]["number"], prs[0].get("state")
        return None, None

    async def get_pr_state(self, owner: str, repo: str, pr_number: int) -> str:
        stdout = await self._gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "state",
        )
        if not stdout:
            return "UNKNOWN"
        data = json.loads(stdout)
        return data.get("state", "UNKNOWN").upper()

    async def create_pr(self, owner: str, repo: str, branch: str, title: str, body: str) -> int:
        stdout = await self._gh(
            "pr",
            "create",
            "--repo",
            f"{owner}/{repo}",
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
        )
        match = re.search(r"/pull/(\d+)", stdout or "")
        if match:
            return int(match.group(1))
        raise RuntimeError(f"Failed to parse PR number from: {stdout}")

    async def request_review(self, owner: str, repo: str, pr_number: int, reviewer: str) -> None:
        await self._gh(
            "pr",
            "edit",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
            "--add-reviewer",
            reviewer,
        )

    async def get_ci_status(self, owner: str, repo: str, pr_number: int | None) -> CIStatus:
        if pr_number is None:
            return CIStatus.PENDING

        stdout = await self._gh(
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "name,state",
        )
        if not stdout:
            return CIStatus.PENDING

        checks = json.loads(stdout)
        if not checks:
            return CIStatus.PENDING

        for check in checks:
            state = check.get("state", "").upper()
            if state in ("PENDING", "QUEUED", "IN_PROGRESS"):
                return CIStatus.PENDING
            if state in ("FAILURE", "ERROR", "TIMED_OUT"):
                return CIStatus.FAILED

        return CIStatus.PASSED

    async def get_ci_failure_logs(self, owner: str, repo: str, pr_number: int | None) -> str:
        if pr_number is None:
            return "No PR number"

        stdout = await self._gh(
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "name,state,link",
        )
        if not stdout:
            return "Could not fetch checks"

        checks = json.loads(stdout)
        failed = [c for c in checks if c.get("state", "").upper() in ("FAILURE", "ERROR")]

        logs_parts = []
        for check in failed[:3]:
            name = check.get("name", "unknown")
            logs_parts.append(f"## Failed check: {name}")

            details_url = check.get("link", "")
            run_id_match = re.search(r"/actions/runs/(\d+)", details_url)
            if run_id_match:
                run_stdout = await self._gh(
                    "run",
                    "view",
                    run_id_match.group(1),
                    "--repo",
                    f"{owner}/{repo}",
                    "--log-failed",
                )
                if run_stdout:
                    logs_parts.append(run_stdout[:5000])
                else:
                    logs_parts.append(f"Details: {details_url}")
            else:
                logs_parts.append(f"Details: {details_url or 'N/A'}")

        return "\n\n".join(logs_parts) or "No failure details available"

    async def get_pr_diff(self, owner: str, repo: str, pr_number: int | None) -> str:
        if pr_number is None:
            return ""
        stdout = await self._gh(
            "pr",
            "diff",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
        )
        return stdout or ""

    async def get_review_status(self, owner: str, repo: str, pr_number: int | None) -> ReviewStatus:
        if pr_number is None:
            return ReviewStatus.PENDING

        stdout = await self._gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "reviews",
        )
        if not stdout:
            return ReviewStatus.PENDING

        data = json.loads(stdout)
        reviews = data.get("reviews", [])
        if not reviews:
            return ReviewStatus.PENDING

        latest_by_author: dict[str, str] = {}
        for review in reviews:
            author = review.get("author", {}).get("login", "")
            state = review.get("state", "").upper()
            if author:
                latest_by_author[author] = state

        states = set(latest_by_author.values())
        if "APPROVED" in states and "CHANGES_REQUESTED" not in states:
            return ReviewStatus.APPROVED
        if "CHANGES_REQUESTED" in states:
            return ReviewStatus.CHANGES_REQUESTED
        if "COMMENTED" in states:
            return ReviewStatus.COMMENTED
        return ReviewStatus.PENDING

    async def get_unaddressed_comments(
        self, owner: str, repo: str, pr_number: int | None, bot_username: str
    ) -> list[ReviewComment]:
        if pr_number is None:
            return []

        stdout = await self._gh(
            "api",
            f"repos/{owner}/{repo}/pulls/{pr_number}/comments",
            "--paginate",
        )
        if not stdout:
            return []

        all_comments = json.loads(stdout)
        bot_replied_ids = {
            c.get("in_reply_to_id")
            for c in all_comments
            if c.get("user", {}).get("login") == bot_username and c.get("in_reply_to_id")
        }

        result = []
        for c in all_comments:
            if c.get("user", {}).get("login") == bot_username:
                continue
            if c.get("id") in bot_replied_ids:
                continue
            result.append(
                ReviewComment(
                    id=c["id"],
                    author=c.get("user", {}).get("login", "unknown"),
                    body=c.get("body", ""),
                    path=c.get("path"),
                    line=c.get("line") or c.get("original_line"),
                    created_at=c.get("created_at"),
                )
            )

        return result

    async def get_default_branch(self, owner: str, repo: str) -> str:
        stdout = await self._gh(
            "repo",
            "view",
            f"{owner}/{repo}",
            "--json",
            "defaultBranchRef",
        )
        if stdout:
            data = json.loads(stdout)
            branch = (data.get("defaultBranchRef") or {}).get("name")
            if branch:
                return branch
        return "main"

    # -- Project operations --

    async def create_project(self, owner: str, title: str) -> str:
        stdout = await self._gh(
            "project",
            "create",
            "--owner",
            owner,
            "--title",
            title,
            "--format",
            "json",
        )
        if not stdout:
            raise RuntimeError(f"Failed to create project: {title}")
        data = json.loads(stdout)
        project_id = str(data.get("number", data.get("id", "")))
        log.info("project_created", owner=owner, title=title, id=project_id)
        return project_id

    async def add_to_project(self, owner: str, project_number: str, issue_url: str) -> None:
        await self._gh(
            "project",
            "item-add",
            project_number,
            "--owner",
            owner,
            "--url",
            issue_url,
        )

    async def list_project_items(self, owner: str, project_number: str) -> list[dict]:
        stdout = await self._gh(
            "project",
            "item-list",
            project_number,
            "--owner",
            owner,
            "--format",
            "json",
        )
        if not stdout:
            return []
        data = json.loads(stdout)
        return data.get("items", data) if isinstance(data, dict) else data

    # -- Idea PR operations --

    async def list_open_prs(self, owner: str, repo: str) -> list[dict]:
        stdout = await self._gh(
            "pr",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--state",
            "open",
            "--json",
            "number,headRefName,labels,title",
            "--limit",
            "50",
        )
        if not stdout:
            return []
        return json.loads(stdout)

    async def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        stdout = await self._gh(
            "api",
            f"repos/{owner}/{repo}/pulls/{pr_number}/files",
        )
        if not stdout:
            return []
        return json.loads(stdout)

    async def get_pr_details(self, owner: str, repo: str, pr_number: int) -> dict:
        stdout = await self._gh(
            "pr",
            "view",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
            "--json",
            "number,headRefName,labels,title,body,comments,state",
        )
        if not stdout:
            return {}
        return json.loads(stdout)

    async def add_pr_label(self, owner: str, repo: str, pr_number: int, label: str) -> None:
        await self._gh(
            "pr",
            "edit",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
            "--add-label",
            label,
        )
        log.info("pr_label_added", repo=f"{owner}/{repo}", pr=pr_number, label=label)

    async def remove_pr_label(self, owner: str, repo: str, pr_number: int, label: str) -> None:
        await self._gh(
            "pr",
            "edit",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
            "--remove-label",
            label,
        )
        log.info("pr_label_removed", repo=f"{owner}/{repo}", pr=pr_number, label=label)

    async def swap_pr_label(
        self, owner: str, repo: str, pr_number: int, remove: Label, add: Label
    ) -> None:
        await self._gh(
            "pr",
            "edit",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
            "--remove-label",
            remove.value,
            "--add-label",
            add.value,
        )
        log.info(
            "pr_label_swap",
            repo=f"{owner}/{repo}",
            pr=pr_number,
            removed=remove.value,
            added=add.value,
        )

    async def get_directory_contents(
        self, owner: str, repo: str, path: str, ref: str = "main"
    ) -> list[dict]:
        stdout = await self._gh(
            "api",
            f"repos/{owner}/{repo}/contents/{path}",
            "-H",
            "Accept: application/vnd.github.v3+json",
            "--method",
            "GET",
            "-f",
            f"ref={ref}",
        )
        if not stdout:
            return []
        data = json.loads(stdout)
        if isinstance(data, list):
            return data
        return [data]

    async def edit_issue_body(self, owner: str, repo: str, number: int, body: str) -> None:
        await self._gh(
            "issue",
            "edit",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--body",
            body,
        )
        log.info("issue_body_edited", repo=f"{owner}/{repo}", number=number)

    # -- File content --

    async def get_file_content(
        self, owner: str, repo: str, path: str, ref: str = "main"
    ) -> str | None:
        stdout = await self._gh(
            "api",
            f"repos/{owner}/{repo}/contents/{path}",
            "--jq",
            ".content",
            "-H",
            "Accept: application/vnd.github.v3+json",
            "--method",
            "GET",
            "-f",
            f"ref={ref}",
        )
        if not stdout:
            return None
        import base64

        try:
            return base64.b64decode(stdout.strip()).decode("utf-8")
        except Exception:
            return stdout

    async def merge_pr(self, owner: str, repo: str, pr_number: int) -> None:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            f"{owner}/{repo}",
            "--squash",
            "--delete-branch",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to merge PR #{pr_number} in {owner}/{repo}: {stderr.decode()}"
            )
        log.info("pr_merged", repo=f"{owner}/{repo}", pr=pr_number)

    # -- Notification helpers --

    async def mention_on_issue(
        self, owner: str, repo: str, number: int, user_or_team: str, message: str
    ) -> None:
        body = f"@{user_or_team} {message}"
        await self.add_comment(owner, repo, number, body)

    async def assign_issue(self, owner: str, repo: str, number: int, assignee: str) -> None:
        await self._gh(
            "issue",
            "edit",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--add-assignee",
            assignee,
        )

    # -- Label management --

    async def ensure_labels(
        self, owner: str, repo: str, labels: dict[str, str]
    ) -> list[str]:
        """Create any missing labels in a repo. Returns list of labels created.

        ``labels`` maps label name to hex color (without #).
        """
        stdout = await self._gh(
            "label", "list", "--repo", f"{owner}/{repo}", "--json", "name", "--limit", "200"
        )
        existing = set()
        if stdout:
            existing = {item.get("name", "") for item in json.loads(stdout)}

        created: list[str] = []
        for name, color in labels.items():
            if name not in existing:
                result = await self._gh(
                    "label", "create", name,
                    "--repo", f"{owner}/{repo}",
                    "--color", color,
                    "--force",
                )
                if result or result == "":
                    created.append(name)
                    log.info("label_created", repo=f"{owner}/{repo}", label=name)
        return created

    # -- Internal --

    async def _gh(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.warning("gh_error", args=args, stderr=stderr.decode())
            return ""
        return stdout.decode()
