# Gemini 解題工具（GeminiMCP）

讓 Claude（Cowork／Claude Code）把數學題轉交給 Google Gemini：數理推理強，也能節省 Claude 的 token。
整個工具就是一個 `gemini_mcp.py`，只用 Python 標準庫，不需要 pip 安裝任何東西。

它有兩種執行方式，但都是同一個檔案：

| 使用端 | 呼叫方式 | 範圍 |
|---|---|---|
| **Cowork** | Claude 在沙盒裡執行 `python3 gemini-tool/gemini_mcp.py solve "..."` | 只有選了這個資料夾的 Cowork 對話才能用 |
| **Claude Code**（Code 分頁） | 專案根目錄的 `.mcp.json` 把它當成 MCP server 載入 | 只有在這個資料夾開的 session 才有 |

兩種方式都**不需要**改 `claude_desktop_config.json`，所以不會出現在 Chat 或其他專案裡。
而且設定跟著資料夾走，不論換哪個 Claude 帳號、哪個 Claude 視窗（`--user-data-dir` 多開）都能用。

---

## 在新專案裡安裝

```bash
cd 你的專案資料夾
git clone https://github.com/7nnyxu/GeminiMCP.git gemini-tool
copy gemini-tool\.env.example gemini-tool\.env
```

1. 在 `gemini-tool/.env` 填入金鑰（金鑰取得方式見下方）。
2. 測試：`python gemini-tool/gemini_mcp.py check`，看到 `2` 就代表成功。
3. 在專案根目錄建立下面兩個檔案。

**`.mcp.json`**（給 Claude Code）：
```json
{
  "mcpServers": {
    "gemini": { "command": "python", "args": ["gemini-tool/gemini_mcp.py"] }
  }
}
```

**`CLAUDE.md`**：複製 [下方「給 Claude 的指示」](#給-claude-的指示範本)。

要讓 Claude Code 不必每次詢問就啟用這個 server，可以建立 `.claude/settings.local.json`：
```json
{ "enabledMcpjsonServers": ["gemini"] }
```

---

## 每個 Claude 帳號要做一次：開放 Cowork 沙盒連到 Google

Cowork 沙盒預設只能連到套件管理網站（npm、pypi、github…），連不到 Gemini。
每個要用 Cowork 解題的 Claude 帳號都要做一次：

1. Claude Desktop → **設定（Settings）→ 功能（Capabilities）**
2. 找到「程式碼執行與檔案建立」底下的 **網路存取／允許的網域**（Network egress / Allowed domains）
3. 在「其他允許的網域」加入：
   - `aiplatform.googleapis.com`（Vertex AI 金鑰）
   - `generativelanguage.googleapis.com`（AI Studio 金鑰，有用到才加）
4. 開一個**新的** Cowork 對話（舊對話不會套用新設定）

這個設定只是讓沙盒「連得到」Google；沒有金鑰和這支程式的專案，什麼事也不會發生。

---

## 金鑰

### Vertex AI 金鑰（綁定服務帳號，用 gcloud 建立）

```bash
PROJECT=你的專案ID
ACCOUNT=你的Google帳號
gcloud services enable aiplatform.googleapis.com apikeys.googleapis.com --project=$PROJECT --account=$ACCOUNT
gcloud iam service-accounts create gemini-mcp --display-name="Gemini MCP" --project=$PROJECT --account=$ACCOUNT
gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:gemini-mcp@$PROJECT.iam.gserviceaccount.com" --role="roles/aiplatform.user" --condition=None --account=$ACCOUNT
gcloud beta services api-keys create --display-name="gemini-mcp" --api-target=service=aiplatform.googleapis.com --service-account="gemini-mcp@$PROJECT.iam.gserviceaccount.com" --project=$PROJECT --account=$ACCOUNT
gcloud services api-keys get-key-string <上一步輸出的 key 名稱> --account=$ACCOUNT
```

`.env` 要設定成 `GEMINI_BACKEND=vertex`、`GOOGLE_CLOUD_PROJECT=$PROJECT`，以及 `GEMINI_API_KEY=`（填上一步的金鑰）。

### AI Studio 金鑰（最簡單）

到 <https://aistudio.google.com/apikey> 建立，`.env` 設定成 `GEMINI_BACKEND=gemini`。

---

## 給 Claude 的指示（範本）

```markdown
## 解數學題：先交給 Gemini

本專案的 `gemini-tool/` 可以把題目轉交給 Google Gemini。為了節省 token，解數學題時：

1. 先用 Gemini 取得答案與關鍵步驟，不要自己從頭長篇推導。
   - **Claude Code**：呼叫 MCP 工具 `gemini_solve`（有圖就放進 `files`；回傳 job_id 時用 `gemini_result` 取回）。
   - **Cowork**：在沙盒執行（timeout 設 600000 毫秒）
     `python3 gemini-tool/gemini_mcp.py solve "完整題目" -f 題目圖.png`
     題目很長或含引號時，先寫進暫存檔再執行 `python3 gemini-tool/gemini_mcp.py solve - < 題目.txt`。
2. 題目文字要完整轉述（所有條件、選項、選填格式）。
3. 拿到答案後做合理性檢查（代回、估算、排除選項）。若把握度低，或答案和你的判斷矛盾，
   就用 `--model pro`（MCP：`model: "pro"`）或 `--code`（MCP：`use_code: true`）再問一次。
4. 要寫給學生看的完整詳解時，加上 `--full`（MCP：`detail: "full"`）。
5. 如果出現「連不上 Gemini API」，請提醒使用者到 設定 → 功能 開放 `aiplatform.googleapis.com`。
```

---

## 命令列用法

```bash
python gemini_mcp.py check                                   # 測試金鑰
python gemini_mcp.py models                                  # 列出可用模型
python gemini_mcp.py solve "設 x^2-5x+6=0，求兩根和" --full   # 完整詳解
python gemini_mcp.py solve "見附圖第 3 題" -f 題目/113數A.png --model pro
python gemini_mcp.py solve - < 題目.txt                       # 從檔案讀題目
python gemini_mcp.py ask "這段解法哪裡錯了？" -f 我的解法.md
```

## MCP 工具（Claude Code 端）

| 工具 | 用途 |
|---|---|
| `gemini_solve` | 解數學題。預設只回【答案】【關鍵步驟】【把握度】；`detail: "full"` 會給完整詳解 |
| `gemini_ask` | 問 Gemini 任何事，例如檢查解法、出類題、讀圖 |
| `gemini_result` | Gemini 算超過 45 秒時，前兩個工具會先回傳 `job_id`，再用這個工具取回 |

## 設定項目（`.env`）

| 名稱 | 預設 | 說明 |
|---|---|---|
| `GEMINI_API_KEY` | （必填） | |
| `GEMINI_BACKEND` | `gemini` | `vertex`＝Vertex AI 金鑰；`gemini`＝AI Studio 金鑰 |
| `GOOGLE_CLOUD_PROJECT` | | 使用 vertex 時填 GCP 專案 ID |
| `GOOGLE_CLOUD_LOCATION` | `global` | 使用 vertex 時的區域 |
| `GEMINI_MODEL` | `gemini-3.8-flash` | 預設模型 |
| `GEMINI_PRO_MODEL` | `gemini-3.1-pro-preview` | `--model pro` 時使用 |
| `GEMINI_THINKING_LEVEL` | `high` | `minimal` / `low` / `medium` / `high`，`high` 是最高等級（見下方說明） |
| `GEMINI_WAIT_SECONDS` | `45` | MCP 單次呼叫最多等幾秒，超過改回傳 `job_id` |

> **關於思考等級**（2026-10 在 Vertex AI 上以 gemini-3.8-flash 實測）：API 也接受 `max`，但同一題的思考 token
> 大約與「不設定」相同（約 900～1400），`high` 則是 1900～4100，所以 `high` 才是真正的最高等級。`xhigh`、`ultra` 會被拒絕。

題目檔案只會從專案資料夾（`gemini-tool/` 的上一層）讀取，支援 png/jpg/webp/heic/pdf/txt/md。
Cowork 沙盒路徑（`/sessions/.../mnt/<資料夾>/...`）會自動對應回本機路徑。

## 備選做法：在 Claude Desktop 全域註冊 MCP

如果真的想讓某個 Claude 視窗的 **Chat 和所有 Cowork 對話**都看到這些工具（注意：這樣就不再限定於單一專案），
可以在該視窗的資料夾裡編輯 `claude_desktop_config.json`：

- 一般啟動：`%APPDATA%\Claude\claude_desktop_config.json`
- 用 `--user-data-dir="<profile 資料夾>"` 多開的：`<profile 資料夾>\claude_desktop_config.json`

加入以下內容（路徑換成你的），然後從系統匣**完全結束**該視窗再重開：
```json
"mcpServers": {
  "gemini": {
    "command": "C:\\Program Files\\Python313\\python.exe",
    "args": ["D:\\你的專案\\gemini-tool\\gemini_mcp.py"],
    "env": { "PYTHONUTF8": "1" }
  }
}
```
紀錄檔在同一個資料夾的 `logs\mcp-server-gemini.log`。

## 授權

MIT，詳見 [LICENSE](LICENSE)。
