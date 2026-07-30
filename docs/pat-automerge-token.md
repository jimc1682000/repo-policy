# AUTOMERGE_TOKEN（PAT）設定與一年輪替

Consumer workflow 可選注入：

```yaml
secrets:
  token: ${{ secrets.AUTOMERGE_TOKEN }}
```

Reusable workflow 內：`secrets.token || github.token`。  
**未設定 secret 時仍可跑**（用 `github-actions[bot]`）。設 PAT 是為了：branch protection 擋 bot、或希望 merge actor 是你的帳號。

## 建議原則

| 項目 | 建議 |
|------|------|
| 類型 | Fine-grained PAT（不要 classic） |
| 到期 | **1 year**（不要 forever） |
| Repository access | **Only select repositories**（不要 All） |
| 權限 | Contents R/W、Pull requests R/W；需要 label 再加 Issues R/W |
| 存放 | 個人帳號 → **各 repo 的 Actions secret**（同名 `AUTOMERGE_TOKEN`，值可同一把） |
| Org | 有 Organization 後可改 **Org secret + Selected repos** |
| 輪替 | 到期前用本 repo 腳本更新；**PAT 本身 GitHub 不會 auto-renew** |

## 首次建立（手動，約 5 分鐘）

1. 開：https://github.com/settings/personal-access-tokens/new  
2. Name：`automerge`；Expiration：**1 year**  
3. Resource owner：你的 user  
4. Repository access：**Only select repositories** → 勾你要 automerge 的 repo（這份 allow-list 就是腳本的目標來源）  
5. Repository permissions：  
   - Contents: Read and write  
   - Pull requests: Read and write  
   - Metadata: Read-only（自動）  
   - Issues: Read and write（可選）  
6. Generate → 立刻存進密碼管理器（**不要貼進 chat / commit**）  
7. 寫入 secret（見下方腳本）

## 用腳本寫入 / 輪替（不印出 token）

腳本：[`scripts/set_automerge_token.sh`](../scripts/set_automerge_token.sh)

**預設會用 PAT 自己去列「它被允許存取的 repos」**（fine-grained Only select），再對那些 repo 設 secret。  
**不會**硬編碼 `consumers.yml`。`consumers.yml` 只是「建議誰該接 wrapper」的 inventory。

權限分開：

| 動作 | 用誰的憑證 |
|------|------------|
| 列出 PAT 允許的 repo | `AUTOMERGE_TOKEN` |
| `gh secret set`（寫 secret） | 你本機已 login 的 `gh`（repo admin） |

```bash
# 從密碼管理器注入後執行（勿 echo token）
export AUTOMERGE_TOKEN='…'

# 預設：PAT allow-list 上的所有非 fork / 非 archived repo
./scripts/set_automerge_token.sh --dry-run
./scripts/set_automerge_token.sh

# 或只寫其中幾個（仍建議是 PAT 有勾的）
./scripts/set_automerge_token.sh film-brain jimc1682000.github.io

# 可選：改讀 consumers.yml（舊行為 / 對帳用）
./scripts/set_automerge_token.sh --from-consumers --dry-run
```

1Password（有 CLI 時）：

```bash
export AUTOMERGE_TOKEN="$(op read 'op://Personal/GitHub automerge PAT/credential')"
./scripts/set_automerge_token.sh
unset AUTOMERGE_TOKEN
```

驗證（只看 secret **名稱**是否存在；同樣用 PAT 列目標）：

```bash
export AUTOMERGE_TOKEN='…'
./scripts/check_automerge_token.sh
unset AUTOMERGE_TOKEN
```

## 一年輪替 runbook

約到期前 1～2 週：

1. GitHub 再 Generate **新** fine-grained PAT（同樣 Only select + 權限；可先保留舊 token）  
2. `export AUTOMERGE_TOKEN='新值'` → `./scripts/set_automerge_token.sh`  
3. 任選一 consumer：Actions → PR merge automation → **dry_run=true** 跑一次  
4. 舊 PAT 在 GitHub → **Revoke**  
5. 密碼管理器更新；日曆設下一次提醒（+11 個月）

## 與 pin `@v1` 的關係

- PAT / secret 與 workflow pin **無關**  
- 換 PAT **不必** bump `repo-policy` tag  
- 改 classification 邏輯才需要 bump `v1` → `v1.1` 並改 consumer `uses` / `policy_ref`

## 不該做的事

- 把 PAT 寫進 repo、issue、PR、workflow log  
- `echo "$AUTOMERGE_TOKEN"` / 把 token 當 script 參數（會進 shell history）  
- All repositories + No expiration 同時使用  
- 在 agent 對話裡貼 token 請別人代設
