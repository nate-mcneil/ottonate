"""Story enrichment: transforms raw stories into execution-grade issues."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass
class EnrichedStory:
    title: str
    description: str
    acceptance_criteria: list[str]
    technical_notes: list[str]
    test_expectations: list[str]
    estimate: str
    dependencies: list[str]
    repo: str = ""

    def to_markdown(self) -> str:
        lines = [self.description, ""]

        if self.acceptance_criteria:
            lines.append("### Acceptance Criteria")
            for ac in self.acceptance_criteria:
                lines.append(f"- [ ] {ac}")
            lines.append("")

        if self.technical_notes:
            lines.append("### Technical Notes")
            for note in self.technical_notes:
                lines.append(f"- {note}")
            lines.append("")

        if self.test_expectations:
            lines.append("### Test Expectations")
            for te in self.test_expectations:
                lines.append(f"- {te}")
            lines.append("")

        if self.estimate:
            lines.append(f"### Estimate: {self.estimate}")

        if self.dependencies:
            lines.append(f"### Dependencies: {', '.join(self.dependencies)}")

        return "\n".join(lines)


def enrich_story_prompt(story_json: dict, spec_context: str = "") -> str:
    ctx = f"\n### Spec Context\n{spec_context}\n" if spec_context else ""
    return f"""You are enriching a GitHub issue to make it execution-grade.

### Original Story
{json.dumps(story_json, indent=2)}
{ctx}
For this story, produce a JSON object with these fields:
- "title": string (refined title)
- "repo": string (target repository name, e.g. "flow-api")
- "description": string (clear, actionable description)
- "acceptance_criteria": array of strings (testable criteria)
- "technical_notes": array of strings (implementation guidance)
- "test_expectations": array of strings (specific tests to write)
- "estimate": string (S/M/L with justification)
- "dependencies": array of strings (issue refs or descriptions)

Be specific and actionable. Each acceptance criterion must be independently testable.
Respond with ONLY the JSON object.
"""


def parse_enriched_story(text: str) -> EnrichedStory | None:
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        data = json.loads(match.group())
        return EnrichedStory(
            title=data.get("title", ""),
            description=data.get("description", ""),
            acceptance_criteria=data.get("acceptance_criteria", []),
            technical_notes=data.get("technical_notes", []),
            test_expectations=data.get("test_expectations", []),
            estimate=data.get("estimate", "M"),
            dependencies=data.get("dependencies", []),
            repo=data.get("repo", ""),
        )
    except (json.JSONDecodeError, KeyError):
        return None
