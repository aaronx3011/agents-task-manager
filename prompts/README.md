# Worker prompt library

Master copies of the prompts sent to workers launched from the Planning queue.
They are model-neutral (Claude, GPT, Gemini) plain Markdown. Edit here first, then
copy into each project's Planning → Settings.

| File | Paste into | Scope |
| --- | --- | --- |
| `base.md` | **Standing instructions** | Identical in every project |
| `project-context.md` | **Project context** | Fill in once per project (facts only) |
| `roles/*.md` | **Prompt templates** (name = role title, text = file content) | Identical set in every project; pick one per Run/Queue |

The card (title, description, checklist) and "Your instructions" carry the task-specific
part. The coordinator appends previous handoffs and the reporting protocol automatically.

## Roles

| Template | Changes code | Ends with |
| --- | --- | --- |
| 01 Implementer | yes | done |
| 02 Bug fixer | yes | done |
| 03 Refactorer | yes | done |
| 04 Test engineer / QA | tests | done |
| 05 Frontend / UI | yes | done |
| 06 Code reviewer | no | review/done |
| 07 Security auditor | no | review |
| 08 Performance | yes | done |
| 09 Researcher / spike | docs | done |
| 10 Architect | docs | review |
| 11 Planner / spec writer | issues/tasks | review |
| 12 Issue triage | issues/tasks | done |
| 13 DevOps / deploy | infra | done |
| 14 Database / migration | yes | done |
| 15 Dependency upgrade | yes | done |
| 16 Docs writer | docs | done |
| 17 Prototyper | throwaway | done |
| 18 Project onboarding | memory | done |

## Rules for editing
- The whole first prompt (context + standing instructions + template + card + instructions
  + handoffs + protocol) must stay under 20,000 characters. Keep base ≈ 7k, context ≤ 3k,
  each role ≤ 2.5k.
- Project settings are frozen into a job when it is queued. After changing these files,
  update every project's settings; already-queued jobs keep the old text.
- Workflow rules belong in `base.md`; repository facts belong in Project context.
  Repository `AGENTS.md`/`CLAUDE.md` files are deliberately ignored by workers.
