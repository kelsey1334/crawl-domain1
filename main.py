import os
import time
import tempfile
import requests
import pandas as pd
from telegram import Update, Document
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CENSYS_API_ID = os.environ.get("CENSYS_API_ID")
CENSYS_API_SECRET = os.environ.get("CENSYS_API_SECRET")

# Giới hạn an toàn để tránh tải quá lớn; có thể chỉnh bằng ENV
MAX_HITS = int(os.environ.get("MAX_HITS", "20000"))  # số certificate tối đa cần duyệt
PER_PAGE = int(os.environ.get("PER_PAGE", "100"))    # mỗi trang Censys
TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))  # giây

STATE_ASKING_SUFFIX = "ASKING_SUFFIX"
user_states = {}

CENSYS_SEARCH_URL = "https://search.censys.io/api/v2/search/certificates"
HEADERS = {"Content-Type": "application/json"}


def ensure_env():
    missing = [k for k in ["TELEGRAM_TOKEN", "CENSYS_API_ID", "CENSYS_API_SECRET"] if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")


def normalize_suffix(s: str) -> str:
    s = s.strip().lower()
    if s.startswith('.'):
        s = s[1:]
    return s


def build_query_for_suffix(suffix: str) -> str:
    # Query Censys v2: dùng wildcard cho parsed.names
    # ví dụ: parsed.names: *.uk.com
    return f"parsed.names: *.{suffix}"


def censys_search_all_domains(suffix: str):
    session = requests.Session()
    session.auth = (CENSYS_API_ID, CENSYS_API_SECRET)
    session.headers.update(HEADERS)

    q = build_query_for_suffix(suffix)

    payload = {
        "q": q,
        "per_page": PER_PAGE,
        # Có thể thêm sort nếu cần: "sort": [{"field": "validation.updated_at", "direction": "desc"}]
    }

    all_names = set()
    total_examined = 0

    while True:
        resp = session.post(CENSYS_SEARCH_URL, json=payload, timeout=TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"Censys API error: HTTP {resp.status_code} - {resp.text[:500]}")
        data = resp.json()

        result = data.get("result", {})
        hits = result.get("hits", [])
        for h in hits:
            # mỗi hit là 1 chứng chỉ; lấy parsed.names
            names = (
                h.get("parsed", {}).get("names")
                or h.get("names")  # phòng trường hợp schema khác
                or []
            )
            for n in names:
                if n.lower().endswith('.' + suffix) or n.lower() == suffix:
                    all_names.add(n.lower())
        total_examined += len(hits)
        if total_examined >= MAX_HITS:
            break

        # Phân trang theo cursor (v2 thường trả về links.next)
        links = result.get("links", {})
        next_cursor = links.get("next")
        if not next_cursor:
            break
        payload = {"cursor": next_cursor, "per_page": PER_PAGE}

        # Tránh rate limit nhẹ
        time.sleep(0.2)

    # Chuẩn hoá: chỉ trả về domain kết thúc đúng với suffix
    filtered = sorted({d for d in all_names if d.endswith('.' + suffix) or d == suffix})
    return filtered


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_states[update.effective_user.id] = STATE_ASKING_SUFFIX
    await update.message.reply_text(
        "Bạn muốn tìm đuôi domain nào?\nVí dụ: uk.com, ru.com, us.com, jpn.com"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_states.get(uid)
    text = (update.message.text or "").strip()

    if state == STATE_ASKING_SUFFIX:
        suffix = normalize_suffix(text)
        await update.message.reply_text(
            f"Đang tìm các domain *.{suffix}* trên Censys. Vui lòng chờ...",
            parse_mode="Markdown",
        )
        try:
            domains = await context.application.run_in_executor(None, censys_search_all_domains, suffix)
            if not domains:
                await update.message.reply_text("Không tìm thấy kết quả phù hợp.")
                return

            # Xuất Excel tạm thời
            df = pd.DataFrame({"domain": domains})
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                out_path = tmp.name
            df.to_excel(out_path, index=False)

            await update.message.reply_document(document=open(out_path, "rb"), filename=f"domains_{suffix}.xlsx")
        except Exception as e:
            await update.message.reply_text(f"Lỗi: {e}")
        finally:
            user_states.pop(uid, None)
    else:
        await update.message.reply_text("Gõ /start để bắt đầu.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start – bắt đầu, nhập đuôi domain (vd: uk.com)")


def main():
    ensure_env()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
