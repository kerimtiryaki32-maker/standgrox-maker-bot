"""EN/TR desktop login. Secrets stay out of logs and source files."""
import queue
import threading
import tkinter as tk
from tkinter import ttk

from credentials import Credentials, CredentialStore, validate_connection
from standx_client import StandXClient

TEXT = {
    "EN": {
        "title": "Sign in to Standgrox Maker Bot", "token": "StandX API Token",
        "key": "Bot signing key (STANDX_SIGN_KEY_HEX)",
        "note": "Use the token and bot signing key from the same login session.\nDo not enter your wallet private key or recovery phrase.",
        "remember": "Remember on this Windows account", "show": "Show credentials",
        "login": "Verify connection and sign in", "forget": "Delete saved credentials",
        "checking": "Checking account access… No orders are sent.",
        "invalid": "Enter a token and a 64-character hexadecimal bot signing key.",
        "failed": "Connection could not be verified. Check your token, expiry and network.",
        "saved": "Saved credentials loaded. Verify to continue.",
        "env": "Existing local .env credentials loaded. Nothing has been saved automatically.",
        "store_failed": "Windows secure storage is unavailable or failed. Uncheck Remember to continue without saving.",
        "deleted": "Saved credentials deleted. Your existing .env, if any, is unchanged.",
        "key_note": "Login checks token access and key format; it does not test signed trading requests.",
    },
    "TR": {
        "title": "Standgrox Maker Bot'a giriş", "token": "StandX API Token",
        "key": "Bot imza anahtarı (STANDX_SIGN_KEY_HEX)",
        "note": "Aynı giriş oturumuna ait token ve bot imza anahtarını kullanın.\nCüzdan private key'i veya kurtarma kelimelerini girmeyin.",
        "remember": "Bu Windows hesabında hatırla", "show": "Bilgileri göster",
        "login": "Bağlantıyı doğrula ve giriş yap", "forget": "Kayıtlı bilgileri sil",
        "checking": "Hesap erişimi kontrol ediliyor… Emir gönderilmez.",
        "invalid": "Token ve 64 karakterlik hexadecimal bot imza anahtarı girin.",
        "failed": "Bağlantı doğrulanamadı. Token, geçerlilik süresi ve ağı kontrol edin.",
        "saved": "Kayıtlı bilgiler yüklendi. Devam etmek için doğrulayın.",
        "env": "Mevcut yerel .env bilgileri yüklendi. Otomatik kayıt yapılmadı.",
        "store_failed": "Windows güvenli depolama kullanılamıyor veya başarısız. Kaydetmeden devam etmek için Hatırla seçimini kaldırın.",
        "deleted": "Kayıtlı bilgiler silindi. Mevcut .env dosyanız değiştirilmedi.",
        "key_note": "Giriş, token erişimini ve anahtar biçimini kontrol eder; imzalı işlem isteği göndermez.",
    },
}


class LoginWindow:
    def __init__(self, asset_path, language="EN"):
        self.root = tk.Tk()
        self.root.title("Standgrox Maker Bot")
        self.root.geometry("660x560")
        self.root.minsize(620, 530)
        self.root.configure(bg="#050b08")
        try:
            self.icon = tk.PhotoImage(file=str(asset_path))
            self.root.iconphoto(True, self.icon)
        except tk.TclError:
            pass
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#050b08")
        style.configure("TLabel", background="#050b08", foreground="#f4fff7", font=("Segoe UI", 11))
        style.configure("TCheckbutton", background="#050b08", foreground="#f4fff7")
        style.map("TCheckbutton", background=[("active", "#0c1b14")])
        style.configure("TButton", background="#164b30", foreground="#ffffff", padding=9)
        style.map("TButton", background=[("active", "#205e3d")])
        self.language = tk.StringVar(value=language)
        self.token, self.key = tk.StringVar(), tk.StringVar()
        self.remember, self.show = tk.BooleanVar(), tk.BooleanVar()
        self.status = tk.StringVar()
        self.credentials = None
        self.events = queue.Queue()
        self.busy = False
        self.store = CredentialStore()
        body = ttk.Frame(self.root, padding=24)
        body.pack(fill="both", expand=True)
        ttk.Combobox(body, textvariable=self.language, values=("EN", "TR"),
                     state="readonly", width=5).pack(anchor="e")
        self.labels = {}
        for name in ("title", "note", "token"):
            label = ttk.Label(body, wraplength=595)
            label.pack(anchor="w", pady=(8, 5))
            self.labels[name] = label
        self.token_entry = ttk.Entry(body, textvariable=self.token, show="•")
        self.token_entry.pack(fill="x")
        self.labels["key"] = ttk.Label(body)
        self.labels["key"].pack(anchor="w", pady=(12, 5))
        self.key_entry = ttk.Entry(body, textvariable=self.key, show="•")
        self.key_entry.pack(fill="x")
        self.show_button = ttk.Checkbutton(body, variable=self.show, command=self.toggle_show)
        self.show_button.pack(anchor="w", pady=(9, 0))
        self.remember_button = ttk.Checkbutton(body, variable=self.remember)
        self.remember_button.pack(anchor="w", pady=7)
        if not self.store.available:
            self.remember_button.configure(state="disabled")
        self.login_button = ttk.Button(body, command=self.submit)
        self.login_button.pack(fill="x", pady=5)
        self.forget_button = ttk.Button(body, command=self.forget)
        self.forget_button.pack(fill="x", pady=5)
        ttk.Label(body, textvariable=self.status, wraplength=595).pack(anchor="w", pady=8)
        self.labels["key_note"] = ttk.Label(body, wraplength=595)
        self.labels["key_note"].pack(anchor="w")
        self.status_key = None
        try:
            stored = self.store.load()
            if stored:
                self.token.set(stored.token)
                self.key.set(stored.sign_key_hex)
                self.remember.set(True)
                self.status_key = "saved"
            else:
                try:
                    existing = Credentials.from_environment()
                    self.token.set(existing.token)
                    self.key.set(existing.sign_key_hex)
                    self.status_key = "env"
                except ValueError:
                    pass
        except Exception:
            self.status_key = "store_failed"
        self.language.trace_add("write", lambda *_: self.translate())
        self.translate()
        self._selected_language = language
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.poll)

    def close(self):
        if self.busy:
            return
        self._selected_language = self.language.get()
        self.root.destroy()

    def translate(self):
        text = TEXT[self.language.get()]
        for name, label in self.labels.items():
            label.configure(text=text[name])
        for widget, key in ((self.login_button, "login"), (self.forget_button, "forget"),
                            (self.remember_button, "remember"), (self.show_button, "show")):
            widget.configure(text=text[key])
        self.status.set(text.get(self.status_key, ""))

    def toggle_show(self):
        for entry in (self.token_entry, self.key_entry):
            entry.configure(show="" if self.show.get() else "•")

    def set_busy(self, value):
        self.busy = value
        for widget in (self.token_entry, self.key_entry, self.login_button, self.forget_button):
            widget.configure(state="disabled" if value else "normal")
        self.remember_button.configure(state="disabled" if value or not self.store.available else "normal")

    def forget(self):
        try:
            self.store.delete()
            self.token.set("")
            self.key.set("")
            self.remember.set(False)
            self.status_key = "deleted"
        except Exception:
            self.status_key = "store_failed"
        self.translate()

    def submit(self):
        if self.busy:
            return
        try:
            credentials = Credentials.parse(self.token.get(), self.key.get())
        except ValueError:
            self.status_key = "invalid"
            self.translate()
            return
        self.set_busy(True)
        self.status_key = "checking"
        self.translate()
        remember = self.remember.get()
        def check():
            try:
                validate_connection(credentials, StandXClient)
            except Exception:
                self.events.put(("failed", None))
                return
            try:
                if remember:
                    self.store.save(credentials)
                else:
                    self.store.delete()
            except Exception:
                self.events.put(("store_failed", None))
                return
            self.events.put(("ok", credentials))
        threading.Thread(target=check, daemon=True).start()

    def poll(self):
        try:
            result, credentials = self.events.get_nowait()
            if result == "ok":
                self.set_busy(False)
                self.credentials = credentials
                self.token.set("")
                self.key.set("")
                self.close()
                return
            self.set_busy(False)
            self.status_key = result
            self.translate()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def run(self):
        self.root.mainloop()
        return self.credentials, self.selected_language

    @property
    def selected_language(self):
        return self._selected_language
