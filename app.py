import asyncio, logging, os, random, re
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kahrba")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "8581440068"))
WELCOME_BONUS = Decimal(os.getenv("WELCOME_BONUS", "25000"))
REFERRAL_REWARD = Decimal(os.getenv("REFERRAL_REWARD", "1500"))
BOT_NAME = "KAHRBA"

if not TOKEN or not DATABASE_URL:
    raise RuntimeError("Set BOT_TOKEN and DATABASE_URL environment variables.")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
pool = None

MAIN_BUTTONS = [
    ["🎁 أحصل على البونص الترحيبي", "💰 رصيدي"],
    ["🔗 رابط إحالتي", "🎁 كود هدية"],
    ["📩 رسالة للدعم", "🔥 متابعة للبوت"],
]
ADMIN_BUTTON = "🛠️ لوحة الأدمن"

class Flow(StatesGroup):
    captcha = State()
    gift = State()
    support = State()
    admin_input = State()

def main_keyboard(is_admin=False):
    rows = [[KeyboardButton(text=x) for x in row] for row in MAIN_BUTTONS]
    if is_admin:
        rows.append([KeyboardButton(text=ADMIN_BUTTON)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)

def inline_channels(channels):
    rows = []
    for ch in channels:
        title = ch["title"] or ch["chat_id"]
        url = ch["url"]
        if url:
            rows.append([InlineKeyboardButton(text=f"📢 {title}", url=url)])
    rows.append([InlineKeyboardButton(text="✅ تحققت من الاشتراك", callback_data="check_channels")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_keyboard():
    rows = [
        [("➕ إضافة رصيد", "adm:add"), ("➖ خصم رصيد", "adm:sub")],
        [("🎁 إنشاء كود هدية", "adm:gift"), ("📋 سجلات المستخدم", "adm:logs")],
        [("📊 الإحصائيات", "adm:stats"), ("💰 أرصدة المستخدمين", "adm:balances")],
        [("🚫 حظر مستخدم", "adm:ban"), ("✅ إلغاء الحظر", "adm:unban")],
        [("📣 إذاعة رسالة", "adm:broadcast"), ("➕ إضافة قناة إجبارية", "adm:addchannel")],
        [("📃 القنوات الإجبارية", "adm:channels"), ("🗑 حذف قناة", "adm:delchannel")],
        [("⚙️ إعدادات المكافآت", "adm:settings")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t,d in r] for r in rows])

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=30)
    async with pool.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS users(
          user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
          balance NUMERIC(20,2) NOT NULL DEFAULT 0,
          welcome_claimed BOOLEAN NOT NULL DEFAULT FALSE,
          referred_by BIGINT, referrals INTEGER NOT NULL DEFAULT 0,
          verified BOOLEAN NOT NULL DEFAULT FALSE, banned BOOLEAN NOT NULL DEFAULT FALSE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS channels(
          id BIGSERIAL PRIMARY KEY, chat_id TEXT UNIQUE NOT NULL, title TEXT,
          url TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gift_codes(
          code TEXT PRIMARY KEY, amount NUMERIC(20,2) NOT NULL,
          max_uses INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 0,
          expires_at DATE
        );
        CREATE TABLE IF NOT EXISTS gift_redemptions(
          user_id BIGINT NOT NULL, code TEXT NOT NULL REFERENCES gift_codes(code),
          redeemed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          PRIMARY KEY(user_id, code)
        );
        CREATE TABLE IF NOT EXISTS daily_gifts(
          user_id BIGINT PRIMARY KEY, redeemed_at DATE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ledger(
          id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, amount NUMERIC(20,2) NOT NULL,
          reason TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS support_messages(
          id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, message TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        await c.execute("INSERT INTO settings(key,value) VALUES('welcome_bonus',$1) ON CONFLICT(key) DO NOTHING", str(WELCOME_BONUS))
        await c.execute("INSERT INTO settings(key,value) VALUES('referral_reward',$1) ON CONFLICT(key) DO NOTHING", str(REFERRAL_REWARD))

async def setting(key, default=""):
    async with pool.acquire() as c:
        v = await c.fetchval("SELECT value FROM settings WHERE key=$1", key)
    return v if v is not None else default

async def is_admin(user_id):
    return user_id == ADMIN_ID

async def ensure_user(user, referrer=None):
    async with pool.acquire() as c:
        await c.execute("""
        INSERT INTO users(user_id,username,first_name,referred_by)
        VALUES($1,$2,$3,$4)
        ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name
        """, user.id, user.username, user.first_name, referrer if referrer and referrer != user.id else None)

async def required_channels():
    async with pool.acquire() as c:
        return await c.fetch("SELECT * FROM channels ORDER BY id")

async def subscribed(user_id, channels):
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["chat_id"], user_id)
            if member.status in ("left", "kicked") or (member.status == "restricted" and not member.is_member):
                return False
        except Exception:
            log.warning("Cannot verify channel %s; ensure bot is admin/member and chat_id is correct", ch["chat_id"])
            return False
    return True

async def show_gate(message, state, question=None):
    channels = await required_channels()
    if channels:
        await message.answer("🔒 يجب الاشتراك بالقنوات التالية أولًا، ثم اضغط «تحققت من الاشتراك».", reply_markup=inline_channels(channels))
    else:
        await ask_captcha(message, state)

async def ask_captcha(message, state):
    a, b = random.randint(1, 9), random.randint(1, 10)
    # Keep the answer in 1..10 as requested.
    if a + b > 10:
        a, b = random.randint(1, 5), random.randint(1, 5)
    await state.set_state(Flow.captcha)
    await state.update_data(captcha_answer=a+b)
    await message.answer(f"🛡️ سؤال الأمان لمتابعة الدخول:\n\nكم يساوي {a} + {b} ؟\nأرسل الرقم فقط.")

async def complete_verification(message, state):
    uid = message.from_user.id
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow("SELECT * FROM users WHERE user_id=$1 FOR UPDATE", uid)
            if not row:
                return
            if row["banned"]:
                await message.answer("تم تقييد استخدام هذا الحساب.")
                return
            if row["verified"]:
                await state.clear()
                await message.answer("أهلًا بك مجددًا في KAHRBA ⚡", reply_markup=main_keyboard(await is_admin(uid)))
                return
            await c.execute("UPDATE users SET verified=TRUE WHERE user_id=$1", uid)
            ref = row["referred_by"]
            if ref and ref != uid:
                reward = Decimal(await setting("referral_reward", "1500"))
                await c.execute("UPDATE users SET balance=balance+$1, referrals=referrals+1 WHERE user_id=$2", reward, ref)
                await c.execute("INSERT INTO ledger(user_id,amount,reason) VALUES($1,$2,$3)", ref, reward, f"Referral: {uid}")
    await state.clear()
    await message.answer("أهلًا بك يا ملك في بوت KAHRBA\nالموثق والرسمي ⚡👑", reply_markup=main_keyboard(await is_admin(uid)))

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    parts = (message.text or "").split(maxsplit=1)
    referrer = None
    if len(parts) > 1:
        m = re.fullmatch(r"ref_(\d+)", parts[1].strip())
        if m:
            referrer = int(m.group(1))
    await ensure_user(message.from_user, referrer)
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT verified,banned FROM users WHERE user_id=$1", message.from_user.id)
    if row["banned"]:
        await message.answer("هذا الحساب مقيّد.")
        return
    if row["verified"]:
        await message.answer("أهلًا بك يا ملك في بوت KAHRBA\nالموثق والرسمي ⚡👑", reply_markup=main_keyboard(await is_admin(message.from_user.id)))
        return
    await message.answer("أهلًا بك يا ملك في بوت KAHRBA\nالموثق والرسمي ⚡👑")
    await show_gate(message, state)

@dp.callback_query(F.data == "check_channels")
async def check_channels(call: CallbackQuery, state: FSMContext):
    channels = await required_channels()
    if await subscribed(call.from_user.id, channels):
        await call.answer("تم التحقق من الاشتراك")
        await ask_captcha(call.message, state)
    else:
        await call.answer("لم يكتمل الاشتراك بعد. اشترك بكل القنوات أولًا.", show_alert=True)

@dp.message(Flow.captcha)
async def captcha_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    if not (message.text or "").strip().isdigit() or int(message.text.strip()) != data.get("captcha_answer"):
        await ask_captcha(message, state)
        return
    channels = await required_channels()
    if channels and not await subscribed(message.from_user.id, channels):
        await message.answer("يجب الاشتراك بالقنوات المطلوبة أولًا.", reply_markup=inline_channels(channels))
        return
    await complete_verification(message, state)

async def access_ok(message):
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT verified,banned FROM users WHERE user_id=$1", message.from_user.id)
    if not row or row["banned"]:
        await message.answer("ابدأ باستخدام /start أو تواصل مع الدعم إذا كان حسابك مقيّدًا.")
        return False
    if not row["verified"]:
        await show_gate(message, dp.fsm.get_context(bot=bot, chat_id=message.chat.id, user_id=message.from_user.id))
        return False
    channels = await required_channels()
    if channels and not await subscribed(message.from_user.id, channels):
        await message.answer("🔒 يجب أن تبقى مشتركًا بالقنوات المطلوبة.", reply_markup=inline_channels(channels))
        return False
    return True

@dp.message(F.text == "🎁 أحصل على البونص الترحيبي")
async def welcome_bonus(message: Message):
    if not await access_ok(message): return
    uid = message.from_user.id
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow("SELECT welcome_claimed FROM users WHERE user_id=$1 FOR UPDATE", uid)
            if row["welcome_claimed"]:
                await message.answer("👑 يا ملك، لقد حصلت على البونص الترحيبي سابقًا.")
                return
            amount = Decimal(await setting("welcome_bonus", "25000"))
            await c.execute("UPDATE users SET balance=balance+$1,welcome_claimed=TRUE WHERE user_id=$2", amount, uid)
            await c.execute("INSERT INTO ledger(user_id,amount,reason) VALUES($1,$2,'Welcome bonus')", uid, amount)
    await message.answer(f"🎉 مبروك يا ملك!\nلقد حصلت على بونص ترحيبي بقيمة {amount:,.0f} ل.س\nيمكنك شحنه للحساب عند انطلاقة البوت.", reply_markup=main_keyboard(await is_admin(uid)))

@dp.message(F.text == "💰 رصيدي")
async def balance(message: Message):
    if not await access_ok(message): return
    async with pool.acquire() as c:
        row = await c.fetchrow("SELECT balance,referrals FROM users WHERE user_id=$1", message.from_user.id)
    await message.answer(f"👑 رصيدك: <b>{row['balance']:,.0f} ل.س</b>\n🔗 عدد الإحالات: {row['referrals']}")

@dp.message(F.text == "🔗 رابط إحالتي")
async def referral(message: Message):
    if not await access_ok(message): return
    me = await bot.get_me()
    reward = await setting("referral_reward", "1500")
    await message.answer(f"🔗 رابط إحالتك الخاص:\nhttps://t.me/{me.username}?start=ref_{message.from_user.id}\n\nاحصل على {Decimal(reward):,.0f} ل.س يا ملك مقابل كل مشترك جديد ينضم عن طريقك بعد اجتياز سؤال الأمان والتحقق من الاشتراك. يمكنك شحنها للحساب.")

@dp.message(F.text == "🎁 كود هدية")
async def gift_start(message: Message, state: FSMContext):
    if not await access_ok(message): return
    await state.set_state(Flow.gift)
    await message.answer("أرسل كود الهدية الآن:")

@dp.message(Flow.gift)
async def gift_redeem(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    uid = message.from_user.id
    today = datetime.now(timezone.utc).date()
    async with pool.acquire() as c:
        async with c.transaction():
            daily = await c.fetchval("SELECT redeemed_at FROM daily_gifts WHERE user_id=$1 FOR UPDATE", uid)
            if daily == today:
                await state.clear(); await message.answer("استخدمت كود هدية اليوم بالفعل."); return
            gift = await c.fetchrow("SELECT * FROM gift_codes WHERE code=$1 FOR UPDATE", code)
            if not gift or gift["uses"] >= gift["max_uses"] or (gift["expires_at"] and gift["expires_at"] < today):
                await state.clear(); await message.answer("الكود غير صالح أو منتهي أو وصل للحد الأقصى."); return
            already = await c.fetchval("SELECT 1 FROM gift_redemptions WHERE user_id=$1 AND code=$2", uid, code)
            if already:
                await state.clear(); await message.answer("استخدمت هذا الكود من قبل."); return
            amount = gift["amount"]
            await c.execute("UPDATE gift_codes SET uses=uses+1 WHERE code=$1", code)
            await c.execute("INSERT INTO gift_redemptions(user_id,code) VALUES($1,$2)", uid, code)
            await c.execute("INSERT INTO daily_gifts(user_id,redeemed_at) VALUES($1,$2) ON CONFLICT(user_id) DO UPDATE SET redeemed_at=EXCLUDED.redeemed_at", uid, today)
            await c.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", amount, uid)
            await c.execute("INSERT INTO ledger(user_id,amount,reason) VALUES($1,$2,$3)", uid, amount, f"Gift code {code}")
    await state.clear()
    await message.answer(f"🎉 تم إضافة {amount:,.0f} ل.س إلى رصيدك.")

@dp.message(F.text == "📩 رسالة للدعم")
async def support_start(message: Message, state: FSMContext):
    if not await access_ok(message): return
    await state.set_state(Flow.support)
    await message.answer("اكتب رسالتك للدعم وسيتم إرسالها للإدارة:")

@dp.message(Flow.support)
async def support_send(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt:
        await message.answer("أرسل رسالة نصية من فضلك."); return
    async with pool.acquire() as c:
        await c.execute("INSERT INTO support_messages(user_id,message) VALUES($1,$2)", message.from_user.id, txt)
    try:
        await bot.send_message(ADMIN_ID, f"📩 رسالة دعم من {message.from_user.full_name} (`{message.from_user.id}`):\n{txt}")
    except Exception:
        pass
    await state.clear()
    await message.answer("تم إرسال رسالتك إلى الدعم.")

@dp.message(F.text == "🔥 متابعة للبوت")
async def follow(message: Message):
    if not await access_ok(message): return
    await message.answer("أنتظر لافتتاحه اليوم 🔥")

@dp.message(Command("admin"))
@dp.message(F.text == ADMIN_BUTTON)
async def admin_panel(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("ليس لديك صلاحية الوصول إلى لوحة الإدارة.")
        return
    await message.answer("🛠️ لوحة إدارة KAHRBA", reply_markup=admin_keyboard())

async def admin_prompt(call, state, key, prompt):
    await state.set_state(Flow.admin_input)
    await state.update_data(admin_action=key)
    await call.message.answer(prompt)
    await call.answer()

@dp.callback_query(F.data.startswith("adm:"))
async def admin_actions(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id):
        await call.answer("غير مصرح لك.", show_alert=True); return
    action = call.data.split(":",1)[1]
    if action == "stats":
        async with pool.acquire() as c:
            users = await c.fetchval("SELECT COUNT(*) FROM users")
            verified = await c.fetchval("SELECT COUNT(*) FROM users WHERE verified")
            total = await c.fetchval("SELECT COALESCE(SUM(balance),0) FROM users")
        await call.message.answer(f"📊 المستخدمون: {users}\nالمتحققون: {verified}\nإجمالي الأرصدة: {total:,.0f} ل.س")
        await call.answer()
    elif action == "balances":
        async with pool.acquire() as c:
            rows = await c.fetch("SELECT user_id,balance FROM users ORDER BY balance DESC LIMIT 30")
        await call.message.answer("💰 أعلى 30 رصيدًا:\n" + "\n".join(f"{r['user_id']}: {r['balance']:,.0f} ل.س" for r in rows))
        await call.answer()
    elif action == "channels":
        chs = await required_channels()
        await call.message.answer("القنوات الإجبارية:\n" + ("\n".join(f"{x['chat_id']} — {x['url']}" for x in chs) if chs else "لا توجد قنوات مضافة."))
        await call.answer()
    elif action in ("add","sub","ban","unban"):
        await admin_prompt(call, state, action, "أرسل بالصيغة المطلوبة:\n" + {
            "add":"USER_ID AMOUNT لإضافة رصيد",
            "sub":"USER_ID AMOUNT لخصم رصيد",
            "ban":"USER_ID لحظر مستخدم",
            "unban":"USER_ID لإلغاء حظر مستخدم"
        }[action])
    elif action == "gift":
        await admin_prompt(call, state, "gift", "أرسل: CODE|AMOUNT|MAX_USES|YYYY-MM-DD\nالتاريخ اختياري؛ استخدم - بدل التاريخ.")
    elif action == "addchannel":
        await admin_prompt(call, state, "addchannel", "أرسل: @channelusername|https://t.me/channelusername|اسم القناة\nأضف البوت مشرفًا في القناة ليتمكن من التحقق.")
    elif action == "delchannel":
        await admin_prompt(call, state, "delchannel", "أرسل chat_id أو @username للقناة التي تريد حذفها.")
    elif action == "settings":
        await admin_prompt(call, state, "settings", "أرسل: welcome_bonus AMOUNT أو referral_reward AMOUNT")
    elif action == "logs":
        await admin_prompt(call, state, "logs", "أرسل USER_ID لعرض آخر 15 عملية.")
    elif action == "broadcast":
        await admin_prompt(call, state, "broadcast", "أرسل نص الرسالة لإذاعتها لجميع المستخدمين.")
    else:
        await call.answer()

@dp.message(Flow.admin_input)
async def admin_input(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await state.clear(); await message.answer("غير مصرح."); return
    data = await state.get_data()
    action = data.get("admin_action")
    raw = (message.text or "").strip()
    try:
        async with pool.acquire() as c:
            if action in ("add","sub"):
                uid_s, amount_s = raw.split(maxsplit=1)
                uid, amount = int(uid_s), Decimal(amount_s)
                if amount <= 0: raise ValueError("amount")
                delta = amount if action == "add" else -amount
                async with c.transaction():
                    await c.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", delta, uid)
                    await c.execute("INSERT INTO ledger(user_id,amount,reason) VALUES($1,$2,$3)", uid, delta, "Admin adjustment")
                await message.answer("تم تنفيذ تعديل الرصيد.")
            elif action in ("ban","unban"):
                uid = int(raw)
                await c.execute("UPDATE users SET banned=$1 WHERE user_id=$2", action=="ban", uid)
                await message.answer("تم تحديث حالة المستخدم.")
            elif action == "gift":
                parts = raw.split("|")
                if len(parts) != 4: raise ValueError("format")
                code, amount_s, max_s, date_s = parts
                amount, max_uses = Decimal(amount_s), int(max_s)
                exp = None if date_s.strip() in ("","-") else datetime.strptime(date_s.strip(), "%Y-%m-%d").date()
                if amount <= 0 or max_uses < 1: raise ValueError("value")
                await c.execute("INSERT INTO gift_codes(code,amount,max_uses,expires_at) VALUES($1,$2,$3,$4)", code.strip().upper(), amount, max_uses, exp)
                await message.answer("تم إنشاء كود الهدية.")
            elif action == "addchannel":
                parts = raw.split("|")
                if len(parts) != 3: raise ValueError("format")
                chat_id, url, title = [p.strip() for p in parts]
                if not url.startswith("https://t.me/"): raise ValueError("url")
                await c.execute("INSERT INTO channels(chat_id,title,url) VALUES($1,$2,$3) ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title,url=EXCLUDED.url", chat_id, title, url)
                await message.answer("تمت إضافة القناة. تأكد أن البوت مشرف فيها.")
            elif action == "delchannel":
                await c.execute("DELETE FROM channels WHERE chat_id=$1", raw)
                await message.answer("تم حذف القناة إن كانت موجودة.")
            elif action == "settings":
                key, val = raw.split(maxsplit=1)
                if key not in ("welcome_bonus","referral_reward") or Decimal(val) < 0: raise ValueError("setting")
                await c.execute("INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", key, val)
                await message.answer("تم تحديث الإعداد.")
            elif action == "logs":
                uid = int(raw)
                rows = await c.fetch("SELECT amount,reason,created_at FROM ledger WHERE user_id=$1 ORDER BY id DESC LIMIT 15", uid)
                await message.answer("آخر العمليات:\n" + ("\n".join(f"{r['created_at']:%Y-%m-%d %H:%M} | {r['amount']:,.0f} | {r['reason']}" for r in rows) or "لا توجد عمليات."))
            elif action == "broadcast":
                rows = await c.fetch("SELECT user_id FROM users WHERE banned=FALSE")
                sent = 0
                for r in rows:
                    try:
                        await bot.send_message(r["user_id"], raw)
                        sent += 1
                        await asyncio.sleep(0.04)
                    except Exception:
                        continue
                await message.answer(f"انتهت الإذاعة. تم الإرسال إلى {sent} مستخدم.")
    except (ValueError, ArithmeticError, asyncpg.UniqueViolationError):
        await message.answer("تعذر تنفيذ العملية. تحقق من الصيغة والقيم ثم حاول مجددًا.")
        return
    except Exception:
        log.exception("Admin action failed: %s", action)
        await message.answer("حدث خطأ أثناء تنفيذ العملية. راجع سجلات Render.")
        return
    finally:
        await state.clear()

async def main():
    await init_db()
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await pool.close()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
