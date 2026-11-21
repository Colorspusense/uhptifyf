from collections import Counter
from pathlib import Path
import os
import tempfile
from shutil import which
import re
import secrets
import string
import subprocess
import threading

import pyperclip
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static

SITE = "https://tmails.netlify.app"

CAT_ART = r"""⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⠤⢤⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠰⠁⠀⠘⡄⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠇⠀⠀⠀⡃⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠸⠀⠀⠀⠀⡀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⣄⡇⠀⠀⠀⠀⡇⠀⠀
⠀⢀⠤⠤⢤⡤⠄⣀⡠⠤⠒⠒⠂⠀⠀⠐⠒⠐⢊⠕⠉⠀⠁⠀⠀⠀⠀⡇⠀⠀
⠀⢸⠀⠀⠀⠈⢱⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠆⠀⠀⡄⠀⠀⠀⢠⠅⠀⠀
⠀⠀⢆⠀⠀⠠⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢢⢠⠃⠀⠀⠀⢸⠀⠀⠀
⠀⠀⠈⢆⠀⡎⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠎⠀⠀⠀⠀⡆⠀⠀⠀
⠀⠀⠀⠈⢣⠀⠀⠀⠀⣠⡤⡄⠀⠀⠀⠀⠀⣯⣽⣦⠀⠸⡀⠀⠀⢀⣇⣀⡀⠀
⠀⠀⠀⠀⣀⡀⠀⠀⢰⣿⣷⣿⠀⠀⠐⠖⠀⠹⢿⠿⠀⢐⣉⠉⠀⠀⠱⡀⠀⠀
⠒⠈⠉⠀⣇⠤⠀⠀⠘⠿⠿⠃⠀⠀⠠⠆⠀⠀⠀⠀⠀⡢⢏⠀⠉⠐⠂⢵⢀⡀
⠀⠠⠒⠉⣸⢞⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⠔⠁⠀⠈⠒⢄⡀⠀⡇⠀
⠀⠀⢠⠎⠀⢀⡏⠒⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠀⠀⠀⠀⠀⠀⠀⠈⠀⡇⠀
⠀⠀⠁⠀⠀⠘⡅⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢰⠀⠀
⠀⠀⠀⠀⠀⠀⢣⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠑⢄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⡠⠂⠀⡎⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⢇⠀⠀⠀⢠⠄⢀⡀⠀⠀⠀⠀⡔⠊⠁⠱⠤⠴⠁⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⢦⠀⠀⠘⠀⠀⠱⡀⠀⠀⢠⠁⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢑⣲⠊⠀⠀⠀⠈⣖⣶⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀"""


def take_card(path=None):
    path = Path(path) if path is not None else Path(__file__).resolve().with_name('card.txt')
    lock = path.with_name(path.name + '.lock')
    # Serialize consumption across script instances; fail closed on a stale lock.
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    temporary = None
    try:
        lines = path.read_bytes().splitlines(keepends=True)
        index = next((i for i, line in enumerate(lines) if line.strip()), None)
        if index is None:
            raise ValueError('card.txt has no entries.')
        try:
            entry = lines[index].decode('ascii').strip()
        except UnicodeDecodeError:
            raise ValueError('Invalid card entry format; card.txt was not changed.') from None
        if not re.fullmatch(r'[0-9]{12,19}\|(?:0[1-9]|1[0-2])\|[0-9]{4}\|[0-9]{3,4}', entry):
            raise ValueError('Invalid card entry format; card.txt was not changed.')
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.card-', delete=False) as output:
            temporary = Path(output.name)
            output.write(b''.join(lines[:index] + lines[index + 1:]))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        return entry
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def extract_code(body):
    # Require a code on its own line; ignore URLs and ambiguous messages.
    codes = set(re.findall(r"(?m)^[ \t]*([0-9]{4,8})[ \t]*\r?$", body))
    return next(iter(codes)) if len(codes) == 1 else None


def watch(page, stop, on_email, on_code, on_error):
    # The landing page initially requests a shared demo inbox.
    api = SITE + "/api/*"
    def empty_demo(route):
        route.fulfill(json=[])

    page.route(api, empty_demo)
    page.goto(SITE)
    page.wait_for_selector("#messagesContainer .empty-state")
    page.unroute(api, empty_demo)

    previous = page.locator("#emailInput").input_value()
    page.get_by_role("button", name="Сгенерировать").click()
    page.wait_for_function(
        "previous => { const email = document.querySelector('#emailInput').value; "
        "return email.includes('@') && email !== previous; }",
        arg=previous,
    )
    on_email(page.locator("#emailInput").input_value())

    seen = Counter()
    last_error = None
    while not stop.is_set():
        error = page.locator(".error-code").all_text_contents()
        if error and error != last_error:
            on_error("Inbox refresh failed: " + " ".join(error))
        last_error = error

        # Snapshot the site's rendered text; never open links or render email HTML.
        rows = page.locator(".message-item").evaluate_all("""items => items.map(item =>
            ['from', 'date', 'subject', 'body'].map(field =>
                item.querySelector('.message-' + field).innerText))""")
        current = Counter(map(tuple, rows))
        for (sender, date, subject, body), copies in (current - seen).items():
            code = extract_code(body)
            if code is not None:
                for _ in range(copies):
                    on_code(code)
        seen |= current
        page.wait_for_timeout(1000)


def generate_credentials():
    username = "user-" + "".join(secrets.choice(string.ascii_lowercase + string.digits)
                                 for _ in range(12))
    groups = (string.ascii_lowercase, string.ascii_uppercase, string.digits, "!@#$%&*-_+")
    password = [secrets.choice(group) for group in groups]
    password.extend(secrets.choice("".join(groups)) for _ in range(16))
    secrets.SystemRandom().shuffle(password)
    return username, "".join(password)


class CopyField(Static):
    DEFAULT_CSS = """
    CopyField { padding: 0 1; height: auto; }
    CopyField:hover { background: $boost; }
    """

    def __init__(self, label, value="", placeholder="…", separator=": ", **kwargs):
        super().__init__(**kwargs)
        self.label, self.value = label, value
        self.placeholder, self.separator = placeholder, separator

    def on_mount(self):
        self.set_value(self.value)

    def set_value(self, value):
        self.value = value
        self.update(Text.assemble((self.label + self.separator, "bold"),
                                  value or self.placeholder))

    def on_click(self):
        if self.value:
            self.app.copy_value(self.label, self.value)


class CheckerApp(App):
    TITLE = "Inbox helper"
    BINDINGS = [("q", "quit", "Quit")]
    CSS = """
    #card_panel { height: auto; margin: 1 0; }
    #card_fields { width: 1fr; height: auto; }
    #cat { width: 34; height: auto; padding: 1; color: #ff69b4; }
    #open_browser { margin: 0 1; }
    #status, #card_status, #copy_hint, #browser_status { padding: 0 1; color: $text-muted; height: auto; }
    """

    def __init__(self):
        super().__init__()
        self.username, self.password = generate_credentials()
        self.stop = threading.Event()

    def compose(self) -> ComposeResult:
        yield CopyField("code", placeholder="*******", separator=":", id="code")
        yield CopyField("Email", id="email")
        yield CopyField("Username", self.username)
        yield CopyField("Password", self.password)
        with Horizontal(id="card_panel"):
            with Vertical(id="card_fields"):
                yield Static("Click a field to copy", id="copy_hint")
                yield CopyField("Number", id="card_number")
                yield CopyField("Month", id="card_month")
                yield CopyField("Year", id="card_year")
                yield CopyField("CVV", id="card_cvv")
            yield Static(CAT_ART, id="cat", markup=False)
        yield Static("", id="card_status", markup=False)
        yield Static("Starting inbox watcher…", id="status")
        yield Button("Open Chrome Incognito", id="open_browser")
        yield Static("", id="browser_status", markup=False)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "open_browser":
            event.button.disabled = True
            self.query_one("#browser_status", Static).update("Opening isolated Chrome…")
            self.open_browser()

    @work(thread=True, exit_on_error=False)
    def open_browser(self):
        process = None
        message = "Chrome session closed."
        try:
            chrome = which("google-chrome") or which("google-chrome-stable")
            if chrome is None:
                raise FileNotFoundError("Google Chrome was not found on PATH.")
            # A fresh profile prevents reuse of any existing Chrome session.
            with tempfile.TemporaryDirectory(prefix="checker-chrome-") as profile:
                try:
                    process = subprocess.Popen(
                        [chrome, f"--user-data-dir={profile}", "--incognito", "--new-window",
                         "--no-first-run", "--no-default-browser-check",
                         "https://app.hoplite.sh", "https://github.com/"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    self.call_from_thread(
                        self.set_browser_status,
                        "Chrome launched. Close this app to end the isolated session.",
                    )
                    while process.poll() is None and not self.stop.wait(0.2):
                        pass
                    if process.poll() not in (None, 0):
                        message = "Chrome exited with an error. Check that a desktop display is available."
                finally:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
        except (OSError, subprocess.SubprocessError) as error:
            message = f"Could not open Chrome: {error}"
        finally:
            if not self.stop.is_set():
                self.call_from_thread(self.browser_finished, message)

    def set_browser_status(self, message):
        self.query_one("#browser_status", Static).update(message)

    def browser_finished(self, message):
        self.set_browser_status(message)
        self.query_one("#open_browser", Button).disabled = False

    def on_mount(self):
        self.email_field = self.query_one("#email", CopyField)
        try:
            card = take_card()
        except (OSError, ValueError) as error:
            self.query_one("#card_status", Static).update(f"Card not loaded: {error}")
        else:
            for name, value in zip(("number", "month", "year", "cvv"), card.split("|")):
                self.query_one(f"#card_{name}", CopyField).set_value(value)
            self.query_one("#card_status", Static).update("Entry removed from card.txt.")
        self.run_watcher()

    def on_unmount(self):
        self.stop.set()

    def copy_value(self, label, value):
        try:
            pyperclip.copy(value)
        except pyperclip.PyperclipException:
            # Fall back to the terminal's OSC 52 clipboard support.
            self.copy_to_clipboard(value)
        self.notify(f"{label} copied")

    def set_status(self, message):
        self.query_one("#status", Static).update(message)

    def add_code(self, code):
        self.query_one("#code", CopyField).set_value(code)

    def show_email(self, email):
        self.email_field.set_value(email)
        self.set_status("Waiting for verification codes…")

    @work(thread=True, exit_on_error=False)
    def run_watcher(self):
        call = self.call_from_thread
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=which("google-chrome"))
                try:
                    watch(browser.new_page(), self.stop,
                          on_email=lambda email: call(self.show_email, email),
                          on_code=lambda code: call(self.add_code, code),
                          on_error=lambda message: call(self.set_status, message))
                finally:
                    browser.close()
        except Exception as error:
            if not self.stop.is_set():
                call(self.set_status, f"Inbox watcher stopped: {error}")


if __name__ == "__main__":
    CheckerApp().run()