"""Tests for engineering repo initialization."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ottonate.init_engineering import _scaffold, init_engineering


class TestScaffold:
    def test_creates_directories(self, tmp_path):
        _scaffold(tmp_path)

        assert (tmp_path / "architecture").is_dir()
        assert (tmp_path / "specs").is_dir()
        assert (tmp_path / "decisions").is_dir()
        assert (tmp_path / ".ottonate").is_dir()

    def test_creates_starter_files(self, tmp_path):
        _scaffold(tmp_path)

        assert (tmp_path / "architecture" / "overview.md").exists()
        assert (tmp_path / "architecture" / "repos.md").exists()
        assert (tmp_path / "specs" / ".gitkeep").exists()
        assert (tmp_path / "decisions" / ".gitkeep").exists()
        assert (tmp_path / ".ottonate" / "config.yml").exists()
        assert (tmp_path / ".ottonate" / "rules.md").exists()

    def test_does_not_overwrite_existing(self, tmp_path):
        (tmp_path / "architecture").mkdir()
        (tmp_path / "architecture" / "overview.md").write_text("custom content")

        _scaffold(tmp_path)

        assert (tmp_path / "architecture" / "overview.md").read_text() == "custom content"

    def test_config_yml_has_content(self, tmp_path):
        _scaffold(tmp_path)
        content = (tmp_path / ".ottonate" / "config.yml").read_text()
        assert "branch_pattern" in content

    def test_rules_md_has_content(self, tmp_path):
        _scaffold(tmp_path)
        content = (tmp_path / ".ottonate" / "rules.md").read_text()
        assert "Organization Rules" in content


class TestInitEngineering:
    @pytest.mark.asyncio
    async def test_invokes_clone_scaffold_and_pr(self, config, mock_github):
        mock_github.create_pr = AsyncMock(return_value=1)

        with (
            patch("ottonate.init_engineering.asyncio.create_subprocess_exec") as mock_exec,
            patch("ottonate.init_engineering._git", new_callable=AsyncMock) as mock_git,
            patch("ottonate.init_engineering.run_agent", new_callable=AsyncMock) as mock_run,
        ):
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc

            from ottonate.models import StageResult

            mock_run.return_value = StageResult(
                text="[INIT_COMPLETE]",
                session_id="s1",
                turns_used=5,
                cost_usd=0.1,
            )

            pr_url = await init_engineering(config, mock_github)

        assert "pull/1" in pr_url
        mock_github.create_pr.assert_called_once()
        assert mock_git.call_count >= 3
