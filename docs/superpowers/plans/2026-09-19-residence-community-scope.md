# Residence Community Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow the residence-platform configuration to select multiple communities while routing every read-only lookup through the selected community's own account and isolated session.

**Architecture:** Store the selected community IDs as a normalized JSON array in `_system_config`, with fallback reads from the legacy single-ID key. Extend the existing residence configuration object with selected community metadata, resolve each task's formal community before external lookup, and create a client/session view for that community only. Keep the existing read-only client and uniform password; do not add external write operations or a new database table.

**Tech Stack:** FastAPI/Pydantic, aiomysql-style cursors, React 18, Ant Design 6, Node built-in tests, pytest.

---

### Task 1: Add failing backend configuration and routing tests

**Files:**
- Modify: `backend/tests/test_residence_platform.py`
- Inspect: `backend/services/residence_platform_config.py`, `backend/services/residence_status_scan.py`, `backend/routers/residence_platform.py`

- [ ] **Step 1: Add tests for normalized multi-community configuration.**

  Cover JSON array parsing, duplicate/invalid values, legacy single-ID fallback, selected community metadata, and public responses containing IDs/names without username or secret values.

- [ ] **Step 2: Add tests for API validation.**

  Exercise multiple active configured communities, empty selection while enabled, unknown IDs, inactive communities, and communities without `residence_username`. Assert HTTP 400 and safe messages.

- [ ] **Step 3: Add tests for per-community client/session routing.**

  Use synthetic community IDs and account usernames. Assert `community_12` and `community_18` sessions remain separate, a task outside the selected scope raises `community_out_of_scope` before the client transport is called, and a client for one community never receives the other community's credentials.

- [ ] **Step 4: Run the focused tests and verify RED.**

  Run:

  ```text
  python -m pytest backend/tests/test_residence_platform.py -q
  ```

  Expected: failures naming the missing multi-community fields/validation/routing behavior, with no import or test syntax errors.

### Task 2: Implement multi-community configuration and API contract

**Files:**
- Modify: `backend/services/residence_platform_config.py`
- Modify: `backend/routers/residence_platform.py`
- Modify: `backend/tests/test_residence_platform.py`

- [ ] **Step 1: Add the array configuration key and value normalization helper.**

  Parse a JSON list of positive integers, deduplicate and sort it. Treat missing/empty array as legacy fallback only when the old single ID is valid. Invalid stored values fail closed to an empty selection.

- [ ] **Step 2: Extend `ResidencePlatformConfig` with selected community data.**

  Add `login_community_ids` and immutable selected-community metadata while retaining `login_community_id`, name, and code as compatibility projections. Build each selected community's username from its encrypted `_communities.residence_username`; never expose that username through `public_residence_config`.

- [ ] **Step 3: Update public configuration output.**

  Return `login_community_ids`, `login_community_names`, selected count, and safe account/session status. Preserve legacy singular fields for older clients, with the first selected community as the projection.

- [ ] **Step 4: Change `ResidenceConfigUpdate` and update validation/save logic.**

  Accept `login_community_ids: list[int]`, reject extra legacy request fields, normalize IDs, validate all selected rows, save the JSON array, and keep the legacy key synchronized to the first ID only for compatibility. Enabled configurations require at least one valid selected community.

- [ ] **Step 5: Clear only affected sessions when scope/account connection changes.**

  Compare old/new selected ID sets and connection fields. Clear sessions for removed or changed communities; clear all sessions when password, base URL, or MAC endpoint changes. Keep audit details to config key names only.

- [ ] **Step 6: Run the focused backend tests and verify GREEN.**

  Run:

  ```text
  python -m pytest backend/tests/test_residence_platform.py -q
  ```

  Expected: all residence platform tests pass, including legacy single-community cases.

### Task 3: Implement task-community routing and isolated sessions

**Files:**
- Modify: `backend/services/residence_status_scan.py`
- Modify: `backend/services/residence_platform_config.py`
- Modify: `backend/tests/test_residence_platform.py`

- [ ] **Step 1: Add a selected-community lookup/view helper.**

  Given a loaded config and resolved community ID, return a client-ready config containing only that community's username/code. Reject IDs not in `login_community_ids` with `ResidencePlatformError("community_out_of_scope", ...)`.

- [ ] **Step 2: Update `_community_client` to require an explicit community target.**

  Use the target ID to form `community_<id>`, load/save only that session, login with the target community username, and preserve organization-code identity checks. Do not fall back to the singular projection or another selected community.

- [ ] **Step 3: Pass the resolved community ID through all lookup helpers.**

  `_lookup_target`, `_lookup_detail_target`, and `_lookup_registration_address_target` must resolve and pass the same target community. The first external call must not happen for an out-of-scope task.

- [ ] **Step 4: Run routing/session tests and the complete residence test module.**

  Run:

  ```text
  python -m pytest backend/tests/test_residence_platform.py -q
  python -m pytest backend/tests/test_residence_status_scan.py -q
  ```

  Expected: all tests pass and no test records real credentials or personal data.

### Task 4: Add failing frontend contract tests

**Files:**
- Modify: `frontend/tests/residenceRegistrationStatus.test.ts`
- Modify: `frontend/tests/offlineResidenceMode.test.ts` if shared config assertions require it
- Inspect: `frontend/src/api/client.ts`, `frontend/src/pages/SystemSettings.tsx`

- [ ] **Step 1: Assert the API types and request use `login_community_ids`.**

  Add source-level contract assertions that the configuration type exposes an array, update payload sends an array, and the old singular field is only a compatibility fallback.

- [ ] **Step 2: Assert the settings page renders a multiple Select.**

  Verify the page uses Ant Design multiple mode, disables inactive/unconfigured options, maps selected IDs to names, and does not send username/password/token fields.

- [ ] **Step 3: Run the focused Node tests and verify RED.**

  Run:

  ```text
  node --experimental-strip-types --test frontend/tests/residenceRegistrationStatus.test.ts frontend/tests/offlineResidenceMode.test.ts
  ```

  Expected: failures for the missing array contract and multi-select implementation.

### Task 5: Implement frontend multi-select configuration

**Files:**
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/pages/SystemSettings.tsx`
- Modify: `frontend/tests/residenceRegistrationStatus.test.ts`

- [ ] **Step 1: Update API interfaces and compatibility normalization.**

  Add `login_community_ids: number[]` and `login_community_names: string[]`; retain singular fields as optional compatibility data. Normalize old responses to a one-element array before page state uses them.

- [ ] **Step 2: Update save payload construction.**

  Send the selected array, preserve the password omission behavior, and keep all existing endpoint paths and read-only wording.

- [ ] **Step 3: Replace the single Select with `mode="multiple"`.**

  Map community options to labels/value/disabled/reason, use selected IDs as the value, calculate selected names locally for the summary fields, and use responsive tag collapsing so narrow layouts remain usable.

- [ ] **Step 4: Update readiness and summary projections.**

  Display selected community names/count through existing configuration summaries without exposing credentials or adding verbose implementation text. Keep the session count and read-only scan controls unchanged.

- [ ] **Step 5: Run the focused frontend tests and verify GREEN.**

  Run:

  ```text
  node --experimental-strip-types --test frontend/tests/residenceRegistrationStatus.test.ts frontend/tests/offlineResidenceMode.test.ts
  ```

  Expected: all focused frontend tests pass.

### Task 6: Update help content only if configuration instructions exist

**Files:**
- Inspect: `backend/help_docs/*.md`
- Modify only the relevant residence-platform help document if it describes the single-community control.

- [ ] **Step 1: Search help documents for the old single-community wording.**

  Run `rg -n "登录社区|居住证平台|单个社区" backend/help_docs`.

- [ ] **Step 2: Update the matching document without adding external write instructions.**

  Describe selecting the communities that form the read-only query scope and keep secrets/personal data out of the document.

- [ ] **Step 3: Run the repository help-document validation if a help file changed.**

### Task 7: Full verification and PR preparation

**Files:**
- Modify only files already covered by Tasks 1–6.
- Create temporary PR event JSON outside the repository.

- [ ] **Step 1: Run backend, frontend, compile, build, and diff checks.**

  ```text
  python -m pytest backend/tests/test_residence_platform.py -q
  python -m pytest backend/tests/test_residence_status_scan.py -q
  python -m compileall backend
  npm test
  npm run build
  git diff --check
  ```

- [ ] **Step 2: Review the diff for scope and secret safety.**

  Confirm no tokens, passwords, cookies, real IDs/phones/addresses, response bodies, external write calls, database migrations, or unrelated dirty-worktree files are staged.

- [ ] **Step 3: Commit implementation changes.**

  Use a descriptive commit such as:

  ```text
  feat: support multi-community residence lookup scope
  ```

- [ ] **Step 4: Push the branch and validate the PR body.**

  Fill every heading in `.github/pull_request_template.md`, explicitly state that real MySQL/external platform/production/device acceptance is unverified, then run `python desktop/scripts/validate_pr_body.py <temporary-event-json>`.

- [ ] **Step 5: Create the PR with base `main`.**

  Re-read the PR URL, base/head, commit, checks, mergeability, and state. Do not merge, deploy, tag, or perform external platform writes.
