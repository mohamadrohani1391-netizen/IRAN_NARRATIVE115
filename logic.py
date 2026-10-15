# -*- coding: utf-8 -*-
"""منطق اصلی ربات: منوی کاربران، ورود ادمین و امکانات مدیریت."""
import hmac
import logging
import re
import threading
import time
from datetime import datetime, timedelta

import config
from rubika_api import RubikaError

log = logging.getLogger("logic")
TEHRAN = timedelta(hours=3, minutes=30)


def norm(text):
    """یکسان‌سازی متن فارسی (ی/ک عربی، نیم‌فاصله، فاصله‌های اضافه)."""
    if not text:
        return ""
    t = text.replace("ي", "ی").replace("ك", "ک")
    t = t.replace("\u200c", " ").replace("\u200f", "").replace("\u200e", "")
    return re.sub(r"\s+", " ", t).strip()


def fmt_time(ts):
    try:
        return (datetime.utcfromtimestamp(int(ts)) + TEHRAN).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return "-"


class BotLogic:
    def __init__(self, api, db):
        self.api = api
        self.db = db
        self.states = {}          # chat_id -> {"state": str, "data": dict}
        self.admin_mode = set()   # چت‌هایی که الان داخل منوی ادمین هستند
        self.fails = {}           # chat_id -> (تعداد خطا, زمان پایان قفل)
        self.max_age = None       # اگر عدد باشد، پیام‌های قدیمی‌تر نادیده گرفته می‌شوند

    # ================= ابزار ارسال =================
    def say(self, chat_id, text, keypad=None):
        text = text or "-"
        chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)]
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            try:
                self.api.send_message(chat_id, chunk, keypad if last else None)
            except RubikaError as exc:
                log.warning("send failed: %s", exc)

    def menu(self, chat_id):
        """نمایش منوی مناسب (ادمین یا کاربر) بدون پیام خوش‌آمد."""
        if chat_id in self.admin_mode:
            self.say(chat_id, f"🛠 منوی ادمین — {self.db.get_setting('bot_name')}\nیک گزینه را انتخاب کنید:",
                     config.ADMIN_KEYPAD)
        else:
            self.say(chat_id, "یکی از گزینه‌ها را انتخاب کنید:", config.USER_KEYPAD)

    def set_state(self, chat_id, state, **data):
        self.states[chat_id] = {"state": state, "data": data}

    def ask(self, chat_id, state, prompt, keypad=None, **data):
        self.set_state(chat_id, state, **data)
        self.say(chat_id, prompt, keypad or config.CANCEL_KEYPAD)

    # ================= ورودی اصلی =================
    def handle_update(self, update):
        if update.get("type") != "NewMessage":
            return
        msg = update.get("new_message") or {}
        chat_id = update.get("chat_id")
        if not chat_id or msg.get("sender_type") not in (None, "User"):
            return
        if chat_id[:1] in ("g", "c"):      # گروه و کانال نادیده گرفته می‌شوند
            return
        if self.max_age:
            try:
                if time.time() - float(msg.get("time", 0)) > self.max_age:
                    return
            except (TypeError, ValueError):
                pass

        text = norm(msg.get("text"))
        file_info = msg.get("file") or None
        message_id = msg.get("message_id")

        is_new = self.db.add_user(chat_id)

        if text.lower() in ("/start", "start", "شروع") or is_new:
            self.states.pop(chat_id, None)
            self.admin_mode.discard(chat_id)
            self.say(chat_id, config.WELCOME_TEXT, config.USER_KEYPAD)
            return

        if text == "شناسه من":
            self.say(chat_id, f"شناسه شما:\n{chat_id}")
            return

        st = self.states.get(chat_id)
        if st and text == config.CANCEL:
            self.states.pop(chat_id, None)
            self.say(chat_id, "عملیات لغو شد.")
            self.menu(chat_id)
            return
        if st:
            self.handle_state(chat_id, st, text, file_info, message_id)
            return

        if text == config.ADMIN_TRIGGER:
            self.begin_admin_login(chat_id)
        elif chat_id in self.admin_mode:
            self.handle_admin_button(chat_id, text)
        else:
            self.handle_user_button(chat_id, text)

    # ================= منوی کاربران =================
    def handle_user_button(self, chat_id, t):
        if "ارسال نظرات" in t:
            self.ask(chat_id, "comment", "📩 نظر یا پیشنهاد خود را بنویسید و ارسال کنید:")
        elif "مشاهده پست" in t:
            self.show_posts(chat_id)
        elif "درباره ما" in t:
            self.say(chat_id, f"ℹ️ درباره {self.db.get_setting('bot_name')}\n\n{self.db.get_setting('about_text')}",
                     config.USER_KEYPAD)
        elif "پشتیبانی" in t:
            self.say(chat_id, f"🛠 پشتیبانی\n\n{self.db.get_setting('support_text')}", config.USER_KEYPAD)
        elif "دریافت کانال" in t:
            ch = self.db.get_setting("channel_text")
            self.say(chat_id, f"📢 کانال ما:\n\n{ch}" if ch else "📢 هنوز کانالی تنظیم نشده است.",
                     config.USER_KEYPAD)
        else:
            self.say(chat_id, "لطفاً از دکمه‌های زیر استفاده کنید 👇", config.USER_KEYPAD)

    def show_posts(self, chat_id):
        posts = self.db.get_posts()
        if not posts:
            self.say(chat_id, "📂 هنوز پستی ثبت نشده است.", config.USER_KEYPAD)
            return
        for p in reversed(posts[-config.MAX_POSTS_SHOWN:]):
            self.send_post(chat_id, p)
            time.sleep(0.2)
        self.say(chat_id, "✅ پایان نمایش پست‌ها.", config.USER_KEYPAD)

    def send_post(self, chat_id, post):
        caption = post.get("caption", "")
        if post.get("file_id"):
            try:
                self.api.send_file(chat_id, post["file_id"], caption)
                return
            except RubikaError as exc:
                log.warning("sendFile failed: %s", exc)
        self.say(chat_id, caption or "(بدون متن)")

    # ================= ورود ادمین =================
    def begin_admin_login(self, chat_id):
        until = self.fails.get(chat_id, (0, 0))[1]
        if until > time.time():
            self.say(chat_id, "⛔ به دلیل تلاش‌های ناموفق، ورود موقتاً قفل شده است. بعداً تلاش کنید.")
            return
        if self.db.is_admin(chat_id):
            self.admin_mode.add(chat_id)
            self.menu(chat_id)
            return
        self.ask(chat_id, "login_name", "👤 لطفاً نام خود را وارد کنید:")

    def _login_fail(self, chat_id):
        count, _ = self.fails.get(chat_id, (0, 0))
        count += 1
        if count >= config.MAX_LOGIN_FAILS:
            self.fails[chat_id] = (0, time.time() + config.LOGIN_LOCK_SECONDS)
            self.say(chat_id, "⛔ تلاش‌های ناموفق زیاد بود. ورود به مدت چند دقیقه قفل شد.", config.USER_KEYPAD)
        else:
            self.fails[chat_id] = (count, 0)
            self.say(chat_id, "❌ نام یا کد رمز صحیح نیست.", config.USER_KEYPAD)

    # ================= حالت‌ها (مراحل گفتگو) =================
    def handle_state(self, chat_id, st, text, file_info, message_id):
        state, data = st["state"], st["data"]

        if state == "comment":
            if not text:
                self.say(chat_id, "لطفاً نظر خود را به صورت متن بنویسید.")
                return
            self.states.pop(chat_id, None)
            self.db.add_comment(chat_id, text)
            self.say(chat_id, "✅ نظر شما ثبت شد. سپاسگزاریم 🌹", config.USER_KEYPAD)
            return

        if state == "login_name":
            self.ask(chat_id, "login_pass", "🔑 لطفاً کد رمز را وارد کنید:", name=text)
            return

        if state == "login_pass":
            self.states.pop(chat_id, None)
            try:   # تلاش برای پاک‌کردن پیام حاوی رمز
                if message_id:
                    self.api.delete_message(chat_id, message_id)
            except RubikaError:
                pass
            cred = self.db.get_credentials()
            name_ok = norm(data.get("name")) == norm(cred["name"])
            pass_ok = hmac.compare_digest((text or "").strip().encode("utf-8"),
                                          cred["password"].strip().encode("utf-8"))
            if name_ok and pass_ok:
                self.fails.pop(chat_id, None)
                self.db.add_admin(chat_id)
                self.admin_mode.add(chat_id)
                self.say(chat_id, "✅ ورود موفق. خوش آمدید!")
                self.menu(chat_id)
            else:
                self._login_fail(chat_id)
            return

        # --- از اینجا به بعد فقط ادمین‌ها ---
        if chat_id not in self.admin_mode:
            self.states.pop(chat_id, None)
            return

        if state in ("edit_name", "edit_about", "set_channel", "set_support", "edit_pass"):
            if not text:
                self.say(chat_id, "لطفاً متن را ارسال کنید.")
                return
            self.states.pop(chat_id, None)
            if state == "edit_pass":
                self.db.set_password(text.strip())
                try:
                    if message_id:
                        self.api.delete_message(chat_id, message_id)
                except RubikaError:
                    pass
                self.say(chat_id, "✅ کد رمز جدید ذخیره شد.")
            else:
                key = {"edit_name": "bot_name", "edit_about": "about_text",
                       "set_channel": "channel_text", "set_support": "support_text"}[state]
                self.db.set_setting(key, text)
                self.say(chat_id, "✅ ذخیره شد.")
            self.menu(chat_id)
            return

        if state == "post_media":
            if file_info and file_info.get("file_id"):
                self.ask(chat_id, "post_caption", "✍️ حالا کپشن پست را بنویسید:",
                         file_id=file_info["file_id"])
            elif text == config.NO_FILE:
                self.ask(chat_id, "post_caption", "✍️ متن/کپشن پست را بنویسید:", file_id=None)
            else:
                self.say(chat_id, "عکس/فایل را بفرستید یا «بدون فایل» را بزنید.", config.NO_FILE_KEYPAD)
            return

        if state == "post_caption":
            if not text:
                self.say(chat_id, "لطفاً کپشن را به صورت متن بنویسید.")
                return
            self.states.pop(chat_id, None)
            self.db.add_post(text, data.get("file_id"))
            self.say(chat_id, "✅ پست جدید ذخیره شد و در «مشاهده پست‌ها» نمایش داده می‌شود.")
            self.menu(chat_id)
            return

        if state == "add_admin":
            if not text or " " in text:
                self.say(chat_id, "شناسه معتبر نیست. شناسه یک رشته بدون فاصله است (مثل b0xxxx).")
                return
            self.states.pop(chat_id, None)
            if self.db.add_admin(text):
                self.say(chat_id, "✅ ادمین جدید اضافه شد.")
                try:
                    self.api.send_message(text, "🎉 شما به عنوان ادمین اضافه شدید. برای ورود بنویسید: منوی ادمین")
                except RubikaError:
                    pass
            else:
                self.say(chat_id, "این شناسه از قبل ادمین است.")
            self.menu(chat_id)
            return

        if state == "broadcast":
            if not text:
                self.say(chat_id, "لطفاً متن پیام را بنویسید.")
                return
            n = len(self.db.get_users())
            self.ask(chat_id, "broadcast_confirm",
                     f"پیام زیر برای {n} کاربر ارسال می‌شود:\n\n{text}\n\nتأیید می‌کنید؟",
                     config.CONFIRM_KEYPAD, text=text)
            return

        if state == "broadcast_confirm":
            if text == config.YES:
                self.states.pop(chat_id, None)
                self.say(chat_id, "⏳ ارسال شروع شد؛ پس از پایان گزارش می‌دهم.")
                threading.Thread(target=self._broadcast, args=(chat_id, data["text"]), daemon=True).start()
            else:
                self.say(chat_id, "برای ارسال، دکمه تأیید را بزنید یا «انصراف».", config.CONFIRM_KEYPAD)
            return

    # ================= منوی ادمین =================
    def handle_admin_button(self, chat_id, t):
        c = config
        if "ویرایش نام ربات" in t:
            self.ask(chat_id, "edit_name",
                     f"نام فعلی: {self.db.get_setting('bot_name')}\nنام جدید را بنویسید:")
        elif "ویرایش درباره ما" in t:
            self.ask(chat_id, "edit_about", "متن جدید «درباره ما» را بنویسید:")
        elif "افزودن پست" in t:
            self.ask(chat_id, "post_media", "📎 عکس یا فایل پست را بفرستید (یا «بدون فایل» را بزنید):",
                     c.NO_FILE_KEYPAD)
        elif "مشاهده نظرات" in t:
            self.show_comments(chat_id)
        elif "افزودن ادمین" in t:
            self.ask(chat_id, "add_admin",
                     "شناسه (chat id) فرد را بفرستید.\nاو می‌تواند با نوشتن «شناسه من» در ربات آن را ببیند.")
        elif "ویرایش کد رمز" in t:
            self.ask(chat_id, "edit_pass", "کد رمز جدید را بنویسید:")
        elif "پیام همگانی" in t:
            self.ask(chat_id, "broadcast", "📣 متن پیام همگانی را بنویسید:")
        elif "تنظیم دریافت کانال" in t:
            self.ask(chat_id, "set_channel", "متن/لینک کانال را بنویسید (مثلاً https://rubika.ir/...):")
        elif "ویرایش پشتیبانی" in t:
            self.ask(chat_id, "set_support", "متن جدید بخش پشتیبانی را بنویسید:")
        elif "خروج" in t:
            self.admin_mode.discard(chat_id)
            self.say(chat_id, "از منوی ادمین خارج شدید.", c.USER_KEYPAD)
        elif any(k in t for k in ("ارسال نظرات", "مشاهده پست", "درباره ما", "پشتیبانی", "دریافت کانال")):
            self.handle_user_button(chat_id, t)
        else:
            self.menu(chat_id)

    def show_comments(self, chat_id):
        comments = self.db.get_comments()
        if not comments:
            self.say(chat_id, "💬 هنوز نظری ثبت نشده است.", config.ADMIN_KEYPAD)
            return
        shown = comments[-config.MAX_COMMENTS_SHOWN:]
        start = len(comments) - len(shown) + 1
        lines = [f"💬 {len(comments)} نظر ثبت شده (نمایش {len(shown)} مورد آخر):\n"]
        for i, c in enumerate(shown, start):
            lines.append(f"#{i} | {fmt_time(c['time'])} | {c['chat_id']}\n{c['text']}\n")
        self.say(chat_id, "\n".join(lines), config.ADMIN_KEYPAD)

    def _broadcast(self, admin_id, text):
        ok = fail = 0
        for uid in self.db.get_users():
            try:
                self.api.send_message(uid, text)
                ok += 1
            except RubikaError:
                fail += 1
            time.sleep(0.15)
        self.say(admin_id, f"📣 ارسال همگانی تمام شد.\n✅ موفق: {ok}\n❌ ناموفق: {fail}", config.ADMIN_KEYPAD)
