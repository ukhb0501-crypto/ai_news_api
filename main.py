import os, hmac, hashlib, base64, json, typing as T
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Header, HTTPException

# ====== バージョン（デプロイ確認用） ======
APP_VERSION = "v-list-debug-2025-10-07-01"

app = FastAPI()

# ====== 設定 ======
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

# キーワード保存先（環境変数 DATA_PATH があれば優先）
DATA_PATH = Path(os.getenv("DATA_PATH", "users.json")).resolve()


# ====== ユーザーデータ（keywords）保存/読込 ======
def load_users() -> dict:
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_users(data: dict) -> None:
    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def normalize_keywords(text: str) -> list[str]:
    # 「a, b , c 」→ ["a", "b", "c"]（空白除去／重複の小文字統一で判定）
    raw = [t.strip() for t in text.split(",") if t.strip()]
    seen = set()
    out = []
    for it in raw:
        key = it.lower()
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


# ====== LINE 署名検証（任意。SECRET未設定ならスキップ） ======
def verify_signature(body: bytes, x_line_signature: str, channel_secret: str) -> bool:
    if not channel_secret:
        return True
    mac = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    calc = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(calc, x_line_signature or "")


# ====== LINE返信 ======
async def line_reply(reply_token: str, text: str) -> None:
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("WARN: LINE_CHANNEL_ACCESS_TOKEN is empty")
        return
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {"replyToken": reply_token, "messages": [{"type": "text", "text": text[:5000]}]}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(LINE_REPLY_URL, headers=headers, json=payload)
        if r.status_code >= 300:
            print("LINE reply error:", r.status_code, r.text)


# ====== ヘルスチェック / バージョン ======
@app.get("/health")
def health():
    return {"ok": True, "data_path": str(DATA_PATH)}

@app.get("/version")
def version():
    return {"version": APP_VERSION}


# ====== Webhook本体 ======
@app.post("/line/webhook")
async def line_webhook(request: Request, x_line_signature: T.Optional[str] = Header(default=None)):
    body_bytes = await request.body()
    try:
        data = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid body")

    if not verify_signature(body_bytes, x_line_signature, LINE_CHANNEL_SECRET):
        raise HTTPException(status_code=400, detail="invalid signature")

    print(f"[{APP_VERSION}] Webhook received:", json.dumps(data, ensure_ascii=False))

    users = load_users()  # まとめてロード（軽量JSONの想定）

    for ev in data.get("events", []):
        etype = ev.get("type")
        src = ev.get("source", {})
        user_id = src.get("userId")
        reply_token = ev.get("replyToken")
        text = (ev.get("message", {}) or {}).get("text", "") if etype == "message" else ""

        # 友だち追加時の挨拶
        if etype == "follow" and reply_token:
            await line_reply(
                reply_token,
                "友だち追加ありがとうございます！\n"
                "キーワード登録: 例）+ 生成AI, 自動運転\n"
                "キーワード削除: 例）- 生成AI\n"
                "一覧表示: list / keywords / キーワード",
            )
            continue

        if etype == "message" and reply_token:
            raw = text or ""
            # 全角空白→半角、前後空白除去
            cmd = raw.replace("　", " ").strip()
            cmd_lower = cmd.lower()
            first = cmd_lower.split(" ", 1)[0] if cmd_lower else ""

            # ★ デバッグ出力（ログで確認用）
            print(f"[{APP_VERSION}] DEBUG_CMD raw={repr(raw)} cmd={repr(cmd)} cmd_lower={repr(cmd_lower)} first={repr(first)}")

            # 当該ユーザーのレコード
            u = users.get(user_id, {"keywords": []})

            # --- 追加: 「+ キーワード1, キーワード2」
            if cmd_lower.startswith("+"):
                print(f"[{APP_VERSION}] HIT: add")
                kws = normalize_keywords(cmd[1:])
                before = {k.lower() for k in u["keywords"]}
                added = []
                for k in kws:
                    if k.lower() not in before:
                        u["keywords"].append(k)
                        added.append(k)
                        before.add(k.lower())
                users[user_id] = u
                save_users(users)
                msg = (
                    f"登録しました：{', '.join(added) if added else '（新規なし）'}\n"
                    f"現在のキーワード：{', '.join(u['keywords']) or '（なし）'}"
                )
                await line_reply(reply_token, msg)
                continue

            # --- 削除: 「- キーワード1, キーワード2」
            if cmd_lower.startswith("-"):
                print(f"[{APP_VERSION}] HIT: remove")
                kws = normalize_keywords(cmd[1:])
                to_remove = {k.lower() for k in kws}
                before_list = u["keywords"]
                u["keywords"] = [k for k in before_list if k.lower() not in to_remove]
                removed = [k for k in before_list if k.lower() in to_remove]
                users[user_id] = u
                save_users(users)
                msg = (
                    f"削除しました：{', '.join(removed) if removed else '（該当なし）'}\n"
                    f"現在のキーワード：{', '.join(u['keywords']) or '（なし）'}"
                )
                await line_reply(reply_token, msg)
                continue

            # --- 一覧: 「list / keywords / キーワード」 （表記ゆれに強く）
            if first in ("list", "keywords", "キーワード"):
                print(f"[{APP_VERSION}] HIT: list")
                await line_reply(
                    reply_token,
                    f"現在のキーワード：{', '.join(u.get('keywords', [])) or '（なし）'}"
                )
                continue

            # 上記コマンド以外はそのままエコー
            await line_reply(reply_token, f"受け取りました：{text}\nあなたのuserId: {user_id}")

    return {"status": "ok"}

