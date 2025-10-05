import os, hmac, hashlib, base64, json, typing as T
import httpx
from fastapi import FastAPI, Request, Header, HTTPException

app = FastAPI()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

def verify_signature(body: bytes, x_line_signature: str, channel_secret: str) -> bool:
    if not channel_secret:
        return True
    mac = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    calc = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(calc, x_line_signature)

async def line_reply(reply_token: str, text: str) -> None:
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("WARN: LINE_CHANNEL_ACCESS_TOKEN is empty"); return
    headers = {"Content-Type": "application/json","Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    payload = {"replyToken": reply_token, "messages": [{"type": "text", "text": text[:5000]}]}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(LINE_REPLY_URL, headers=headers, json=payload)
        if r.status_code >= 300:
            print("LINE reply error:", r.status_code, r.text)

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/line/webhook")
async def line_webhook(request: Request, x_line_signature: T.Optional[str] = Header(default=None)):
    body_bytes = await request.body()
    try:
        data = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid body")

    channel_secret = os.getenv("LINE_CHANNEL_SECRET", "")
    if x_line_signature and channel_secret:
        if not verify_signature(body_bytes, x_line_signature, channel_secret):
            raise HTTPException(status_code=400, detail="invalid signature")

    print("Webhook received:", json.dumps(data, ensure_ascii=False))

    for ev in data.get("events", []):
        etype = ev.get("type")
        src = ev.get("source", {})
        user_id = src.get("userId")
        reply_token = ev.get("replyToken")

        if etype == "message" and reply_token:
            text = ev.get("message", {}).get("text", "")
            await line_reply(reply_token, f"受け取りました：{text}\nあなたのuserId: {user_id}")
        if etype == "follow" and reply_token:
            await line_reply(reply_token, "友だち追加ありがとうございます！\n「+ 生成AI, 自動運転」のようにキーワードを登録できます。")
    return {"status": "ok"}
