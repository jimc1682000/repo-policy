# repo-policy

## 用途

維護可重用的 repository 政策與自動化。政策位於 `policies/`，腳本位於 `scripts/`，操作說明位於 `docs/`。

## 驗證

安裝測試相依套件後執行 CI 測試：

```bash
pip install --quiet pyyaml pytest
PYTHONPATH=. python -m pytest tests/ -v
```

## 邊界

先審閱 report，再明確執行 apply；`audit-only` 預設不寫入，只有明確設定 `apply: true` 才可寫入。套用前確認 required checks 已在 default branch 出現。詳見 [README.md](README.md) 與 [操作手冊](docs/personal-account-control-plane.md)。
