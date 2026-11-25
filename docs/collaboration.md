# FomoLess 協作流程

###### tags: `FomoLess` `Git` `協作`

從拿到任務到合併的完整流程。部署環境設定見 [deploy-guide.md](deploy-guide.md)。

## 流程總覽

```
開 issue（指派負責人）→ 從 develop 開分支 → 開發＋測試 → 發 PR（寫 Closes #N）→ 1 人 approve → Squash and merge
                                                                                 └─ issue 自動關閉、CI 自動部署 dev
```

## 1. Issue

- 在 **Issues → New issue** 選範本：
  - **開發任務**：開發、文件、設定類的工作。標題格式 `[任務代號] 說明`，例如 `[PU-3] 新增 S3 觸發的處理 Lambda`，會自動加上 `task` label。
  - **Bug 回報**：程式壞掉、回覆錯誤、部署失敗。**不要貼金鑰、token、個資。**
  - 臨時提問可以開空白 issue。
- 開好後在右側 **Assignees** 指派負責人。用指令開也可以：

  ```sh
  gh issue create --template 開發任務 --assignee <GitHub 帳號>
  ```

- 接到任務的人，有問題直接在 issue 底下留言，討論紀錄會留在同一個地方。

## 2. 分支

- `main`（prod）、`develop`（dev）受保護，不能直接 push。
- 一律從 `develop` 開分支：`feature/*`（新功能）、`fix/*`（修 bug）。

  ```sh
  git fetch origin
  git switch -c feature/s3-processor origin/develop
  ```

## 3. Commit

- 格式：`feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:` ＋中文說明，例如 `feat: 新增爬蟲上傳用 S3 ingest bucket`。
- 新功能要附測試（`tests/test_*.py`），發 PR 前本機先跑 `pytest`。
- 不要 commit 金鑰、`env.json`、完整爬蟲 JSON。

## 4. Pull Request

- PR 一律發到 `develop`。說明欄會自動帶入範本，**第一行填這個 PR 完成的 issue 編號**：

  | 寫法 | 效果 |
  | --- | --- |
  | `Closes #12` | 合併後自動關閉 #12 |
  | `Closes #12, Closes #13` | 一次關多個，每個編號前面都要寫 `Closes` |
  | `Refs #12` | 只連結、不關閉（只做了一部分時用） |

  注意：填的是 **issue 的編號**，不是 PR 的編號。

- 合併條件：CI 的 `test` 通過、1 人 approve、所有 review 留言都標成 resolved。
- 核准後按 **Squash and merge**。PR 作者可以自己按合併。
- 合併到 `develop` 會自動部署 `fomoless-dev`；不要手動 `sam deploy`。

### 為什麼合併後 issue 會自動關閉

GitHub 只有在 PR 合併進 **default branch** 時才會處理 `Closes #N`。我們 repo 的 default branch 是 `develop`，所以 PR 合併到 develop 就會關閉 issue。之後如果改 default branch，這個機制會失效。

## 5. 發布到 prod

由 Pupu 從 `develop` 發 PR 到 `main`。合併後部署 `fomoless-prod`，需要在 GitHub `production` 環境人工核准。
