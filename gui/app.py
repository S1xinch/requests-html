"""
requests-html Desktop

A Tkinter GUI on top of the requests_html library: fetch a URL, optionally
render its JavaScript, then explore the result with CSS selectors, XPath,
links, and raw/rendered text/HTML -- without writing any Python.

Packaged into a standalone Windows .exe via PyInstaller
(see .github/workflows/build-exe.yml).
"""
import asyncio
import os
import queue
import sys
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

# So this runs both from source (python gui/app.py) and once frozen by
# PyInstaller, where the repo root needs to be on sys.path to find
# requests_html when run unpackaged/for local testing.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from requests_html import HTMLSession  # noqa: E402

APP_TITLE = "requests-html Desktop"


class RequestsHtmlApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1000x700")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.current_response = None
        self.job_queue = queue.Queue()
        self.result_queue = queue.Queue()

        # requests_html's synchronous HTMLSession does asyncio.get_event_loop()
        # under the hood, which only auto-creates a loop on the main thread.
        # Network/render calls run on this one dedicated worker thread instead
        # (so the UI stays responsive), which sets up its own loop once at
        # start and reuses it for every job -- mixing loops across threads is
        # what causes the "no current event loop" crashes documented in
        # README's Known Issues (#294).
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

        self._build_ui()
        self._poll_result_queue()

    # --- UI construction -------------------------------------------------

    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="URL:").pack(side="left")
        self.url_var = tk.StringVar(value="https://example.com")
        url_entry = ttk.Entry(top, textvariable=self.url_var)
        url_entry.pack(side="left", fill="x", expand=True, padx=6)
        url_entry.bind("<Return>", lambda e: self.fetch())

        self.render_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Render JavaScript", variable=self.render_var).pack(
            side="left", padx=6
        )

        self.fetch_btn = ttk.Button(top, text="Fetch", command=self.fetch)
        self.fetch_btn.pack(side="left", padx=6)

        self.status_var = tk.StringVar(value="Ready.")
        status = ttk.Label(self, textvariable=self.status_var, anchor="w", relief="sunken")
        status.pack(fill="x", side="bottom")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.html_text = self._make_text_tab("HTML")
        self.text_tab = self._make_text_tab("Text")
        self.links_tab = self._make_text_tab("Links")

        _, self.find_entry, self.find_results = self._make_query_tab("CSS Select", self.run_find)
        _, self.xpath_entry, self.xpath_results = self._make_query_tab("XPath", self.run_xpath)

        self._make_settings_tab()

    def _make_text_tab(self, label):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=label)
        text = scrolledtext.ScrolledText(frame, wrap="word")
        text.pack(fill="both", expand=True)

        btns = ttk.Frame(frame)
        btns.pack(fill="x")
        ttk.Button(
            btns, text="Save to file...", command=lambda: self._save_text(text)
        ).pack(side="right", padx=4, pady=4)
        return text

    def _make_query_tab(self, label, run_callback):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=label)

        bar = ttk.Frame(frame, padding=4)
        bar.pack(fill="x")
        entry = ttk.Entry(bar)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: run_callback())
        ttk.Button(bar, text="Run", command=run_callback).pack(side="left", padx=4)

        results = scrolledtext.ScrolledText(frame, wrap="word")
        results.pack(fill="both", expand=True)
        return frame, entry, results

    def _make_settings_tab(self):
        frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(frame, text="Settings")

        ttk.Label(frame, text="Chromium executable path (optional):").pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "If rendering fails to download Chromium, point this at an installed\n"
                "Chrome/Chromium instead, e.g.\n"
                "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
            ),
            foreground="gray",
        ).pack(anchor="w", pady=(0, 4))

        self.exe_path_var = tk.StringVar(value=os.environ.get("REQUESTS_HTML_CHROMIUM_PATH", ""))
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.exe_path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse...", command=self._browse_chromium).pack(side="left", padx=4)

        return frame

    def _browse_chromium(self):
        path = filedialog.askopenfilename(title="Select Chrome/Chromium executable")
        if path:
            self.exe_path_var.set(path)

    def _save_text(self, widget):
        content = widget.get("1.0", "end-1c")
        if not content.strip():
            messagebox.showinfo(APP_TITLE, "Nothing to save yet.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".txt")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self.status_var.set(f"Saved to {path}")

    # --- Worker thread: owns the session, the event loop, and all network IO

    def _worker_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        session = HTMLSession()

        while True:
            job = self.job_queue.get()
            if job is None:
                session.close()
                break

            kind, payload = job
            try:
                if kind == "fetch":
                    url, should_render, exe_path = payload
                    if exe_path:
                        os.environ["REQUESTS_HTML_CHROMIUM_PATH"] = exe_path
                    r = session.get(url, timeout=20)
                    if should_render:
                        r.html.render(timeout=30)
                    self.result_queue.put(("fetch_done", r))
            except Exception as exc:  # noqa: BLE001 -- surfaced to the user, not swallowed
                self.result_queue.put(("fetch_error", exc))

    def _on_close(self):
        self.job_queue.put(None)
        self.destroy()

    # --- Actions triggered from the UI (main thread) ----------------------

    def fetch(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning(APP_TITLE, "Enter a URL first.")
            return

        self.fetch_btn.state(["disabled"])
        self.status_var.set(f"Fetching {url}...")
        exe_path = self.exe_path_var.get().strip() or None
        self.job_queue.put(("fetch", (url, self.render_var.get(), exe_path)))

    def run_find(self):
        self._run_query(self.find_entry, self.find_results, mode="css")

    def run_xpath(self):
        self._run_query(self.xpath_entry, self.xpath_results, mode="xpath")

    def _run_query(self, entry, results_widget, mode):
        if self.current_response is None:
            messagebox.showinfo(APP_TITLE, "Fetch a page first.")
            return
        selector = entry.get().strip()
        if not selector:
            return

        try:
            html = self.current_response.html
            elements = html.find(selector) if mode == "css" else html.xpath(selector)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Query failed:\n{exc}")
            return

        results_widget.delete("1.0", "end")
        if not elements:
            results_widget.insert("end", "(no matches)")
            return

        for i, el in enumerate(elements):
            if isinstance(el, str):
                results_widget.insert("end", f"[{i}] {el}\n\n")
                continue
            results_widget.insert("end", f"[{i}] <{el.tag}> {el.attrs}\n")
            text = el.text.strip()
            if text:
                results_widget.insert("end", f"    text: {text[:300]}\n")
            results_widget.insert("end", "\n")

    # --- Main-thread polling of results coming off the worker thread -----

    def _poll_result_queue(self):
        try:
            while True:
                kind, payload = self.result_queue.get_nowait()
                if kind == "fetch_done":
                    self._on_fetch_done(payload)
                elif kind == "fetch_error":
                    self._on_fetch_error(payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_result_queue)

    def _on_fetch_done(self, response):
        self.current_response = response
        self.fetch_btn.state(["!disabled"])
        self.status_var.set(f"{response.status_code} \u2014 {response.url}")

        self.html_text.delete("1.0", "end")
        self.html_text.insert("end", response.html.html)

        self.text_tab.delete("1.0", "end")
        self.text_tab.insert("end", response.html.full_text)

        self.links_tab.delete("1.0", "end")
        for link in sorted(response.html.absolute_links):
            self.links_tab.insert("end", link + "\n")

    def _on_fetch_error(self, exc):
        self.fetch_btn.state(["!disabled"])
        self.status_var.set("Error.")
        messagebox.showerror(
            APP_TITLE, f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc(limit=3)}"
        )


def main():
    app = RequestsHtmlApp()
    app.mainloop()


if __name__ == "__main__":
    main()
