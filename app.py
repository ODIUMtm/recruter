import asyncio, hmac, hashlib, json, os, sqlite3, time, uuid
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BASE_URL = os.getenv("BASE_URL", "")  # https://your-domain
ALLOWED = {int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
DB_PATH = os.getenv("DB_PATH", "data.db")

UNITS = {"n-ter": "Нагатинская · Терапия", "n-sta": "Нагатинская · Стационар",
         "n-ana": "Нагатинская · Анестезиология", "n-inf": "Нагатинская · Инфекция",
         "p-ter": "Павелецкая · Терапия", "p-sta": "Павелецкая · Стационар",
         "p-ana": "Павелецкая · Анестезиология", "c-sur": "Общая база · Хирургия",
         "c-den": "Общая база · Стоматология"}
ROLES = {"doc": "врачи", "asst": "ассистенты"}


def db():
    c = sqlite3.connect(DB_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS staff(id TEXT PRIMARY KEY, data TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    return c


def check_init_data(init_data: str):
    """Validates Telegram Mini App initData, returns user id."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash")
        check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            raise ValueError
        if time.time() - int(pairs.get("auth_date", 0)) > 86400:
            raise ValueError
        return json.loads(pairs["user"])["id"]
    except Exception:
        raise HTTPException(401, "bad initData")


def auth(authorization: str = Header("")):
    uid = check_init_data(authorization.removeprefix("tma "))
    if uid not in ALLOWED:
        raise HTTPException(403, "not allowed")
    return uid


def load_all():
    c = db()
    staff = [dict(json.loads(d), id=i) for i, d in c.execute("SELECT id,data FROM staff")]
    row = c.execute("SELECT v FROM kv WHERE k='needs'").fetchone()
    c.close()
    return staff, (json.loads(row[0]) if row else {})


def summary_text():
    staff, needs = load_all()
    lines = []
    for uid, name in UNITS.items():
        for r, rn in ROLES.items():
            w = sum(1 for e in staff if e["unit"] == uid and e["role"] == r and e["status"] == "work")
            n = needs.get(f"{uid}_{r}", 0)
            if n > w:
                i = sum(1 for e in staff if e["unit"] == uid and e["role"] == r and e["status"] == "intern")
                lines.append(f"• {name}, {rn}: не хватает {n - w}" + (f" (стажёров: {i})" if i else ""))
    interns = [e for e in staff if e["status"] == "intern"]
    head = "Не хватает:\n" + "\n".join(lines) if lines else "Все отделения укомплектованы по плану."
    return head + f"\n\nСтажируются сейчас: {len(interns)}"


@asynccontextmanager
async def lifespan(app):
    task = None
    if BOT_TOKEN:
        from aiogram import Bot, Dispatcher
        from aiogram.filters import Command
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
        bot, dp = Bot(BOT_TOKEN), Dispatcher()

        @dp.message(Command("start"))
        async def start(m: Message):
            if m.from_user.id not in ALLOWED:
                await m.answer(f"Нет доступа. Ваш Telegram ID: {m.from_user.id}\nПередайте его администратору бота.")
                return
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="Открыть штат клиники", web_app=WebAppInfo(url=BASE_URL))]])
            await m.answer("Штат клиники. Команда /summary покажет, чего не хватает.", reply_markup=kb)

        @dp.message(Command("summary"))
        async def summ(m: Message):
            if m.from_user.id in ALLOWED:
                await m.answer(summary_text())

        task = asyncio.create_task(dp.start_polling(bot))
    yield
    if task:
        task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse("index.html")


@app.get("/api/data")
def get_data(_=Depends(auth)):
    staff, needs = load_all()
    return {"staff": staff, "needs": needs}


@app.post("/api/staff")
def save_staff(body: dict, _=Depends(auth)):
    sid = body.pop("id", None) or uuid.uuid4().hex
    if body.get("unit") not in UNITS or body.get("role") not in ROLES or body.get("status") not in ("work", "intern"):
        raise HTTPException(400, "bad fields")
    c = db()
    c.execute("INSERT OR REPLACE INTO staff VALUES(?,?)", (sid, json.dumps(body, ensure_ascii=False)))
    c.commit(); c.close()
    return {"id": sid}


@app.delete("/api/staff/{sid}")
def del_staff(sid: str, _=Depends(auth)):
    c = db(); c.execute("DELETE FROM staff WHERE id=?", (sid,)); c.commit(); c.close()
    return {"ok": True}


@app.put("/api/needs")
def put_needs(body: dict, _=Depends(auth)):
    clean = {k: int(v) for k, v in body.items() if isinstance(v, int) and v >= 0}
    c = db()
    c.execute("INSERT OR REPLACE INTO kv VALUES('needs',?)", (json.dumps(clean),))
    c.commit(); c.close()
    return {"ok": True}
