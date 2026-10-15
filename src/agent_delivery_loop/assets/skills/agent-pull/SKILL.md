---
name: agent-pull
description: Pick up and execute a user-authorized shared pull task in the current agent session, then report its result.
---

# Agent Pull

At the start of each scheduled activation, run the trusted local updater:

```sh
{{INSTALL_ROOT}}/bin/agent-pull-update
```

If it returns `status: updated`, stop this activation. The currently loaded Skill belongs to the prior release; let the host's next activation load the updated Skill. Continue only when it returns `status: current`. An updater error means do not discover or acquire work.

Use `agent-pull` from `{{INSTALL_ROOT}}/bin/agent-pull`. Derive `OWNER/REPO` from the current project’s configured GitHub origin, then discover candidates. Discovery is not permission to work:

```sh
{{INSTALL_ROOT}}/bin/agent-pull discover --repo OWNER/REPO
```

For each candidate, verify its exact Plan PR and task path with `show`, then read `status`; skip completed, occupied, pending, or unknown candidates. Acquire at most one new task per activation. Read the task's required capabilities and declare only those available in this active harness. For writes, use only the user's already-configured explicit GitHub PAT file path and expected login; never search for, request, print, or create credentials. If these settings are unavailable, stop as blocked. Use a private grant file under `{{INSTALL_ROOT}}/state/grants/` and keep its path for reports:

```sh
{{INSTALL_ROOT}}/bin/agent-pull show --repo OWNER/REPO --plan-pr PLAN_PR --task-path TASK_PATH
{{INSTALL_ROOT}}/bin/agent-pull status --repo OWNER/REPO --plan-pr PLAN_PR --task-path TASK_PATH
{{INSTALL_ROOT}}/bin/agent-pull acquire --repo OWNER/REPO --plan-pr PLAN_PR --task-path TASK_PATH \
  --capability CAPABILITY --harness CURRENT_HARNESS \
  --grant-file {{INSTALL_ROOT}}/state/grants/TASK-REVISION.json \
  --github-pat-file TRUSTED_PAT_FILE --github-login EXPECTED_LOGIN
```

Begin work only when this activation's `acquire` result says `can_execute: true`. A status result never grants permission; after interruption, inspect status and do not resume or acquire the same task again. Execute only the grant's scope in this current agent session. Report `running` before work, then `completed`, `blocked`, or `failed` with a concise summary and any result link. Do not launch another agent, widen task authority, edit the install configuration, or change model credentials or host scheduling settings.

```sh
{{INSTALL_ROOT}}/bin/agent-pull report --repo OWNER/REPO --plan-pr PLAN_PR --task-path TASK_PATH \
  --grant-file {{INSTALL_ROOT}}/state/grants/TASK-REVISION.json --status running --summary "Starting authorized work" \
  --github-pat-file TRUSTED_PAT_FILE --github-login EXPECTED_LOGIN
{{INSTALL_ROOT}}/bin/agent-pull report --repo OWNER/REPO --plan-pr PLAN_PR --task-path TASK_PATH \
  --grant-file {{INSTALL_ROOT}}/state/grants/TASK-REVISION.json --status completed --summary "Result" --artifact URL \
  --github-pat-file TRUSTED_PAT_FILE --github-login EXPECTED_LOGIN
```
