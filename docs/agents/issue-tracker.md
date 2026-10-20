# Issue tracker: GitHub

Repository: `aaronx3011/agents-task-manager`. Use `gh` for issue operations.
Specs, tickets, documentation and progress live in GitHub Issues; never in `.scratch/`
or other local Markdown tickets. These `docs/agents/` files are skill configuration only.

- Create: `gh issue create --repo aaronx3011/agents-task-manager --title "..." --body "..."`
- Read: `gh issue view NUMBER --repo aaronx3011/agents-task-manager --comments`
- List: `gh issue list --repo aaronx3011/agents-task-manager --state open`
- Claim bootstrap work: `gh issue edit NUMBER --add-assignee @me`
- Report: `gh issue comment NUMBER --body "..."`
- Close verified work: `gh issue close NUMBER --comment "..."`

PRs as a request surface: no.
When a skill says publish to the issue tracker, create a GitHub issue.
Live task claims belong in the coordinator once MCP is operational; include issue URLs.
