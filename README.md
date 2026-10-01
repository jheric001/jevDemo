# Jev 教學示範：網頁搜尋助手 v2

這是一個教學用專案，目的是示範 TypeSafe 的 [Jev 決策模型](https://docs.typesafe.ai/introduction)可以怎麼用：它在瀏覽器代理裡負責什麼、適合和不適合做什麼，以及怎麼跟一般 LLM 分工。

> 本專案改寫自 [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast)（MIT 授權，Browser Use 團隊與 TypeSafe 合作），以 commit `1231850` 為基礎。原版的英文說明、效能量測與設計細節請看原專案 README 與 [docs/performance.md](docs/performance.md)。

---

## 一、先認識 Jev

Jev 是「用選的、不用生成的」決策模型。你給它一段狀態（文字或 JSON）和幾個有型別的問題，它一次呼叫就平行回答全部，每題都回傳選擇、機率分佈與信心值。

| 問題型別 | 回答什麼 | 例子 |
| --- | --- | --- |
| Choice | 從幾個選項裡選一個 | 這張工單該給哪個部門？下一步要點哪個按鈕？ |
| Noul | 某個屬性存在的機率（是非題） | 這則訊息急不急？這筆商品符不符合條件？ |
| Score | 落在有序量表上的分數 | 嚴重程度、相關性 |

學習重點：

- Jev 只能「選」，不能「寫」。要打字時，得另外交給一個小型文字模型產生字串。
- Jev 很快也很便宜（每次決策約兩、三百毫秒），適合放在需要大量、重複判斷的地方。
- 需要理解長篇中文、抽取欄位、寫報告這類工作，交給一般 LLM 比較合適。

不透過瀏覽器，直接呼叫 Jev API 的最小例子：

```bash
curl -X POST https://api.typesafe.ai/v1/systemone \
  -H "Authorization: Bearer $TYPESAFE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "state": "客戶來信：我的 Stripe 串接壞了三天，一直失敗，我在流失訂單，請盡快處理。",
    "model": "jev-latest",
    "questions": {
      "department": {"type": "choice", "instructions": "哪個團隊該處理",
        "criteria": {"billing": "付款或訂閱問題", "technical": "Bug 或串接問題", "sales": "價格或帳號問題"}},
      "is_urgent": {"type": "noul", "instructions": "這則訊息帶有急迫性"}
    }
  }'
```

也可以先到 [TypeSafe Playground](https://console.typesafe.ai/playground) 免寫程式試玩，或啟動本專案後打開 `/lab`（題型實驗室），直接貼文字試三種題型。

## 二、Jev 在這個專案裡被用在哪裡

| 用在哪裡 | 問題型別 | 問 Jev 什麼 | 程式位置 |
| --- | --- | --- | --- |
| 瀏覽器的每一步操作 | Choice | 一題選「做什麼」（點擊、打字、選下拉、捲動、等待、完成、卡住），另外每種操作各一題選「對哪個元素」，一次請求全部回答 | [model.py](jev_ultrafast/model.py)、[agent.py](jev_ultrafast/agent.py) |
| 首頁助手的站內搜尋 | Choice | 在網站的搜尋框輸入關鍵字並送出 | [assistant.py](jev_ultrafast/assistant.py) 的 `search_one` |
| 比價頁「純 Jev」挑商品 | Noul | 每筆商品各一題：符不符合品牌、尺寸等條件。每 15 筆合成一次請求，機率 0.5 以上才保留 | [compare.py](jev_ultrafast/compare.py) 的 `jev_pick` |

安全邊界沿用原版：模型的輸出永遠不會變成 CSS selector、座標、shell 指令或 JavaScript，只能從程式實際讀到的元素編號裡選。

## 三、跟原版相比，我改了什麼

### 1. 繁體中文任務台（`/console`）

- 中文任務先翻成一句英文指令再交給 Jev，關鍵字、品牌、網站名保留原文。
- 預設常用網站（momo、PChome、酷澎、Google、Google 航班、維基百科），也可以自訂網址。
- 用量面板：Jev 的 token 用量與費用估算、OpenRouter 即時餘額、每次任務的花費。
- 介面文字、狀態與錯誤訊息全部中文化。

### 2. 網頁搜尋助手（首頁 `/`）

原版只有 Jev，遇到「只要某品牌某尺寸」「每一頁都收錄」這類需要判斷的任務，常會選錯或停下。v2 改成三層分工，讓 Jev 只做它擅長的互動：

| 層 | 誰負責 | 做什麼 |
| --- | --- | --- |
| 規劃 | 規劃模型（預設 Claude Sonnet 5） | 讀懂中文任務、拆成步驟、定義篩選條件與收錄欄位，先給你確認 |
| 執行 | Jev ＋ 程式 | Jev 在搜尋框打字並送出；開網址、捲動、翻頁、逐一開啟由程式做 |
| 判斷與收錄 | 判斷模型（預設 Gemini 2.5 Flash） | 從結果頁挑出符合條件的候選、把每頁抽成結構化欄位、寫比較報告 |

其他功能：

- 一次搜尋多個網站，報告做成跨站比較表。
- 規劃模型先產生 3 到 5 組關鍵字；第一輪搜完，再從商品標題學出正式品名追搜一輪。
- 結果頁會捲動到底並翻頁，候選以「網域＋路徑＋商品編號」去重。
- 每次任務自動寫一份報告到 `reports/`，介面的「歷史報告」可以直接打開。
- 收錄結果可匯出 CSV、JSON，每筆附截圖。

### 3. 三種模式比價（`/compare`）

同一個比價任務用三種做法各跑一次，並列比較時間、費用與挑選結果。這頁最適合在課堂上說明 Jev 的定位：

| 步驟 | 純 LLM | LLM + Jev | 純 Jev |
| --- | --- | --- | --- |
| 理解任務 | LLM | LLM | 程式（抓「」裡的關鍵字） |
| 網站搜尋 | LLM | Jev | Jev |
| 讀取結果 | 程式 | 程式 | 程式 |
| 挑選商品 | LLM | Jev | Jev |
| 比價整理 | LLM | LLM | 程式 |

每一步都記錄誰負責、花了多少時間，以及其中有多少時間花在模型本身，用來區分「模型慢」和「網站慢」。

### 4. 首頁搜尋固定由 Jev 執行（2026-10-01）

首頁助手的「網站搜尋」步驟一律由 Jev 在站內搜尋框打字並送出，不再用搜尋網址樣板，原本的切換勾選框也已移除。第 2 頁以後的翻頁仍由程式直接開網址。

### 5. 題型實驗室（`/lab`，2026-10-01）

貼一段文字、設定幾個 Choice／Noul／Score 問題，一次送給 Jev，結果以長條圖顯示機率與信心值。不用寫程式，也不用登入 TypeSafe 主控台，金鑰只留在本機伺服器。

- 內建 8 題題庫：5 題正常題（客服分流、美業 LINE 訊息、商品評論、社群公關風險、履歷初篩）、3 題陷阱題（算數、日期比較、夾帶指令），多數附變化版。
- 改一句話再跑，右邊會並排顯示上一次的結果，方便看數字怎麼動。
- 題目、課堂帶法與實測數字見 [docs/jev-題型Demo題庫-v2.md](docs/jev-題型Demo題庫-v2.md)。
- 這頁的呼叫不會記進用量面板。

### 6. 對原版核心程式的小修改

- [agent.py](jev_ultrafast/agent.py)：決策與打字可以替換成其他實作，讓比價頁能用 LLM 跑同一個迴圈。
- [model.py](jev_ultrafast/model.py)：把「組問題」和「解讀答案」拆成兩個函式，Jev 與 LLM 共用；文字模型出錯時，錯誤訊息會帶出供應商回傳的原因。
- [questions.py](jev_ultrafast/questions.py)：明定日期欄位只填日期、地點欄位只填地點。
- [browser.py](jev_ultrafast/browser.py)：使用者手動關掉分頁時不再報錯。

新增的檔案：

| 檔案 | 用途 |
| --- | --- |
| `assistant.py`、`planner.py`、`capture.py`、`archive.py` | 網頁搜尋助手：工作流程、規劃、頁面讀取與欄位抽取、報告存檔 |
| `compare.py` | 三種模式比價 |
| `llm.py` | 規劃、判斷模型的連線（OpenRouter） |
| `sites.py` | 網站清單與搜尋網址樣板 |
| `translate.py` | 中文任務翻譯成英文指令 |
| `usage.py` | 用量帳本與費用估算 |
| `static/assistant.*`、`static/compare.*`、`static/zh.*`、`static/lab.*` | 四個中文介面 |
| `static/lab-items.json` | 題型實驗室的題庫資料 |
| `tests/test_assistant.py`、`test_compare.py`、`test_zh.py`、`test_lab.py` | 離線測試 |
| `docs/assistant.md`、`docs/zh-tw.md` | 中文說明 |
| `docs/jev-題型Demo題庫-v2.md` | 題型實驗室的題目、課堂帶法與實測數字 |

## 四、安裝與啟動

需要 Python 3.12 以上、Chrome，以及兩把金鑰：

- TypeSafe 金鑰：<https://console.typesafe.ai/keys>
- OpenRouter 金鑰：<https://openrouter.ai>（文字模型、規劃模型、判斷模型共用這一把）

```bash
git clone https://github.com/jheric001/jevDemo.git
cd jevDemo
uv sync
cp .env.example .env
# 打開 .env，填入 TYPESAFE_API_KEY 與 TEXT_MODEL_API_KEY
uv run jev
```

沒有安裝 [uv](https://docs.astral.sh/uv/) 的話：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/jev
```

啟動後打開 <http://127.0.0.1:8767>。

第一次使用要讓 Chrome 開放連線：在 Chrome 打開 `chrome://inspect/#remote-debugging`，勾選「Allow remote debugging for this browser instance」。連不上時執行 `uv run browser-harness --doctor` 診斷。

`.env` 建議設定：

- `TEXT_MODEL=google/gemini-2.5-flash-lite`：原版預設的 `inception/mercury-2.5` 在 OpenRouter 上曾間歇性失敗（2026-09-23 本機實測五次失敗兩次）。
- `PLANNER_MODEL`、`JUDGE_MODEL`：可以換成其他 OpenRouter 上的模型，不設定就用預設值。
- 其他選項請看 [.env.example](.env.example)。

## 五、頁面一覽

| 網址 | 內容 |
| --- | --- |
| `/` | 網頁搜尋助手（v2 主要功能） |
| `/compare` | 三種模式比價 |
| `/console` | 單步任務台：一句中文任務，直接交給 Jev 操作 |
| `/lab` | 題型實驗室：貼一段文字，看 Jev 用 Choice、Noul、Score 怎麼回答 |
| `/en` | 原版英文 inspector，可以看到每一步的元素編號、操作機率與目標機率 |

## 六、使用前務必知道

- 它會用你目前登入的 Chrome 帳號操作，等於用你的身分點擊。建議先用獨立的 Chrome profile 測試。
- Jev 說「完成」不等於真的完成，正式使用要另外寫程式驗證結果。
- 目標必須是做得到的。例如日期已經過期，Jev 不會說卡住，而是一直嘗試，直到把決策次數用完。
- 原版是 MVP，不支援 Shadow DOM、iframe、canvas、檔案上傳、彈出新分頁、巢狀捲動。很多網站的篩選對話框（例如 Google 航班的轉機次數）讀不到選項。
- Jev 以英文為主要訓練語言，所以中文任務會先翻成英文指令再交給它。
- 伺服器重啟後要重新整理瀏覽器分頁，舊分頁的 token 會失效；網址只能用 `127.0.0.1`，不能用 `localhost`。

## 七、費用參考

- Jev 每百萬輸入 token 0.042 美元，輸出不收費（[TypeSafe 模型與價格](https://docs.typesafe.ai/models)，2026-09-23 查詢）。
- 2026-09-24 本機實測（當時搜尋使用網址樣板）：在 momo 與 PChome 搜尋「水凝尿布M」，收錄 6 筆並產出比較報告，共 169 秒、0.062 美元；台北到檀香山的來回機票查詢，共 50 秒、約 0.024 美元。

## 延伸閱讀

- 原專案：<https://github.com/browser-use/jev-ultrafast>
- Browser Harness：<https://github.com/browser-use/browser-harness>
- TypeSafe 文件：<https://docs.typesafe.ai/introduction>
- Jev 1.13 已知弱點：<https://docs.typesafe.ai/model-jaggedness/jev-1.13>

## 授權

MIT。原始著作權屬於 Browser Use（見 [LICENSE](LICENSE)），本專案的修改部分同樣以 MIT 授權釋出。
