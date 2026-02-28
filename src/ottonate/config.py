"""Configuration via Pydantic settings."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class OttonateConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OTTONATE_", env_file=".env", extra="ignore")

    # GitHub
    github_org: str = ""
    github_engineering_repo: str = "engineering"
    github_engineering_branch: str = "main"
    github_username: str = ""
    github_agent_label: str = "otto"
    github_notify_team: str = ""

    # Claude
    claude_model: str = "sonnet"
    claude_permission_mode: str = "bypassPermissions"

    # Bedrock
    use_bedrock: bool = False
    aws_region: str = ""
    aws_profile: str = ""
    bedrock_model: str = ""
    bedrock_small_model: str = ""

    # Ideas (Step 0)
    ideas_dir: str = "ideas"
    idea_poll_enabled: bool = True

    # Scheduler
    max_concurrent_tickets: int = 3
    poll_interval_s: int = 30

    # Retries
    max_plan_retries: int = 2
    max_implement_retries: int = 2
    max_ci_fix_retries: int = 3
    max_review_retries: int = 5

    # Rate limiting
    rate_limit_base_delay_s: int = 60
    rate_limit_max_delay_s: int = 600
    rate_limit_cooldown_s: int = 300

    # Paths
    workspace_dir: Path = Path("~/.ottonate/workspaces")

    def resolved_workspace_dir(self) -> Path:
        return self.workspace_dir.expanduser()

    @property
    def engineering_repo_full(self) -> str:
        return f"{self.github_org}/{self.github_engineering_repo}"
