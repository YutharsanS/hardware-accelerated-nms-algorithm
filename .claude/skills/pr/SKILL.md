---
name: pr
description: Clean up code, stage changes, and prepare a pull request
argument-hint: <issue-number>
allowed-tools: Bash(git add:*), Bash(git status:*), Bash(git diff:*), Bash(npm test:*), Bash(npm run lint:*)
---

# Pull Request Preparation Checklist

Before creating a PR, execute these steps:

1. Review git diff: `git diff HEAD`
2. Stage changes: `git add .`
3. Create commit message following conventional commits(if the commits are not already there):
   - `fix:` for bug fixes
   - `feat:` for new features
   - `docs:` for documentation
   - `refactor:` for code restructuring
   - `test:` for test additions
   - `chore:` for maintenance
4. Fetch the task list from the issue number provided by $ARGUMENTS (if thee are any) and report if there are incomplete tasks.
5. Generate PR summary in a human reviewer friendly manner including:
   - What changed
   - Why it changed
   - Testing performed
   - Potential impacts

---
**Last Updated**: August 4, 2026
**Claude Code Version**: 2.1.220
**Sources**:
- https://code.claude.com/docs/en/commands
**Compatible Models**: Claude Fable 5, Claude Opus 5, Claude Sonnet 5, Claude Sonnet 4.6, Claude Opus 4.8, Claude Haiku 4.5
