# 個人帳號 Repository 中央管理

GitHub 個人帳號沒有 Organization security configuration 的繼承能力。本控制面以
desired-state reconciliation 管理已登錄 repository：預設只產生差異報告，只有明確
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

排程稽核可加 `--fail-on-drift`，讓 drift 或 blocker 以非零 exit code 呈現；一般人工
report 不加此參數，因此發現 drift 不會被誤判成執行失敗。

## Apply

先人工審閱 report，再以 owner 名稱作為第二道確認：

```bash
python scripts/repository_settings.py \
  --repo fhr \
  --apply \
  --confirm-owner jimc1682000
```

未帶 `--repo` 時會處理 inventory 中所有 `manage: true` 的 repositories。初期應逐一
導入；確認 profile 與 required check 正確後，才進行批次 apply。

## 新增 Repository

1. 在 inventory 新增 repository，先設 `manage: false`。
2. 選擇既有 profile，或在 policy 新增所需 required checks。
3. 改成 `manage: true` 並執行 report-only。
4. 解決所有 `BLOCKED`，審閱 `DRIFT` 後才 apply。

敏感 repository 可保留 `manage: false`，或使用 repository-level overrides 放寬或收緊
merge 與 security settings。
