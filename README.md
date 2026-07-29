# repo-policy

共用 GitHub repo 設定：PR risk classification / low-risk automerge、Renovate preset。

各 consumer repo **不要**複製整份 merge 邏輯，只留 thin wrapper workflow + 可選 override YAML。

## 內容

| 路徑 | 用途 |
|------|------|
| `.github/workflows/pr-automerge.yml` | reusable workflow（`workflow_call`） |
| `scripts/pr_merge_automation.py` | 從 policy YAML 讀規則並執行 label / Codex request / squash merge |
| `policies/pr-automerge.yml` | 預設風險規則 |
| `renovate/default.json` | Renovate shared preset（`extends`） |
| `tests/` | classification 與 guard unit tests |

## 風險原則

每個 PR **exactly one** risk label：

| Label | 行為 |
|-------|------|
| `risk:low` | 可 auto squash merge（gate 全過時） |
| `risk:medium` | 只 label + `@codex review`，不 merge |
| `risk:high` | 同上 |
| `risk:manual-only` | 同上（draft / stacked / 無法自信分類等） |

### `risk:low` 條件（摘要）

- dependency patch/minor、lockfile-only、workflow patch/minor、小幅 docs-only
- required checks 全綠
- unresolved review threads = 0（GraphQL **分頁查完**）
- `mergeable`
- head SHA 不變（`gh pr merge --squash --match-head-commit <oid>`）
- 寫入 `Looks Good` marker comment（僅信任 automation actor）

### Merge gate

- actor ∈ allowed automation identities
- checks green、threads resolved、mergeable、head SHA guard
- **只** merge `risk:low`

### 安全

- 呼叫端可用 `pull_request_target`，但 reusable workflow **不** checkout / 執行 PR head
- script 與預設 policy 來自本 repo 的固定 `policy_ref`（預設 `main`）
- override YAML 只從 caller 的 **default branch** 讀
- marker comment 只信 `trusted_comment_authors` / `AUTOMATION_COMMENT_AUTHORS`
- dependency metadata 缺失或 ambiguous → fail closed（`risk:manual-only` 或 high）

## Consumer 接入（thin wrapper）

### 1. Workflow

在 consumer 建立 `.github/workflows/pr-merge-automation.yml`：

```yaml
name: PR merge automation

on:
  pull_request_target:
    types: [opened, reopened, synchronize, ready_for_review]
    branches: [master]   # or main
  workflow_run:
    workflows: ["CI"]    # 依 repo 實際 CI workflow 名稱
    types: [completed]
  schedule:
    - cron: "17,47 * * * *"
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write
  issues: write
  checks: read
  actions: read
  statuses: read

jobs:
  evaluate:
    uses: jimc1682000/repo-policy/.github/workflows/pr-automerge.yml@main
    with:
      default_branch: master
      pr_number: ${{ github.event.pull_request.number || '' }}
      policy_override_path: .github/policies/pr-automerge.yml
      automation_comment_authors: github-actions[bot],YOUR_GITHUB_LOGIN
      automation_workflow_name: PR merge automation
    secrets:
      token: ${{ secrets.AUTOMERGE_TOKEN }}
```

> `AUTOMERGE_TOKEN` 可選。未設定時 reusable workflow 會 fallback 到 `github.token`（需上述 permissions）。若要用 PAT 跨 check 限制，再設 repo secret。

### 2. Repo-specific override（可選）

`.github/policies/pr-automerge.yml` 只放差異。list 欄位與預設 **union**（例如加 high-risk deps / paths）：

```yaml
high_risk_dependencies:
  - torch

high_risk_file_patterns:
  - backend/db.py
  - backend/interfaces.py
  - backend/llm_client.py
  - backend/models.py
  - backend/routers/search.py
  - backend/services/search/*
  - docs/adr/*

dependabot_structural_file_patterns:
  - backend/db.py
  - backend/interfaces.py
  - backend/llm_client.py
  - backend/models.py
  - backend/routers/search.py
  - backend/services/search/*
  - docs/adr/*

trusted_comment_authors:
  - jimc1682000

allowed_merge_actors:
  - jimc1682000
```

### 3. Renovate

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["github>jimc1682000/repo-policy//renovate/default"]
}
```

Renovate **不** automerge；開 PR 後由本 workflow 依 risk 決定。

## 本機測試

```bash
pip install pyyaml pytest
python -m pytest tests/ -v
```

Dry-run（需 `gh` auth 與目標 repo）：

```bash
export GITHUB_REPOSITORY=owner/repo
export POLICY_PATH=policies/pr-automerge.yml
export DEFAULT_BRANCH=master
export DRY_RUN=1
export PR_NUMBER=123
export GITHUB_ACTOR=github-actions[bot]
python scripts/pr_merge_automation.py
```

## 固定 ref

生產環境建議 pin：

```yaml
uses: jimc1682000/repo-policy/.github/workflows/pr-automerge.yml@v1
# 並傳
with:
  policy_ref: v1
```

`main` 適合個人 repo 快速迭代；對外或高敏感 repo 請 pin tag / commit SHA。
