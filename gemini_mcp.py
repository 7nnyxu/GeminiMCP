#!/usr/bin/env python3
"""Gemini 解題工具 —— 讓 Claude（Cowork / Claude Code）把數學題轉交給 Gemini。

只用 Python 標準庫，不需要 pip install 任何套件。

兩種用法：
  1. MCP server：由 .mcp.json 或 claude_desktop_config.json 啟動，不帶參數。
  2. 命令列（Cowork 沙盒裡用這個；也可以自己測試）：
       python gemini_mcp.py check
       python gemini_mcp.py models
       python gemini_mcp.py solve "題目文字" [-f 題目.png] [--full] [--model pro] [--code]
       python gemini_mcp.py solve - < 題目.txt        # 用 - 從 stdin 讀題目
       python gemini_mcp.py ask "任意問題" [-f 檔案] [--system "..."] [--model pro]

設定（環境變數優先，其次讀同資料夾的 .env）：
  GEMINI_API_KEY         必填
  GEMINI_BACKEND         gemini（AI Studio 金鑰，預設）或 vertex（Vertex AI 金鑰）
  GOOGLE_CLOUD_PROJECT   vertex 用：GCP 專案 ID
  GOOGLE_CLOUD_LOCATION  vertex 用：區域，預設 global
  GEMINI_MODEL           預設模型，預設 gemini-3.8-flash
  GEMINI_PRO_MODEL       model="pro" 時使用的模型，預設 gemini-3.1-pro-preview
  GEMINI_THINKING_LEVEL  思考等級 minimal/low/medium/high，預設 high（最高）
  GEMINI_WORKSPACE       允許讀取題目檔案的資料夾（多個用 ; 分隔），預設為本工具的上一層資料夾
  GEMINI_WAIT_SECONDS    MCP 模式單次呼叫最多等幾秒，超過先回傳 job_id（避開逾時），預設 45
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
API_BASE = "https://generativelanguage.googleapis.com/v1beta"
SERVER_NAME = "gemini-solver"
SERVER_VERSION = "1.1.0"
DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_PRO_MODEL = "gemini-3.1-pro-preview"
DEFAULT_THINKING_LEVEL = "high"

MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/plain",
}
# inline 請求總量上限約 20MB，base64 會膨脹 4/3 倍，所以原始檔案合計抓 14MB
MAX_INLINE_BYTES = 14 * 1024 * 1024

SOLVE_BRIEF = """你是台灣高中數學解題專家，熟悉學測數學A／B與分科測驗數學甲的題型與命題慣例。
請仔細解題，得到答案後用另一種方法或代回原題再確認一次。
只輸出以下三段（繁體中文，數學式用 LaTeX 的 $...$）：
【答案】最終答案。單選寫選項代號與數值；多選列出全部正確選項；選填題依題目格式寫出每一格。
【關鍵步驟】3～8 行，只寫必要推導，不要寒暄、不要重述題目。
【把握度】高／中／低。若題意有歧義、資訊不足或圖形看不清楚，用一句話說明。"""

SOLVE_FULL = """你是台灣高中數學名師，熟悉學測數學A／B與分科測驗數學甲的題型與命題慣例。
請仔細解題並驗算，再寫成給高三學生看的詳解（繁體中文，數學式用 LaTeX 的 $...$）：
【答案】最終答案（多選列出全部正確選項；選填題依題目格式寫出每一格）。
【詳解】分步驟推導，每一步說明理由；選擇題要逐一說明各選項對錯。
【觀念】本題考的核心觀念或公式，1～3 點。
【易錯點】學生常見的錯誤，1～2 點。
【把握度】高／中／低；若題意有歧義或資訊不足請說明。"""


class GeminiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def log(message: str) -> None:
    # MCP 模式下 stdout 是協定通道，所有紀錄一律寫 stderr（Claude Desktop 會存進 mcp-server-*.log）
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 設定

def read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    path = HERE / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("\"'")
    return values


def setting(name: str, default: str = "") -> str:
    # 每次重讀 .env，改了金鑰或模型不用重啟 Claude Desktop
    return os.environ.get(name) or read_env_file().get(name) or default


def resolve_model(name: str | None) -> str:
    name = (name or "").strip()
    if not name or name.lower() == "flash":
        return setting("GEMINI_MODEL", DEFAULT_MODEL)
    if name.lower() == "pro":
        return setting("GEMINI_PRO_MODEL", DEFAULT_PRO_MODEL)
    return name.removeprefix("models/")


def use_vertex() -> bool:
    return setting("GEMINI_BACKEND", "gemini").lower() == "vertex"


def vertex_base() -> str:
    location = setting("GOOGLE_CLOUD_LOCATION", "global")
    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    return f"https://{host}"


def generate_url(model: str) -> str:
    if not use_vertex():
        return f"{API_BASE}/models/{model}:generateContent"
    project = setting("GOOGLE_CLOUD_PROJECT")
    location = setting("GOOGLE_CLOUD_LOCATION", "global")
    scope = f"projects/{project}/locations/{location}/" if project else ""
    return f"{vertex_base()}/v1/{scope}publishers/google/models/{model}:generateContent"


def workspace_roots() -> list[Path]:
    raw = setting("GEMINI_WORKSPACE")
    roots = [Path(p) for p in raw.split(os.pathsep) if p.strip()] if raw else [HERE.parent]
    return [root.resolve() for root in roots]


def resolve_path(raw: str) -> Path:
    """找出題目檔案在本機的位置。

    Cowork 在沙盒裡看到的路徑（例如 /sessions/xxx/mnt/我的專案/題目/1.png）
    和本機路徑不同，所以逐段去掉開頭，嘗試接在工作資料夾底下。
    只允許讀工作資料夾內、且副檔名在 MIME_TYPES 裡的檔案。
    """
    roots = workspace_roots()
    pieces = [p for p in re.split(r"[\\/]+", raw.strip()) if p not in ("", ".")]
    candidates = [Path(raw.strip())]
    for root in roots:
        candidates += [root.joinpath(*pieces[i:]) for i in range(len(pieces))]
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        if candidate.is_file() and any(candidate.is_relative_to(root) for root in roots):
            if candidate.suffix.lower() not in MIME_TYPES:
                raise GeminiError(f"不支援的檔案類型：{candidate.name}（支援 {' '.join(MIME_TYPES)}）")
            return candidate
    raise GeminiError(
        f"找不到檔案：{raw}\n只會在這些資料夾內尋找：{'; '.join(map(str, roots))}"
    )


# ---------------------------------------------------------------- Gemini API

def api_request(url: str, body: dict | None = None, timeout: float = 600) -> dict:
    api_key = setting("GEMINI_API_KEY")
    if not api_key:
        raise GeminiError(
            f"找不到 GEMINI_API_KEY。請把 .env.example 複製成 {HERE / '.env'} 並填入金鑰。"
        )
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    for attempt in range(3):
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                pass
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                log(f"HTTP {e.code}，{5 * (attempt + 1)} 秒後重試：{detail[:200]}")
                time.sleep(5 * (attempt + 1))
                continue
            raise GeminiError(f"Gemini API 錯誤 {e.code}：{detail}", status=e.code) from None
        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(3)
                continue
            raise GeminiError(f"連不上 Gemini API：{e.reason}") from None
        except TimeoutError:
            raise GeminiError(f"Gemini API 超過 {timeout:.0f} 秒沒有回應") from None
    raise GeminiError("Gemini API 重試多次仍失敗")


def call_gemini(
    prompt: str,
    files: list[str] | tuple[str, ...] = (),
    system: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    use_code: bool = False,
) -> str:
    model = resolve_model(model)
    thinking = thinking or setting("GEMINI_THINKING_LEVEL", DEFAULT_THINKING_LEVEL)
    parts: list[dict] = []
    total = 0
    for raw in files:
        path = resolve_path(raw)
        content = path.read_bytes()
        total += len(content)
        if total > MAX_INLINE_BYTES:
            raise GeminiError("附加檔案合計超過 14MB，請裁切圖片或只附需要的頁面。")
        parts.append({"inlineData": {
            "mimeType": MIME_TYPES[path.suffix.lower()],
            "data": base64.b64encode(content).decode("ascii"),
        }})
    parts.append({"text": prompt})  # 官方建議圖片放在文字前面

    body: dict = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if thinking:
        body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": thinking}
    if use_code:
        body["tools"] = [{"codeExecution": {}}]

    url = generate_url(model)
    started = time.monotonic()
    try:
        data = api_request(url, body)
    except GeminiError as e:
        # 舊模型（例如 2.5 系列）不認得 thinkingLevel，拿掉再試一次
        if not (thinking and e.status == 400 and "thinking" in str(e).lower()):
            raise
        del body["generationConfig"]["thinkingConfig"]
        data = api_request(url, body)
    return format_response(data, model, time.monotonic() - started)


def format_response(data: dict, model: str, elapsed: float) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "未知原因")
        raise GeminiError(f"Gemini 沒有回傳內容（{reason}）")
    candidate = candidates[0]
    texts: list[str] = []
    ran_code = False
    for part in (candidate.get("content") or {}).get("parts") or []:
        if part.get("thought"):
            continue
        if part.get("text", "").strip():
            texts.append(part["text"].strip())
        elif "executableCode" in part:
            ran_code = True
    answer = "\n\n".join(texts) or "（Gemini 回傳了空白內容）"

    notes = []
    finish = candidate.get("finishReason", "STOP")
    if finish not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
        notes.append(f"⚠ 結束原因：{finish}（答案可能不完整）")
    if ran_code:
        notes.append("Gemini 有執行 Python 驗算")
    usage = data.get("usageMetadata") or {}
    notes.append(
        f"{model}｜輸入 {usage.get('promptTokenCount', 0)}、"
        f"思考 {usage.get('thoughtsTokenCount', 0)}、"
        f"輸出 {usage.get('candidatesTokenCount', 0)} tokens｜{elapsed:.0f} 秒"
    )
    return answer + "\n\n---\n" + "\n".join(notes)


def list_models() -> str:
    if use_vertex():
        data = api_request(f"{vertex_base()}/v1beta1/publishers/google/models?pageSize=1000")
        names = sorted(
            m["name"].rsplit("/", 1)[-1]
            for m in data.get("publisherModels", [])
            if "gemini" in m.get("name", "")
        )
    else:
        data = api_request(f"{API_BASE}/models?pageSize=1000")
        names = sorted(
            m["name"].removeprefix("models/")
            for m in data.get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        )
    return "\n".join(names)


# ---------------------------------------------------------------- MCP server

def as_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


FILES_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "description": "題目圖片或 PDF 的路徑：專案資料夾內的相對路徑，或你在沙盒裡看到的完整路徑都可以。"
                   "支援 png/jpg/webp/heic/pdf/txt/md。",
}
MODEL_SCHEMA = {
    "type": "string",
    "description": "留空＝預設（flash，快）；難題填 \"pro\"；也可以直接填完整模型名稱。",
}

TOOLS = [
    {
        "name": "gemini_solve",
        "description": (
            "把數學題交給 Google Gemini 解（數理推理強，而且不佔用你的 token）。"
            "回傳【答案】【關鍵步驟】【把握度】。題目文字請完整轉述（所有條件、選項、選填格式），"
            "有圖就用 files 附上圖檔。拿到答案後請做合理性檢查；把握度低或和你的判斷矛盾時，"
            "改用 model=\"pro\" 或 use_code=true 再問一次。"
            "若回傳內容含 job_id，代表 Gemini 還在算，請呼叫 gemini_result 取回。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem": {"type": "string", "description": "完整題目文字（若題目全在圖裡，可寫「見附圖第 N 題」）。"},
                "files": FILES_SCHEMA,
                "detail": {
                    "type": "string",
                    "enum": ["brief", "full"],
                    "default": "brief",
                    "description": "brief＝只要答案與關鍵步驟（省 token，預設）；full＝給學生看的完整詳解。",
                },
                "model": MODEL_SCHEMA,
                "use_code": {
                    "type": "boolean",
                    "default": False,
                    "description": "讓 Gemini 執行 Python 驗算，適合計算繁重的題目（會比較慢）。",
                },
            },
            "required": ["problem"],
        },
    },
    {
        "name": "gemini_ask",
        "description": (
            "向 Google Gemini 提出任意問題，例如檢查一段解法、出類題、解釋觀念、讀圖。"
            "若回傳內容含 job_id，請呼叫 gemini_result 取回。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "要問 Gemini 的內容。"},
                "files": FILES_SCHEMA,
                "system": {"type": "string", "description": "（選填）給 Gemini 的角色設定或輸出格式要求。"},
                "model": MODEL_SCHEMA,
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "gemini_result",
        "description": "取回之前還在計算中的 Gemini 結果（gemini_solve / gemini_ask 回傳 job_id 時使用）。",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
]

SERVER_INSTRUCTIONS = (
    "這個 server 把問題轉交給 Google Gemini。遇到數學題時先用 gemini_solve 取得答案與關鍵步驟，"
    "再自行檢查合理性，不需要自己從頭長篇推導。"
)


class Jobs:
    """Gemini 可能思考超過一分鐘，但 MCP 用戶端通常 60 秒就逾時；
    所以每次最多等 GEMINI_WAIT_SECONDS 秒，沒算完就先回傳 job_id，之後用 gemini_result 取回。"""

    def __init__(self) -> None:
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="gemini")
        self.futures: dict[str, concurrent.futures.Future] = {}

    def start(self, fn, *args, **kwargs) -> str:
        job_id = uuid.uuid4().hex[:8]
        self.futures[job_id] = self.pool.submit(fn, *args, **kwargs)
        return self.wait(job_id)

    def wait(self, job_id: str) -> str:
        future = self.futures.get(job_id)
        if future is None:
            raise GeminiError(f"沒有這個 job_id：{job_id}（可能已經取回過了）")
        concurrent.futures.wait([future], timeout=float(setting("GEMINI_WAIT_SECONDS", "45")))
        if not future.done():
            return (f"Gemini 還在計算中（job_id={job_id}）。"
                    f"請呼叫 gemini_result 並帶入 job_id=\"{job_id}\" 取回答案。")
        del self.futures[job_id]
        return future.result()


def run_tool(jobs: Jobs, name: str, args: dict) -> str:
    if name == "gemini_result":
        return jobs.wait(str(args.get("job_id", "")).strip())
    if name == "gemini_solve":
        problem = str(args.get("problem") or "").strip() or "請解附圖中的題目。"
        system = SOLVE_FULL if args.get("detail") == "full" else SOLVE_BRIEF
        return jobs.start(call_gemini, problem, files=as_list(args.get("files")), system=system,
                          model=args.get("model"), use_code=bool(args.get("use_code")))
    if name == "gemini_ask":
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise GeminiError("請提供 prompt。")
        return jobs.start(call_gemini, prompt, files=as_list(args.get("files")),
                          system=args.get("system") or None, model=args.get("model"))
    raise GeminiError(f"未知的工具：{name}")


def serve() -> None:
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # 任何誤用的 print 都不會污染協定通道
    write_lock = threading.Lock()
    jobs = Jobs()

    def send(message: dict) -> None:
        data = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        with write_lock:
            out.write(data)
            out.flush()

    def handle_call(request_id, params: dict) -> None:
        name = params.get("name", "")
        started = time.monotonic()
        try:
            text, is_error = run_tool(jobs, name, params.get("arguments") or {}), False
        except GeminiError as e:
            text, is_error = str(e), True
        except Exception as e:  # noqa: BLE001 —— 任何錯誤都要回報給 Cowork，不能讓 server 掛掉
            text, is_error = f"工具內部錯誤：{e!r}", True
        log(f"{name} {'失敗' if is_error else '完成'}（{time.monotonic() - started:.0f} 秒）"
            + (f"：{text[:300]}" if is_error else ""))
        send({"jsonrpc": "2.0", "id": request_id,
              "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})

    log(f"{SERVER_NAME} {SERVER_VERSION} 啟動，預設模型 {resolve_model(None)}，"
        f"金鑰{'已設定' if setting('GEMINI_API_KEY') else '未設定'}")
    workers: list[threading.Thread] = []
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log(f"無法解析的訊息：{line[:200]!r}")
            continue
        if not isinstance(message, dict):
            continue
        method, request_id = message.get("method"), message.get("id")
        if method is None or request_id is None:
            continue  # notification 或用戶端的回應，不需要回覆
        params = message.get("params") or {}

        if method == "tools/call":
            worker = threading.Thread(target=handle_call, args=(request_id, params), daemon=True)
            worker.start()
            workers = [w for w in workers if w.is_alive()] + [worker]
            continue
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": SERVER_INSTRUCTIONS,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        elif method == "prompts/list":
            result = {"prompts": []}
        else:
            send({"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": -32601, "message": f"Method not found: {method}"}})
            continue
        send({"jsonrpc": "2.0", "id": request_id, "result": result})

    # stdin 關閉＝用戶端離開：給進行中的呼叫幾秒送出回覆，然後直接結束，不等還在跑的 Gemini 請求
    deadline = time.monotonic() + 5
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))
    log("用戶端已離開，結束")
    os._exit(0)


# ---------------------------------------------------------------- 命令列

def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        serve()
        return

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # 避免 Windows 主控台亂碼

    parser = argparse.ArgumentParser(description="Gemini 解題工具（不帶參數執行＝MCP server）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="確認金鑰與預設模型可以用")
    sub.add_parser("models", help="列出可用的 Gemini 模型")
    p = sub.add_parser("solve", help="解一題數學題")
    p.add_argument("problem", help="題目文字；填 - 代表從 stdin 讀")
    p.add_argument("-f", "--file", action="append", default=[], help="題目圖片或 PDF，可重複")
    p.add_argument("--full", action="store_true", help="輸出完整詳解")
    p.add_argument("--model", help="flash / pro / 完整模型名稱")
    p.add_argument("--code", action="store_true", help="讓 Gemini 執行 Python 驗算")
    p = sub.add_parser("ask", help="問 Gemini 任意問題")
    p.add_argument("prompt", help="問題；填 - 代表從 stdin 讀")
    p.add_argument("-f", "--file", action="append", default=[], help="附加檔案，可重複")
    p.add_argument("--system", help="角色設定或輸出格式要求")
    p.add_argument("--model", help="flash / pro / 完整模型名稱")
    args = parser.parse_args(argv)

    try:
        if args.command == "models":
            print(list_models())
        elif args.command == "check":
            print(call_gemini("1+1=? 只回答一個數字。", thinking="low"))
        elif args.command == "solve":
            problem = sys.stdin.read() if args.problem == "-" else args.problem
            print(call_gemini(problem, files=args.file, system=SOLVE_FULL if args.full else SOLVE_BRIEF,
                              model=args.model, use_code=args.code))
        elif args.command == "ask":
            prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
            print(call_gemini(prompt, files=args.file, system=args.system, model=args.model))
    except GeminiError as e:
        print(f"錯誤：{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
