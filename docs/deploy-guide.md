# FomoLess M1 部署教學

###### tags: `FomoLess` `AWS` `SAM` `部署`

> 給管理員（Pupu）第一次部署用。照順序做完，LINE Bot 就會上線，之後合併到 develop 會自動部署。
> 指令以 **Mac** 為主，Windows 不同處會另外標註。全程約 2–3 小時，第 0 步安裝最花時間。

[TOC]

## 先懂三件事

**1. SAM 在做什麼**

`template.yaml` 用文字描述我們要的 AWS 資源（Lambda、API、資料表）。執行兩個指令，SAM 就幫你全部建好：

```
sam build   →  把程式和套件打包好（用 Docker 模擬 Lambda 的 Linux 環境）
sam deploy  →  上傳到 AWS，建立或更新一整組資源（稱為 Stack）
```

之後改程式或改 `template.yaml`，再跑一次這兩個指令，AWS 只會更新有變動的部分。

**2. 這次會建立什麼**

```
學生在 LINE 傳訊息
   → LINE 平台送到 API Gateway 網址（Webhook）
   → Lambda 驗證簽章、查 DynamoDB
   → 用 Reply API 回覆學生（不計入 LINE 訊息額度）
```

**3. 完成後的樣子**

| 學生輸入 | Bot 回覆 |
| --- | --- |
| 加好友 | 歡迎訊息 |
| 任意文字，例如「嗨」 | Hello FomoLess！與使用說明 |
| 「本週活動」 | 接下來 7 天的活動，含時間、地點、學習護照時數、名額 |

---

## 第 0 步：安裝工具（每台電腦一次）

| 工具 | 用途 | 確認指令 |
| --- | --- | --- |
| AWS CLI v2 | 用指令操作 AWS | `aws --version` |
| AWS SAM CLI | 打包、部署 | `sam --version` |
| Docker Desktop | `sam build` 打包 Linux 套件 | `docker run hello-world` |
| Python 3.12 | 執行測試、寫入假資料 | `python3.12 --version` |
| Git、GitHub CLI | 已在 Git 協作入門裝過 | `git --version`、`gh --version` |

### 0-1 AWS CLI v2

- **Mac**：下載官方安裝檔並執行

```bash
curl "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o "AWSCLIV2.pkg"
sudo installer -pkg AWSCLIV2.pkg -target /
```

- **Windows**：下載並執行 https://awscli.amazonaws.com/AWSCLIV2.msi
- 其他方式見[官方安裝說明](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)

### 0-2 AWS SAM CLI

到[官方安裝頁](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)下載對應的安裝檔：

| 電腦 | 安裝檔 |
| --- | --- |
| Mac（M1/M2/M3/M4） | macOS **arm64** 的 `.pkg` |
| Mac（Intel） | macOS **x86_64** 的 `.pkg` |
| Windows | 64 位元的 `.msi` |

Mac 也可以用 Homebrew：`brew install aws-sam-cli`。

### 0-3 Docker Desktop

1. 下載 [Docker Desktop](https://www.docker.com/products/docker-desktop/)，依晶片選 Apple Silicon 或 Intel 版。Windows 安裝時會要求啟用 **WSL 2**，照提示操作。
2. 安裝後**打開 Docker Desktop**，等左下角顯示 Engine running。
3. 確認：

```bash
docker run hello-world
```

看到 `Hello from Docker!` 就成功了。之後每次 `sam build` 前，Docker Desktop 都要是開著的。

### 0-4 Python 3.12

- **Mac**：從 [python.org](https://www.python.org/downloads/) 下載 3.12.x 安裝，或 `brew install python@3.12`
- **Windows**：從 python.org 下載安裝，**第一個畫面要勾選 Add python.exe to PATH**
- 確認：Mac 用 `python3.12 --version`，Windows 用 `py -3.12 --version`

### 0-5 全部確認一次

```bash
aws --version          # aws-cli/2.x.x
sam --version          # SAM CLI, version 1.x.x
docker run hello-world # Hello from Docker!
python3.12 --version   # Python 3.12.x
```

---

## 第 1 步：讓 gl-pupu 能用指令操作 AWS

### 1-1 建立 Access key

1. 用 **gl-pupu** 登入 AWS 主控台。
2. 右上角名稱 → **Security credentials**。
3. 往下找到 **Access keys** → **Create access key**。
4. Use case 選 **Command Line Interface (CLI)** → 勾選下方確認框 → **Next**。
5. Description tag 填 `pupu-laptop-cli`（之後才知道這把鑰匙是哪台電腦的）→ **Create access key**。
6. 按 **Download .csv file** 下載保存。**這個畫面關掉後就看不到 Secret access key 了。**

:::danger
**Access key 等同帳號密碼**
- 絕對不能 commit 進 GitHub、貼到群組或 HackMD
- 下載的 .csv 存進密碼管理工具後就刪掉
- 電腦遺失或不再使用時，到同一頁把它 **Deactivate** 再 **Delete**
- 第 6 步設定好自動部署後，平常就很少需要用到它
:::

### 1-2 設定到電腦

```bash
aws configure
```

依序輸入：

```
AWS Access Key ID [None]:     貼上 Access key ID
AWS Secret Access Key [None]: 貼上 Secret access key
Default region name [None]:   ap-east-2
Default output format [None]: json
```

### 1-3 確認

```bash
aws sts get-caller-identity
```

看到 `"Arn": "arn:aws:iam::你的帳號ID:user/gl-pupu"` 就成功了。

---

## 第 2 步：把骨架放進 repo

### 2-1 開一個新分支

```bash
git clone https://github.com/fomoless-ncu/Fomoless.git
cd Fomoless
git checkout develop
git pull
git checkout -b feature/m1-skeleton
```

### 2-2 複製骨架檔案

把 `fomoless-m1-v2.zip` 解壓縮，複製**資料夾裡的所有內容**（包含 `.github`、`.gitignore` 這些隱藏檔）到 `Fomoless` 資料夾。Mac 的 Finder 預設不顯示隱藏檔，建議用指令：

```bash
# 假設解壓縮在「下載」資料夾
cp -R ~/Downloads/fomoless-m1-v2/. .
ls -a   # 應該看到 .github、.gitignore、template.yaml、backend、shared…
```

Windows（PowerShell）：

```powershell
Copy-Item -Recurse -Force "$HOME\Downloads\fomoless-m1-v2\*" .
Copy-Item -Recurse -Force "$HOME\Downloads\fomoless-m1-v2\.github" .
Copy-Item -Force "$HOME\Downloads\fomoless-m1-v2\.gitignore" .
```

repo 原本的 `README.md` 和 `.gitignore` 會被覆蓋，這是預期的。

### 2-3 本機先跑測試

```bash
python3.12 -m venv .venv
source .venv/bin/activate          # Windows：.venv\Scripts\activate
pip install -r backend/webhook/requirements.txt boto3 pytest
pytest
```

看到 `18 passed` 就代表程式沒問題。之後每次開新終端機，都要先執行 `source .venv/bin/activate`。

---

## 第 3 步：建立 LINE 官方帳號並存金鑰

### 3-1 建立 LINE 官方帳號（dev 測試用）

目前 LINE 要先建立官方帳號，再啟用 Messaging API，無法直接在 Developers Console 建立 channel。

1. 到 [LINE Official Account Manager](https://manager.line.biz/) 用個人 LINE 帳號登入 → **建立帳號**。
2. 帳號名稱填 `FomoLess Dev`（測試用，和正式帳號分開），填完基本資料送出。
3. 進入帳號 → 右上角 **設定** → 左側 **Messaging API** → **啟用 Messaging API**。
4. 選擇或建立 **Provider**，名稱填 `FomoLess` → 同意條款 → 完成。

### 3-2 取得兩個金鑰

到 [LINE Developers Console](https://developers.line.biz/console/) → Provider `FomoLess` → channel `FomoLess Dev`：

| 金鑰 | 位置 |
| --- | --- |
| **Channel secret** | **Basic settings** 分頁 → Channel secret |
| **Channel access token** | **Messaging API** 分頁最下方 → Channel access token (long-lived) → **Issue** |

### 3-3 存進 AWS Parameter Store

金鑰不寫在程式裡，存在 AWS 的 Parameter Store，以加密（SecureString）保存，Lambda 執行時再讀取。

```bash
aws ssm put-parameter --name /fomoless/dev/line-channel-secret \
  --type SecureString --value "貼上 Channel secret"

aws ssm put-parameter --name /fomoless/dev/line-channel-access-token \
  --type SecureString --value "貼上 Channel access token"
```

Windows PowerShell 把行尾的 `\` 換成 `` ` ``，或寫成同一行。看到 `"Version": 1` 就是成功。

:::info
之後要更新金鑰（例如重新 Issue token），在同一個指令最後加上 `--overwrite`。
:::

---

## 第 4 步：打包與部署

### 4-1 檢查範本

```bash
sam validate --lint
```

看到 `template.yaml is a valid SAM Template` 就可以繼續。

### 4-2 打包

**先確認 Docker Desktop 開著。**

```bash
sam build --config-env dev
```

第一次會下載 Lambda 的 Docker 映像檔，大約 3–10 分鐘。看到 `Build Succeeded` 就完成了。

### 4-3 部署

```bash
sam deploy --config-env dev
```

第一次部署時，SAM 會先自動建立一個存放打包檔的 S3 bucket（`aws-sam-cli-managed-default`），再建立 `fomoless-dev` Stack，大約 2–5 分鐘。成功後最下方會印出：

```
Outputs
-------------------------------------------------------------
Key          WebhookUrl
Description  Paste into LINE Developers Console > Messaging API > Webhook URL
Value        https://xxxxxxxx.execute-api.ap-east-2.amazonaws.com/webhook
-------------------------------------------------------------
Successfully created/updated stack - fomoless-dev in ap-east-2
```

**複製 `WebhookUrl` 的值**，下一步會用到。

忘記網址時可以再查：

```bash
aws cloudformation describe-stacks --stack-name fomoless-dev \
  --query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" --output text
```

---

## 第 5 步：串接 LINE 並用手機測試

### 5-1 設定 Webhook

1. LINE Developers Console → `FomoLess Dev` → **Messaging API** 分頁。
2. **Webhook URL** → **Edit** → 貼上 `WebhookUrl` → **Update**。
3. 按 **Verify**，顯示 **Success** 就代表 LINE 能連到我們的 Lambda，而且簽章驗證正確。
4. 打開 **Use webhook**。

### 5-2 關掉官方帳號的自動回覆

不關的話，學生每傳一則訊息會收到兩則回覆（官方帳號一則、我們的 Bot 一則）。

1. LINE Official Account Manager → `FomoLess Dev` → **設定** → **回應設定**。
2. **自動回應訊息**：關閉。
3. **加入好友的歡迎訊息**：關閉（我們的 Bot 會自己送歡迎訊息）。
4. **Webhook**：確認是開啟。

### 5-3 用手機測試

1. Messaging API 分頁有 **QR code**，用手機 LINE 掃描加好友 → 應該收到歡迎訊息。
2. 傳「嗨」→ 應該收到 **Hello FomoLess！👋** 與使用說明。
3. 傳「本週活動」→ 這時資料庫還是空的，會收到「接下來 7 天還沒有收錄到活動」。

### 5-4 寫入假資料，再測一次

```bash
python -m shared.seed --table fomoless-dev-events
```

看到 `已寫入 16 筆範例活動` 後，到 LINE 再傳一次「本週活動」，應該會看到類似：

```
📅 接下來 7 天的活動：
• 11-10 10:00 115-1 學期院週會（範例）｜國際會議廳
  ⏱ 院週會 2h｜👥 已額滿
• 11-11 18:30 半導體產業實習說明會｜國鼎圖書館 B1 國際會議廳
  ⏱ 自我探索與生涯規劃 2h｜👥 64/200｜🎁 出席即提供晚餐
```

假資料的日期在 2026 年 11–12 月。看到的活動依測試當天而不同；12 月中以後測試的話，大部分假活動都已經過了。

### 5-5 出問題時

```bash
sam logs -n LineWebhookFunction --stack-name fomoless-dev --tail
```

在另一個視窗用手機傳訊息，這裡會即時顯示 Lambda 的日誌。

| 狀況 | 可能原因與解法 |
| --- | --- |
| Verify 失敗 | Parameter Store 的 channel secret 貼錯 → 用 `--overwrite` 重存；看 `sam logs` 有沒有 `Invalid LINE signature` |
| 日誌出現 `AccessDenied` 或 `ParameterNotFound` | 第 3-3 步的參數名稱打錯，名稱要完全一致 |
| 沒有任何回覆、日誌也沒動靜 | Use webhook 沒開，或 Webhook URL 貼錯 |
| 回覆兩則 | 第 5-2 步的自動回應訊息沒關 |
| 回覆 401 或 token 錯誤 | Channel access token 貼錯或已重新 Issue → 重存 |
| `sam build` 失敗 | Docker Desktop 沒開 |
| `sam deploy` 出現 ap-east-2 以外的區域 | `aws configure` 的區域要是 `ap-east-2` |

---

## 第 6 步：交棒給組員

### 6-1 發 PR

```bash
git add .
git status          # 確認清單裡沒有 env.json、.venv、.aws-sam
git commit -m "feat: M1 SAM 骨架與 LINE webhook"
git push -u origin feature/m1-skeleton
gh pr create --base develop --title "M1：SAM 骨架與 LINE webhook" \
  --body "已部署 dev 並用手機測試：歡迎訊息、Hello、本週活動皆正常。部署步驟見 docs/deploy-guide.md。"
```

### 6-2 審查與合併

- 指定 **士育** 審查（你不能核准自己的 PR）。
- 核准後按 **Squash and merge**。

### 6-3 組員接手後怎麼開發

| 誰 | 做什麼 |
| --- | --- |
| 士育 | 熟悉 `template.yaml`，之後新增爬蟲、推播等 Lambda |
| Gabe | 在 `backend/webhook/app.py` 開發 Flex Message、rich menu、更多指令 |
| 所有人 | 從 develop 開分支 → 本機 `pytest` → 發 PR；部署由第 7 步的自動流程處理 |

組員**不需要 AWS 帳號就能開發和跑測試**。要在本機執行 Lambda，可以用 `sam local invoke`（見 README）。

---

## 第 7 步：設定 GitHub Actions 自動部署

目標：合併到 `develop` 自動部署 dev，合併到 `main` 自動部署 prod。GitHub 透過 **OIDC** 向 AWS 換取臨時憑證，**不需要在 GitHub 存任何 AWS 金鑰**。

### 7-1 在 AWS 建立 GitHub 身分提供者（只做一次）

1. 用 gl-pupu 登入 → **IAM** → 左側 **Identity providers** → **Add provider**。
2. Provider type 選 **OpenID Connect**。
3. Provider URL 填 `https://token.actions.githubusercontent.com`。
4. Audience 填 `sts.amazonaws.com`。
5. **Add provider**。

### 7-2 建立部署角色

1. IAM → **Roles** → **Create role**。
2. Trusted entity type 選 **Web identity**。
3. Identity provider 選 `token.actions.githubusercontent.com`，Audience 選 `sts.amazonaws.com`。
4. GitHub organization 填 `fomoless-ncu`，GitHub repository 填 `Fomoless`（大小寫要和 repo 名稱完全一致），GitHub branch 先留空 → **Next**。
5. 權限勾選 `AdministratorAccess` → **Next**。
6. Role name 填 `FomoLess-GitHubDeploy` → **Create role**。

:::warning
**為什麼給 AdministratorAccess**：SAM 部署要建立 Lambda、API、資料表、IAM 角色等多種資源，精確列出所需權限很繁瑣。我們用下一步的**信任政策**限制「只有 Fomoless repo 的 development、production 環境能使用這個角色」，再加上 PR 審查，風險可控。專案穩定後，可以再改成只包含必要權限的政策。
:::

### 7-3 收緊信任政策

1. 打開剛建立的 `FomoLess-GitHubDeploy` → **Trust relationships** 分頁 → **Edit trust policy**。
2. 全部換成以下內容，把 `你的帳號ID` 換成 12 位數帳號 ID：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::你的帳號ID:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": [
            "repo:fomoless-ncu/Fomoless:environment:development",
            "repo:fomoless-ncu/Fomoless:environment:production"
          ]
        }
      }
    }
  ]
}
```

3. **Update policy**。
4. 回到角色的 **Summary**，複製 **ARN**，長得像 `arn:aws:iam::123456789012:role/FomoLess-GitHubDeploy`。

### 7-4 在 GitHub 建立部署環境

repo → **Settings** → 左側 **Environments** → **New environment**：

| 環境名稱 | Deployment branches and tags | Required reviewers |
| --- | --- | --- |
| `development` | Selected branches → 加入 `develop` | 不需要 |
| `production` | Selected branches → 加入 `main` | 勾選，加入你和一位備援 |

設定後只有 `develop` 能部署到 dev、只有 `main` 能部署到 prod，而且 prod 每次部署前都要有人按核准。

### 7-5 設定角色 ARN 變數

repo → **Settings** → **Secrets and variables** → **Actions** → **Variables** 分頁 → **New repository variable**：

| Name | Value |
| --- | --- |
| `AWS_DEPLOY_ROLE_ARN` | 第 7-3 步複製的 ARN |

### 7-6 測試自動部署

1. 隨便改一個小地方（例如 README 加一行），走一次 feature 分支 → PR → 合併到 develop。
2. 到 repo 的 **Actions** 分頁，會看到 `CI/CD` 執行：先跑 `test`，通過後跑 `deploy-dev`。
3. 全部綠燈後，到 LINE 傳「本週活動」確認 Bot 仍然正常。

| 狀況 | 解法 |
| --- | --- |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | 信任政策的 repo 名稱、環境名稱大小寫不一致，或帳號 ID 填錯 |
| `Credentials could not be loaded` | 沒有設定 `AWS_DEPLOY_ROLE_ARN` 變數 |
| build 很慢 | 正常：GitHub 機器要模擬 arm64 打包，約 5–10 分鐘 |

### 7-7 讓 PR 必須通過測試

repo → **Settings** → **Branches** → 編輯 `develop` 和 `main` 的規則 → 勾選 **Require status checks to pass before merging** → 搜尋並加入 `test` → **Save changes**。

之後測試沒通過的 PR 就不能合併。

---

## 之後：正式環境（prod）

期末 Demo 前要上線正式版時：

1. 建立正式的 LINE 官方帳號，取得金鑰，存到 `/fomoless/prod/line-channel-secret`、`/fomoless/prod/line-channel-access-token`。
2. 從 develop 發 PR 到 main → 合併 → 在 Actions 核准 `deploy-prod`。
3. 查詢 prod 的 Webhook URL（把第 4-3 步指令的 `fomoless-dev` 換成 `fomoless-prod`），設定到正式 LINE 帳號。

## 費用與清除

- 這次建立的資源都在免費額度內，或每月只有幾美分：Lambda、DynamoDB On-Demand、API Gateway、Parameter Store 標準參數、日誌保留 14 天。
- 要刪除 dev 環境：`sam delete --stack-name fomoless-dev`。資料表設了保留，不會被刪；真的要刪，到 DynamoDB 主控台手動刪除。
