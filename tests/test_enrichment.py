from __future__ import annotations

import json

from ottonate.enrichment import EnrichedStory, enrich_story_prompt, parse_enriched_story


class TestEnrichedStory:
    def test_to_markdown(self):
        story = EnrichedStory(
            title="Login form",
            description="Implement the login form with email/password.",
            acceptance_criteria=[
                "Given a valid email and password, When the user submits, Then they are logged in",
                "Given an invalid password, When the user submits, Then an error is shown",
            ],
            technical_notes=["Use the AuthService API", "Rate limit to 5 attempts/minute"],
            test_expectations=["Unit test: form validation", "E2E test: login flow"],
            estimate="M",
            dependencies=["org/auth-service#100"],
        )
        desc = story.to_markdown()
        assert "Acceptance Criteria" in desc
        assert "Given a valid email" in desc
        assert "Technical Notes" in desc
        assert "AuthService" in desc
        assert "Test Expectations" in desc
        assert "Unit test" in desc
        assert "Estimate: M" in desc
        assert "org/auth-service#100" in desc

    def test_to_markdown_uses_checkboxes_for_ac(self):
        story = EnrichedStory(
            title="Test",
            description="Desc",
            acceptance_criteria=["AC1"],
            technical_notes=[],
            test_expectations=[],
            estimate="S",
            dependencies=[],
        )
        md = story.to_markdown()
        assert "- [ ] AC1" in md


class TestEnrichStoryPrompt:
    def test_includes_story_json(self):
        story = {"title": "Login", "description": "Build it"}
        prompt = enrich_story_prompt(story)
        assert "Login" in prompt
        assert "acceptance_criteria" in prompt

    def test_includes_spec_context(self):
        story = {"title": "Login"}
        prompt = enrich_story_prompt(story, spec_context="Security is important")
        assert "Security is important" in prompt

    def test_mentions_github(self):
        story = {"title": "Login"}
        prompt = enrich_story_prompt(story)
        assert "GitHub issue" in prompt


class TestParseEnrichedStory:
    def test_parses_valid_json(self):
        data = {
            "title": "Login form",
            "description": "Implement login",
            "acceptance_criteria": ["AC1", "AC2"],
            "technical_notes": ["Use API"],
            "test_expectations": ["Unit test"],
            "estimate": "S",
            "dependencies": [],
        }
        result = parse_enriched_story(json.dumps(data))
        assert result is not None
        assert result.title == "Login form"
        assert len(result.acceptance_criteria) == 2

    def test_parses_embedded_json(self):
        data = {
            "title": "X",
            "description": "Y",
            "acceptance_criteria": [],
            "technical_notes": [],
            "test_expectations": [],
            "estimate": "M",
            "dependencies": [],
        }
        text = f"Here is the enriched story:\n{json.dumps(data)}\nDone."
        result = parse_enriched_story(text)
        assert result is not None
        assert result.title == "X"

    def test_returns_none_for_invalid(self):
        assert parse_enriched_story("no json here") is None

    def test_parses_repo_field(self):
        data = {
            "title": "X",
            "description": "Y",
            "repo": "my-service",
            "acceptance_criteria": [],
            "technical_notes": [],
            "test_expectations": [],
            "estimate": "S",
            "dependencies": [],
        }
        result = parse_enriched_story(json.dumps(data))
        assert result is not None
        assert result.repo == "my-service"
