import asyncio
import base64
import json
import os
import re
import time
import zlib

from aiosplus import Bot, Dispatcher
from aiosplus.filters import Command
from aiosplus.types import Message

TOKEN = os.environ.get("BOT_TOKEN") or "69954493:fOqbkUfpTZOilid0N5nVvlcMLz-qKWyKZOM"

ADMIN_USERNAME = "@User_ligo"
START_FILE = "start_text.txt"
USERS_FILE = "users.txt"
CHANNELS_FILE = "channels.json"
CACHE_SECONDS = 120
# True: اگر چک عضویت خطا بدهد، کاربر باید جوین شود (امن‌تر)
# False: اگر چک عضویت خطا بدهد، کاربر رد می‌شود (قبلاً همین بود)
FAIL_CLOSED = True

DEFAULT_START = (
    "سلام 👋\n"
    "به ربات تبدیل ویدئو به Video Message خوش آمدید 🎥\n\n"
    "ابتدا در کانال ما عضو شوید و سپس یک ویدئو برای من ارسال کنید "
    "تا آن را به Video Message تبدیل کنم. 🔄"
)

bot = Bot(token=TOKEN)
dp = Dispatcher()

waiting_for_start_text = False
waiting_for_broadcast_text = False


def get_start_text():
    if os.path.exists(START_FILE):
        with open(START_FILE, "r", encoding="utf-8") as f:
            return f.read()

    with open(START_FILE, "w", encoding="utf-8") as f:
        f.write(DEFAULT_START)

    return DEFAULT_START


def is_admin(message):
    username = getattr(message.from_user, "username", None)

    if username and not username.startswith("@"):
        username = "@" + username

    return username is not None and username.lower() == ADMIN_USERNAME.lower()


def save_user(chat_id):
    users = load_users()

    if str(chat_id) not in users:
        with open(USERS_FILE, "a", encoding="utf-8") as f:
            f.write(f"{chat_id}\n")


def load_users():
    if not os.path.exists(USERS_FILE):
        return []

    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


# ---------------- یوزربات (اکانت شخصی برای چک عضویت) ----------------
userbot = None  # SplusClient لاگین‌شده؛ اگر None باشد از خود بات استفاده می‌شود


def restore_session():
    """فایل‌های session را از متغیر USERBOT_SESSION_B64 برمی‌گرداند."""
    data = os.environ.get("USERBOT_SESSION_B64", "").strip()
    if not data:
        import glob

        for path in sorted(glob.glob("session_b64*")) + sorted(glob.glob("*session*b64*")):
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = "".join(f.read().split())
                if data:
                    print("USERBOT: session خوانده شد از", path)
                    break
    if not data:
        return False
    try:
        files = json.loads(zlib.decompress(base64.b64decode(data)).decode("utf-8"))
        for name, content in files.items():
            with open(os.path.basename(name), "wb") as f:
                f.write(base64.b64decode(content))
        return True
    except Exception as e:
        print("SESSION RESTORE ERROR:", e)
        return False


async def start_userbot():
    global userbot
    if not restore_session() and not any(
        n.startswith("userbot") for n in os.listdir(".")
    ):
        print("USERBOT: session پیدا نشد؛ چک عضویت با خود بات انجام می‌شود.")
        print("USERBOT: پوشه فعلی:", os.getcwd())
        print("USERBOT: فایل‌های این پوشه:", sorted(os.listdir(".")))
        return

    try:
        from spluslib import SplusClient

        client = SplusClient("userbot")
        # بدون شماره: از session ذخیره‌شده استفاده می‌کند
        if hasattr(client, "connect"):
            await client.connect()
        else:
            await client.start()
        me = await client.get_me()
        print("USERBOT logged in:", me)
        print(
            "USERBOT methods:",
            [m for m in dir(client) if "member" in m.lower() or "particip" in m.lower()],
        )
        userbot = client
    except Exception as e:
        print("USERBOT START ERROR:", e)
        userbot = None


async def userbot_get_member(chat_id, user_id):
    """عضویت را با یوزربات می‌گیرد. نام متدها را با چند حالت امتحان می‌کند."""
    base = getattr(userbot, "_client", None)

    # سروش GetParticipant را پشتیبانی نمی‌کند؛ لیست اعضا (فیلتر Recent) را می‌گیریم
    ids = await get_member_ids(chat_id)
    if int(user_id) in ids:
        return {"status": "member"}
    # اگر در لیست کش‌شده نبود، یک بار لیست تازه بگیر (شاید تازه جوین شده)
    ids = await get_member_ids(chat_id, force=True)
    if int(user_id) in ids:
        return {"status": "member"}
    return {"status": "left"}


_members_cache = {}  # chat_id -> (زمان, set آیدی اعضا)
MEMBERS_LIMIT = 20000
MEMBERS_CACHE_SECONDS = 60


def _extract_id(m):
    if isinstance(m, dict):
        for k in ("id", "user_id"):
            if m.get(k) is not None:
                return m[k]
        u = m.get("user")
        if isinstance(u, dict):
            return u.get("id")
        return None
    for k in ("id", "user_id"):
        if getattr(m, k, None) is not None:
            return getattr(m, k)
    return None


async def fetch_members_recent(chat_id):
    """لیست اعضا با فیلتر Recent (تنها روشی که سروش برای کانال جواب می‌دهد)."""
    import importlib

    base = userbot._client
    mod = type(base).__module__
    root = mod[: mod.find("._base") + 6]
    types = importlib.import_module(root + ".tl.types")
    flt = types.ChannelParticipantsRecent()
    return [
        u
        async for u in base.iter_participants(chat_id, limit=MEMBERS_LIMIT, filter=flt)
    ]


async def get_member_ids(chat_id, force=False):
    now = time.time()
    cached = _members_cache.get(str(chat_id))
    if not force and cached and now - cached[0] < MEMBERS_CACHE_SECONDS:
        return cached[1]

    members = await fetch_members_recent(chat_id)
    ids = set()
    for m in members or []:
        mid = _extract_id(m)
        if mid is not None:
            try:
                ids.add(int(mid))
            except (TypeError, ValueError):
                pass

    if not ids:
        raise Exception("لیست اعضا خالی برگشت (اکانت باید عضو/ادمین کانال باشد)")
    if len(members) >= MEMBERS_LIMIT:
        raise Exception("تعداد اعضا از سقف MEMBERS_LIMIT بیشتر است")

    _members_cache[str(chat_id)] = (now, ids)
    return ids


# ---------------- جوین اجباری ----------------
_member_cache = {}  # (user_id, chat_id) -> زمان آخرین تایید عضویت


def load_channels():
    if not os.path.exists(CHANNELS_FILE):
        return []
    try:
        with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("CHANNELS LOAD ERROR:", e)
        return []


def save_channels(channels):
    with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
        json.dump(channels, f, ensure_ascii=False, indent=2)


def username_from_link(link):
    m = re.search(r"(?:splus\.ir|sapp\.ir)/([A-Za-z0-9_]+)", link)
    return m.group(1) if m else None


def is_member_status(member):
    if isinstance(member, dict):
        member = member.get("status", member)
    status = str(getattr(member, "status", member)).lower()
    return not any(x in status for x in ("left", "kicked", "banned"))


_botapi_url = None


def find_botapi_url():
    """آدرس پایه Bot API را از خود aiosplus پیدا می‌کند."""
    global _botapi_url
    if _botapi_url:
        return _botapi_url

    import importlib
    import inspect
    import pkgutil
    from urllib.parse import urlparse

    origin = None
    try:
        import aiosplus

        for m in pkgutil.walk_packages(aiosplus.__path__, "aiosplus."):
            try:
                src = inspect.getsource(importlib.import_module(m.name))
            except Exception:
                continue
            for u in re.findall(r"https?://[A-Za-z0-9_.\-]*splus\.ir", src):
                if "api" in u or "bot" in u:
                    p = urlparse(u)
                    origin = f"{p.scheme}://{p.netloc}"
                    break
            if origin:
                break
    except Exception as e:
        print("BOTAPI URL DISCOVERY ERROR:", e)

    origin = origin or "https://botapi.splus.ir"
    _botapi_url = f"{origin}/bot{TOKEN}"
    print("BOTAPI base:", origin)
    return _botapi_url


async def botapi_get_member(chat_id, user_id):
    """getChatMember از API رسمی بات (بات باید ادمین کانال باشد)."""
    import httpx

    url = find_botapi_url() + "/getChatMember"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(url, json={"chat_id": chat_id, "user_id": int(user_id)})
    try:
        data = r.json()
    except Exception:
        raise Exception(f"botapi HTTP {r.status_code}: {r.text[:200]}")
    if not data.get("ok", True) or "result" not in data:
        raise Exception(f"botapi: {str(data)[:300]}")
    return data["result"]


async def get_member(user_id, chat_id):
    if userbot is not None:
        return await userbot_get_member(chat_id, user_id)
    # بدون یوزربات: فقط اگر بات ادمین کانال باشد کار می‌کند
    return await botapi_get_member(chat_id, user_id)


async def check_one(user_id, chat_id):
    """True/False عضویت. اگر خطا بدهد Exception بالا می‌رود."""
    key = (user_id, str(chat_id))
    if time.time() - _member_cache.get(key, 0) < CACHE_SECONDS:
        return True

    member = await get_member(user_id, chat_id)
    ok = is_member_status(member)
    if ok:
        _member_cache[key] = time.time()
    return ok


async def missing_channels(user_id):
    """لیست کانال‌هایی که کاربر عضو نیست."""
    missing = []
    for ch in load_channels():
        try:
            if not await check_one(user_id, ch["chat_id"]):
                missing.append(ch)
        except Exception as e:
            print("JOIN CHECK ERROR:", ch, e)
            if FAIL_CLOSED:
                # اگر چک ممکن نبود، کاربر را «عضو نیست» حساب کن
                missing.append(ch)
    return missing


async def ensure_joined(message):
    """اگر عضو همه کانال‌ها نبود پیام می‌فرستد و False برمی‌گرداند."""
    if is_admin(message):
        return True

    missing = await missing_channels(message.from_user.id)
    if not missing:
        return True

    links = "\n".join(f"🔹 {ch['link']}" for ch in missing)
    await message.answer(
        "⚠️ برای استفاده از ربات ابتدا در کانال(های) زیر عضو شوید:\n\n"
        f"{links}\n\n"
        "بعد از عضویت، دوباره ویدئوی خود را ارسال کنید (یا /start بزنید)."
    )
    return False


@dp.message(Command("start"))
async def start_handler(message: Message):
    save_user(message.chat.id)
    await message.answer(get_start_text())
    await ensure_joined(message)


@dp.message(Command("setstart"))
async def setstart_handler(message: Message):
    global waiting_for_start_text

    if not is_admin(message):
        return

    waiting_for_start_text = True

    await message.answer(
        "✏️ متن جدید استارت را ارسال کنید.\n\n"
        "برای لغو: /cancel"
    )


@dp.message(Command("broadcast"))
async def broadcast_handler(message: Message):
    global waiting_for_broadcast_text

    if not is_admin(message):
        return

    waiting_for_broadcast_text = True

    await message.answer(
        "📢 متن اطلاعیه را ارسال کنید تا برای همه کاربران فرستاده شود.\n\n"
        "برای لغو: /cancel"
    )


@dp.message(Command("addchannel"))
async def addchannel_handler(message: Message):
    if not is_admin(message):
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "➕ فرمت:\n/addchannel https://splus.ir/Meme_Goblin\n\n"
            "اگر chat_id با یوزرنیم کار نکرد:\n"
            "/addchannel https://splus.ir/Meme_Goblin CHAT_ID"
        )
        return

    link = parts[1]
    if len(parts) >= 3:
        chat_id = parts[2]
    else:
        uname = username_from_link(link)
        if not uname:
            await message.answer("❌ لینک معتبر نیست. chat_id را دستی بدهید.")
            return
        chat_id = "@" + uname

    # تست: آیا می‌شود عضویت را چک کرد؟
    try:
        res = await get_member(message.from_user.id, chat_id)
        test = f"✅ تست چک عضویت موفق بود.\nپاسخ: {str(res)[:300]}"
    except Exception as e:
        test = (
            "⚠️ تست چک عضویت خطا داد "
            f"(بات باید ادمین کانال باشد / chat_id را چک کنید):\n{e}"
        )

    channels = load_channels()
    channels = [c for c in channels if c["link"] != link]
    channels.append({"link": link, "chat_id": chat_id})
    save_channels(channels)

    await message.answer(f"✅ کانال اضافه شد.\n{link}\nchat_id: {chat_id}\n\n{test}")


@dp.message(Command("dbg"))
async def dbg_handler(message: Message):
    """/dbg @Meme_Goblin  -> خروجی خام کتابخانه برای عیب‌یابی"""
    if not is_admin(message):
        return
    if userbot is None:
        await message.answer("یوزربات بالا نیست.")
        return

    parts = (message.text or "").split()
    chat = parts[1] if len(parts) > 1 else "@Meme_Goblin"
    out = []

    try:
        mem = await userbot.get_chat_members(chat, limit=5)
        out.append(f"get_chat_members: {type(mem).__name__}, len={len(mem) if mem is not None else None}")
        out.append("نمونه: " + repr(mem)[:600])
    except Exception as e:
        out.append(f"get_chat_members ERROR: {e}")

    try:
        info = await userbot.get_chat_info(chat)
        out.append("get_chat_info: " + repr(info)[:600])
    except Exception as e:
        out.append(f"get_chat_info ERROR: {e}")

    try:
        chats = await userbot.get_chats(limit=100)
        rows = []
        for c in chats or []:
            s = repr(c)
            if "Goblin" in s or "goblin" in s:
                rows.append(s[:300])
        out.append("get_chats (مرتبط با Goblin): " + (" | ".join(rows) if rows else "پیدا نشد"))
    except Exception as e:
        out.append(f"get_chats ERROR: {e}")

    await message.answer("\n\n".join(out)[:3500])


@dp.message(Command("dbg2"))
async def dbg2_handler(message: Message):
    """/dbg2 -> سورس و سیگنچر متدهای مربوط به اعضا در spluslib"""
    if not is_admin(message):
        return
    if userbot is None:
        await message.answer("یوزربات بالا نیست.")
        return

    import inspect

    out = []
    try:
        out.append("sig: " + str(inspect.signature(userbot.get_chat_members)))
        src = inspect.getsource(userbot.get_chat_members)
        out.append("SOURCE:\n" + src[:1800])
    except Exception as e:
        out.append(f"source ERROR: {e}")

    try:
        base = getattr(userbot, "_client", None)
        names = [
            n for n in dir(base)
            if any(k in n.lower() for k in ("particip", "member", "permission"))
        ]
        out.append("_client methods: " + repr(names))
    except Exception as e:
        out.append(f"_client ERROR: {e}")

    text = "\n\n".join(out)
    for i in range(0, len(text), 3500):
        await message.answer(text[i:i + 3500])


@dp.message(Command("dbg3"))
async def dbg3_handler(message: Message):
    """/dbg3 [chat] [user_id] -> امتحان چند راه چک عضویت با اکانت"""
    if not is_admin(message):
        return
    if userbot is None:
        await message.answer("یوزربات بالا نیست.")
        return

    import importlib

    parts = (message.text or "").split()
    chat = parts[1] if len(parts) > 1 else "@Meme_Goblin"
    uid = int(parts[2]) if len(parts) > 2 else int(message.from_user.id)
    base = userbot._client
    out = [f"chat={chat} user_id={uid}"]

    async def attempt(name, fn):
        try:
            res = await fn()
            out.append(f"✅ {name}: {repr(res)[:350]}")
        except Exception as e:
            out.append(f"❌ {name}: {type(e).__name__}: {str(e)[:200]}")

    types = functions = None
    try:
        m = type(base).__module__
        i = m.find("._base")
        root = m[: i + 6]
        types = importlib.import_module(root + ".tl.types")
        functions = importlib.import_module(root + ".tl.functions")
    except Exception as e:
        out.append(f"❌ import tl: {e}")

    async def listp(**kw):
        return [
            (u.id, getattr(u, "first_name", None))
            async for u in base.iter_participants(chat, limit=5, **kw)
        ]

    await attempt("wrapper admins", lambda: userbot.get_chat_members(chat, limit=5, filter_admins=True))
    await attempt("iter_participants", lambda: listp())
    await attempt("iter_participants search=''", lambda: listp(search=""))

    if types is not None:
        await attempt("filter Recent", lambda: listp(filter=types.ChannelParticipantsRecent()))
        await attempt("filter Admins", lambda: listp(filter=types.ChannelParticipantsAdmins()))
        await attempt("filter Search", lambda: listp(filter=types.ChannelParticipantsSearch("")))

    if types is not None and functions is not None:
        async def raw_participants():
            ent = await base.get_input_entity(chat)
            return await base(
                functions.channels.GetParticipantsRequest(
                    ent, types.ChannelParticipantsRecent(), 0, 5, 0
                )
            )

        async def common_chats():
            u = await base.get_input_entity(uid)
            return await base(functions.messages.GetCommonChatsRequest(u, 0, 100))

        await attempt("raw GetParticipants", raw_participants)
        await attempt("GetCommonChats", common_chats)

    async def admin_log():
        return [
            repr(e)[:150]
            async for e in base.iter_admin_log(chat, join=True, leave=True, limit=5)
        ]

    await attempt("admin log", admin_log)
    await attempt("get_input_entity(user)", lambda: base.get_input_entity(uid))

    text = "\n\n".join(out)
    for k in range(0, len(text), 3500):
        await message.answer(text[k:k + 3500])


@dp.message(Command("dbg4"))
async def dbg4_handler(message: Message):
    """/dbg4 USER_ID [chat] -> آیا کاربر در لیست اعضا هست؟ لیست کامل است؟"""
    if not is_admin(message):
        return
    if userbot is None:
        await message.answer("یوزربات بالا نیست.")
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("فرمت: /dbg4 USER_ID [@Meme_Goblin]")
        return
    uid = int(parts[1])
    chat = parts[2] if len(parts) > 2 else "@Meme_Goblin"

    out = []
    try:
        info = await userbot.get_chat_info(chat)
        out.append(f"participants_count (طبق سروش): {info.get('participants_count')}")
    except Exception as e:
        out.append(f"get_chat_info ERROR: {e}")

    try:
        ids = await get_member_ids(chat, force=True)
        out.append(f"تعداد آیدی در لیست Recent: {len(ids)}")
        out.append(f"کاربر {uid} در لیست هست؟ {uid in ids}")
        out.append("نمونه آیدی‌ها: " + str(sorted(ids)[:8]))
    except Exception as e:
        out.append(f"لیست ERROR: {e}")

    await message.answer("\n".join(out))


@dp.message(Command("delchannel"))
async def delchannel_handler(message: Message):
    if not is_admin(message):
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("فرمت: /delchannel https://splus.ir/Meme_Goblin")
        return

    channels = load_channels()
    new = [c for c in channels if c["link"] != parts[1]]
    save_channels(new)

    if len(new) == len(channels):
        await message.answer("❌ همچین کانالی در لیست نیست.")
    else:
        await message.answer("🗑 کانال حذف شد.")


@dp.message(Command("channels"))
async def channels_handler(message: Message):
    if not is_admin(message):
        return

    channels = load_channels()
    if not channels:
        await message.answer("📋 هنوز کانالی برای جوین اجباری ثبت نشده.")
        return

    text = "\n".join(f"{i}. {c['link']}  ({c['chat_id']})" for i, c in enumerate(channels, 1))
    await message.answer("📋 کانال‌های جوین اجباری:\n\n" + text)


@dp.message(Command("cancel"))
async def cancel_handler(message: Message):
    global waiting_for_start_text, waiting_for_broadcast_text

    if not is_admin(message):
        return

    waiting_for_start_text = False
    waiting_for_broadcast_text = False
    await message.answer("❌ عملیات لغو شد.")


@dp.message(Command("starttext"))
async def starttext_handler(message: Message):
    if not is_admin(message):
        return

    await message.answer(
        "📋 متن فعلی استارت:\n\n"
        + get_start_text()
    )


@dp.message(Command("stats"))
async def stats_handler(message: Message):
    if not is_admin(message):
        return

    users = load_users()

    await message.answer(f"📊 آمار:\n👥 کاربران: {len(users)}")



@dp.message(Command("status"))
async def status_handler(message: Message):
    if not is_admin(message):
        return

    users = load_users()
    await message.answer(
        f"📊 آمار:\n👥 کاربران: {len(set(users))}"
    )

@dp.message()
async def message_handler(message: Message):
    global waiting_for_start_text, waiting_for_broadcast_text

    if is_admin(message) and waiting_for_start_text and message.text:
        waiting_for_start_text = False

        with open(START_FILE, "w", encoding="utf-8") as f:
            f.write(message.text)

        await message.answer("✅ متن استارت با موفقیت تغییر کرد!")
        return

    if is_admin(message) and waiting_for_broadcast_text and message.text:
        waiting_for_broadcast_text = False

        users = load_users()
        success = 0
        failed = 0

        await message.answer(f"⏳ در حال ارسال به {len(users)} کاربر...")

        for user_id in users:
            try:
                await bot.send_message(chat_id=int(user_id), text=message.text)
                success += 1
            except Exception as e:
                print("BROADCAST ERROR:", user_id, e)
                failed += 1

            await asyncio.sleep(0.05)

        await message.answer(
            f"✅ اطلاعیه ارسال شد.\n"
            f"موفق: {success}\n"
            f"ناموفق: {failed}"
        )
        return

    if not message.video:
        return

    if not await ensure_joined(message):
        return

    await message.answer("⏳ ویدئو دریافت شد، دارم تبدیلش می‌کنم...")

    input_file = f"input_{message.message_id}.mp4"
    output_file = f"output_{message.message_id}.mp4"

    try:
        file = await bot.get_file(message.video.file_id)

        await bot.download_file(file, input_file)

        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", input_file,
            "-vf",
            "scale=720:720:force_original_aspect_ratio=increase,crop=720:720",
            "-r", "30",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "26",
            "-c:a", "aac",
            "-movflags", "+faststart",
            output_file
        )
        await process.wait()

        if process.returncode != 0:
            raise Exception("ffmpeg failed")

        await bot.send_video_note(
            chat_id=message.chat.id,
            video_note=output_file
        )

        await message.answer("✅ آماده شد!")

    except Exception as e:
        print("ERROR:", e)
        await message.answer("❌ هنگام تبدیل ویدئو مشکلی پیش آمد.")

    finally:
        if os.path.exists(input_file):
            os.remove(input_file)

        if os.path.exists(output_file):
            os.remove(output_file)


async def main():
    print("BOT VERSION: 3")
    await start_userbot()
    print("Bot is running...")
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
