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
| `consumers.yml` | 建議接入的 consumer 清單（非全部個人 repo） |
| `docs/pat-automerge-token.md` | PAT 一年輪替 runbook |
| `scripts/set_automerge_token.sh` | 候選=gh login；過濾=PAT probe；寫入 secret（不印 token） |
| `scripts/check_automerge_token.sh` | 同上過濾後檢查 secret 名稱是否存在 |
| `scripts/scaffold_consumer.sh` | 產生 thin wrapper 範本 |
| `scripts/list_consumers.sh` | 列出 adopt / wrapper 狀態 |

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
    uses: jimc1682000/repo-policy/.github/workflows/pr-automerge.yml@v1.1
    with:
      default_branch: master
      policy_ref: v1.1
      pr_number: ${{ github.event.pull_request.number || '' }}
      policy_override_path: .github/policies/pr-automerge.yml
      automation_comment_authors: github-actions[bot],YOUR_GITHUB_LOGIN
      automation_workflow_name: PR merge automation
    secrets:
      token: ${{ secrets.AUTOMERGE_TOKEN }}
```

> `AUTOMERGE_TOKEN` 可選（fine-grained PAT，建議 1 年到期、Only select repos）。未設定時 fallback `github.token`。
>
> 建立 / 一年輪替：見 [docs/pat-automerge-token.md](docs/pat-automerge-token.md) 與 `scripts/set_automerge_token.sh`。

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



## 哪些 repo 該接入？

**不是**帳號下每個 repo 都要開。見 [`consumers.yml`](consumers.yml)：

| 建議 adopt | 跳過 |
|------------|------|
| 有在維護、會開 PR / Dependabot / Renovate 的 active repo | 過期 `*Lab`、fork、archived、純 backup dump |
| `film-brain`、`dotfiles`、`my-kb`、sites、小工具、copier 模板 | `repo-policy` 自身（先避免 self-loop）、agent workspace 需人工評估者 |

```bash
./scripts/list_consumers.sh          # inventory + 是否已有 wrapper
./scripts/scaffold_consumer.sh fhr --default-branch main --write /path/to/fhr --with-override

# PAT secret：目標 = PAT 允許的 repos（非硬編碼 consumers.yml）
export AUTOMERGE_TOKEN='…'           # 從密碼管理器；勿 echo
./scripts/set_automerge_token.sh --dry-run
./scripts/set_automerge_token.sh
./scripts/check_automerge_token.sh
unset AUTOMERGE_TOKEN
```

接入後開 PR 合併 wrapper；`pull_request_target` 要 default branch 上有 workflow 才會事件驅動。

## PAT 輪替（一年）

1. GitHub 重生 fine-grained PAT（Only select + Contents/PR write）  
2. `export AUTOMERGE_TOKEN=…` → `./scripts/set_automerge_token.sh`（自動對齊新 PAT 的 repo allow-list）  
3. 任一 consumer dry-run workflow  
4. Revoke 舊 PAT  

詳見 [docs/pat-automerge-token.md](docs/pat-automerge-token.md)。PAT **無法**自動 renew。

## 固定 ref

生產環境建議 pin：

```yaml
uses: jimc1682000/repo-policy/.github/workflows/pr-automerge.yml@v1.1
# 並傳
with:
  policy_ref: v1.1
```

`main` 適合個人 repo 快速迭代；對外或高敏感 repo 請 pin tag / commit SHA。
