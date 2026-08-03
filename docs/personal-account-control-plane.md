# 個人帳號 Repository 中央管理

GitHub 個人帳號沒有 Organization security configuration 的繼承能力。本控制面以
desired-state reconciliation 管理 owner 擁有的全部 repositories：預設只產生差異報告，只有明確
指定 `--apply` 與 owner 確認值時才會寫入。

同步程式固定使用 GitHub REST API `2026-03-10`，避免 API 預設版本改變造成未預期
payload 行為。

## 管理範圍

- Repository merge settings
- Security and analysis toggles
- Repository-level ruleset
- 各技術棧的 required status checks

不管理 repository 刪除、轉移、visibility、collaborator 或 secret。同步程式也不會刪除
既有 ruleset；只建立或更新名稱為 `repo-policy-baseline` 的 ruleset。

## Report-only

`repositories.yml` 的 `discovery.enabled: true` 會透過 authenticated `/user/repos`
完整分頁探索 owner 的 public／private repositories。未明確列出的 repository 會套用
`baseline` profile、分類為 `audit-only` 並固定 `apply: false`；fork、archived 與沒有
default branch 的 repository 仍列入報告，但分別標示其分類。

GitHub Free 不提供 private repository rulesets 時，報告會標示
`UNAVAILABLE ruleset.repo-policy-baseline: GitHub plan limitation`，不把它誤判成
disabled 或 drift，也不嘗試 apply。Repository API 未回傳的 security feature 同樣
標成 `UNAVAILABLE`；其他 `403` 仍視為授權錯誤並立即停止。

先確認 `gh auth status` 的 active account 是 repository owner，再執行：

```bash
python scripts/repository_settings.py --repo fhr
```

輸出中的 `DRIFT` 是預計變更，`BLOCKED` 代表 apply 會被拒絕。Required status check
必須曾出現在 default branch；這項保護可避免套用不存在的 check 後鎖死 merge。

需要 machine-readable 結果時：

```bash
python scripts/repository_settings.py --repo fhr --json
```

排程稽核可加 `--fail-on-drift`，讓 drift 以非零 exit code 呈現；一般人工
report 不加此參數，因此發現 drift 不會被誤判成執行失敗。預設任何 repository 的
blocker 都會讓 exit code 非零。

週期性中央 audit（GitHub Actions）請再加 `--fail-on-active`，只讓
`classification: active` 的 drift／blocker 讓 workflow 失敗；audit-only、fork、
archived、empty 仍寫進報告，但不會讓排程長期紅燈。

```bash
python scripts/repository_settings.py \
  --fail-on-active \
  --fail-on-drift \
  --output-json reports/repository-settings-audit.json \
  --output-text reports/repository-settings-audit.md
```

## 每週自動 audit

Workflow：`.github/workflows/repository-settings-audit.yml`

- 觸發：每週一 03:17 UTC（`schedule`）與 `workflow_dispatch`
- 行為：discovery 全量 audit；active 有 drift／blocker 時 job 失敗；JSON／Markdown
  report 上傳為 artifact（保留 90 天）
- 必要 secret：`REPO_POLICY_AUDIT_TOKEN`

`github.token` 只綁定目前 repository，無法列出或稽核帳號下其他 private
repositories，因此必須使用 **user-scoped read PAT**：

1. 建立 fine-grained PAT，Resource owner = `jimc1682000`
2. Repository access = **All repositories**（含未來新建）
3. Permissions（read-only）：
   - Metadata: Read
   - Administration: Read（rulesets / merge settings 讀取）
   - Contents: Read
   - Checks: Read
4. 寫入 repo secret 名稱：`REPO_POLICY_AUDIT_TOKEN`（勿印出 token）
5. 在 Actions 手動跑一次 **Repository settings audit** 確認綠燈

此 token 只用於 report-only；workflow **不會**帶 `--apply`。

## Apply

先人工審閱 report，再以 owner 名稱作為第二道確認：

```bash
python scripts/repository_settings.py \
  --repo fhr \
  --apply \
  --confirm-owner jimc1682000
```

未帶 `--repo` 時會處理 inventory 中所有 `manage: true` 的 repositories。初期應逐一
導入；確認 profile 與 required check 正確後，才進行批次 apply。只有 inventory 明確
設定 `apply: true` 的 repository 可以寫入；任何 audit-only repository 都會讓整次 apply
失敗，不會被靜默略過。

批次 rollout 以 `consumers.yml` 既有的人工 active 清單為準。Fork、archived、backup、
舊 Lab 與尚未人工評估的 repository 仍受中央 audit 覆蓋，但不開啟 apply。

## 新增 Repository

1. 新 repository 會自動進入 inventory，無須手動登錄。
2. 要啟用 apply 時才新增 explicit override，選擇 profile 並設 `classification: active`。
3. 執行 report-only，解決所有 `BLOCKED` 並審閱 `DRIFT`。
4. 取得 apply 核准後，才把 `apply` 改成 `true`。

不納管的 repository 使用 `manage: false`；需要長期 audit-only 的 repository 維持
`manage: true`、`apply: false`。Repository-level overrides 可放寬或收緊 merge 與
security settings。
