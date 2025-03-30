import os
import logging
import requests
import time
from threading import Thread, Lock

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# 引入 v3 Messaging API 所需的模組
from linebot.v3.messaging import ApiClient
from linebot.v3.messaging.configuration import Configuration as MessagingConfiguration
from linebot.v3.messaging.api.messaging_api import MessagingApi
from linebot.v3.messaging.models.show_loading_animation_request import ShowLoadingAnimationRequest

# 建立 Flask 應用程式
app = Flask(__name__)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 取得環境變數
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
XAI_API_KEY = os.getenv("XAI_API_KEY")
if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET or not XAI_API_KEY:
    logger.error("請確認環境變數 LINE_CHANNEL_ACCESS_TOKEN、LINE_CHANNEL_SECRET 與 XAI_API_KEY 均已設定。")
    exit(1)

# 初始化 LINE SDK (v2)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 假設的最大單條訊息長度（根據 LINE 限制，此處設定為 1000 字元）
MAX_LINE_MESSAGE_LENGTH = 1000

# 排隊參數：設定最大同時處理請求數
MAX_CONCURRENT_REQUESTS = 5
current_requests = 0
counter_lock = Lock()

# 更新後的系統提示 (角色設定)
SYSTEM_PROMPT = (
    "請扮演的PMP助教，以親切且專業的語氣，搭配有記憶點的emoji，回答 PMP 考試的答題邏輯解釋專案管理概念。\n"
    "• 在回答 PMP 相關問題時，請參考 PMBOK 指南（最新版），並以 PMP 考試的標準來解釋，確保符合 PMI 的最佳實踐。\n"
    "• 優先定義，再提供使用情境式解釋\n"
    "• 提供 PMP 答題思維，與練習題目\n"
    "請生成詳細的回應，確保內容足夠具體，並請確保你的回應完整，不要中斷。\n\n"
    "例如：\n"
    "如果我問：「在專案執行過程中發現需求變更，應該怎麼辦？」\n"
    "你可以回答：\n"
    "  • 情境分析：當需求變更發生時，專案經理應該依循變更管理流程，而不是直接修改專案範疇。\n"
    "  • PMBOK 指南：根據 PMBOK（第六版，第 4 章），變更請求應該透過整合變更控制流程進行評估。\n"
    "  • 正確的做法：\n"
    "    1. 提交變更請求（Change Request）。\n"
    "    2. 透過變更控制委員會（CCB）審查變更的影響。\n"
    "    3. 若批准，更新專案文件（如專案管理計畫與範疇說明書）。\n"
    "開始對話"
)

# 預設的 PMP 答題思維區塊（僅當使用者詢問「定義」時附加）
EXTRA_BLOCK = (
    "PMP 答題思維\n"
    "- 是否應該遵循 PMBOK 的流程？是的，PDM 是 PMBOK 第 6 版第 6 章《專案時間管理》的一部分，特別是在活動排序過程中使用。\n"
    "- 這個選項是否與 PMP 最佳實踐相符？是的，使用 PDM 繪製活動之間的邏輯關係是符合 PMI 的最佳實踐。\n"
    "- 是否需要與利害關係人協商，或遵循變更管理流程？在繪製 PDM 的階段，通常不需要與利害關係人協商，但如果活動之間的依賴關係變更，可能需要啟動變更管理流程。\n\n"
    "【PMBOK 章節參考】：PMBOK 第 6 版，第 6 章《專案時間管理》。"
)

def send_loading_animation(user_id: str, loading_seconds: int = 10):
    """
    呼叫 LINE Messaging API 顯示等待動畫，
    loading_seconds 必須為 5 的倍數，且最大值為 60 秒。
    """
    config = MessagingConfiguration(
        access_token=LINE_CHANNEL_ACCESS_TOKEN,
        host="https://api.line.me"
    )
    try:
        with ApiClient(config) as api_client:
            messaging_api = MessagingApi(api_client)
            request_body = ShowLoadingAnimationRequest(
                chat_id=user_id,
                loading_seconds=loading_seconds
            )
            messaging_api.show_loading_animation(request_body)
            logger.info("成功發送等待動畫給用戶：%s", user_id)
    except Exception as e:
        logger.error("發送等待動畫錯誤：%s", e)

def call_xai_api(user_message: str) -> str:
    """
    呼叫 x.ai API 生成回應，
    設置參數：max_tokens 為 1000，temperature 為 0.7。
    """
    api_url = "https://api.x.ai/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {XAI_API_KEY}"
    }
    payload = {
        "model": "grok-2-latest",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message}
        ],
        "max_tokens": 1000,
        "temperature": 0.7
    }
    try:
        response = requests.post(api_url, json=payload, headers=headers)
        if response.status_code == 200:
            result = response.json()
            return result.get("choices", [{}])[0].get("message", {}).get("content", "")
        else:
            logger.error("x.ai API 回應錯誤，狀態碼: %s, 回應內容: %s", response.status_code, response.text)
            return ("對不起，生成回應時發生錯誤。可能原因包括：系統繁忙、API 回應延遲或網絡問題，請稍後再試。")
    except Exception as e:
        logger.error("呼叫 x.ai API 時發生例外：%s", e)
        return ("對不起，生成回應時發生例外。可能原因包括：系統繁忙、API 回應延遲或網絡問題，請稍後再試。")

def split_message(text: str, max_length: int = MAX_LINE_MESSAGE_LENGTH) -> list:
    """
    以智慧方式將長訊息分段，
    儘量在換行符或空白處切割，避免破壞內容完整性。
    """
    parts = []
    while len(text) > max_length:
        split_pos = text.rfind("\n", 0, max_length)
        if split_pos == -1:
            split_pos = text.rfind(" ", 0, max_length)
        if split_pos == -1 or split_pos < max_length // 2:
            split_pos = max_length
        parts.append(text[:split_pos].rstrip())
        text = text[split_pos:].lstrip()
    if text:
        parts.append(text)
    return parts

def reply_long_text(reply_token, user_id, text):
    """
    根據生成的回應文字，若超過 1000 字元則分段傳送：
      - 若分段數不超過 5 條，直接以 reply_message 傳送
      - 若超過 5 條，先以 reply_message 傳送前 5 條，再以 push_message 傳送剩餘部分
    """
    segments = split_message(text, MAX_LINE_MESSAGE_LENGTH)
    messages = [TextSendMessage(text=seg) for seg in segments]
    try:
        if len(messages) <= 5:
            line_bot_api.reply_message(reply_token, messages)
            logger.info("成功傳送 %d 條訊息 (reply_message)", len(messages))
        else:
            line_bot_api.reply_message(reply_token, messages[:5])
            logger.info("成功傳送前 5 條訊息 (reply_message)")
            for msg in messages[5:]:
                try:
                    line_bot_api.push_message(user_id, msg)
                    logger.info("成功傳送後續訊息 (push_message): %s", msg.text[:20])
                    time.sleep(0.5)
                except Exception as e:
                    logger.error("傳送後續訊息失敗 (push_message): %s", str(e))
    except Exception as e:
        logger.error("回覆訊息失敗: %s", str(e))

def get_api_response(user_message: str, container: dict):
    """
    線程執行函數，呼叫 x.ai API 並將回應存入 container。
    """
    try:
        container['response'] = call_xai_api(user_message)
    except Exception as e:
        container['response'] = "生成回應時發生錯誤，請稍後再試。"
        logger.error("線程執行錯誤: %s", str(e))

@app.route("/webhook", methods=['POST'])
def webhook():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    logger.info("Received webhook: %s", body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature")
        abort(400)
    return 'OK', 200

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    global current_requests
    # 若使用者詢問查詢無回應原因，則立即回覆
    if event.message.text.strip() in ["查詢原因", "為什麼沒有回應", "無回應原因"]:
        line_bot_api.reply_message(
            event.reply_token,
            [TextSendMessage(text="可能原因包括：系統繁忙、API 回應延遲或網絡問題。請稍後再試或聯繫我們。")]
        )
        return

    # 進入前先檢查是否超過最大同時處理請求數量
    with counter_lock:
        if current_requests >= MAX_CONCURRENT_REQUESTS:
            logger.info("系統繁忙：請求數量達上限")
            line_bot_api.reply_message(
                event.reply_token,
                [TextSendMessage(text="低成本維運中😅 目前系統繁忙，等等在試試吧！")]
            )
            return
        current_requests += 1

    try:
        user_id = event.source.user_id
        user_message = event.message.text.strip()
        logger.info("Received message from %s: %s", user_id, user_message)

        # 發送等待動畫
        send_loading_animation(user_id, loading_seconds=10)
        
        # 非同步呼叫 x.ai API 並等待回應
        container = {}
        start_time = time.time()
        thread = Thread(target=get_api_response, args=(user_message, container))
        thread.start()
        thread.join(timeout=15)  # 延長等待時間至 15 秒
        if thread.is_alive():
            thread.join()  # 若超時則持續等待直到完成
        total_elapsed = time.time() - start_time

        xai_response = container.get('response', "對不起，生成回應時發生錯誤。可能原因包括：系統繁忙、API 回應延遲或網絡問題，請稍後再試。")
        logger.info("x.ai response: %s", xai_response)
        
        # 若使用者訊息包含「定義」關鍵字，則附加 PMP 答題思維區塊
        if "定義" in user_message:
            xai_response += "\n\n" + EXTRA_BLOCK
        
        # 使用 reply_long_text 函式分段回覆
        reply_long_text(event.reply_token, user_id, xai_response)
        logger.info("Reply sent successfully")
    finally:
        with counter_lock:
            current_requests -= 1

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
