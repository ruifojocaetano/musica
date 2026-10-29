import ctypes
import glob
import json
import msvcrt
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# Ferramentas: primeiro as que vêm ao lado do programa (pasta "ferramentas"), depois as do sistema
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(BASE, "ferramentas")


def find_tool(name, *fallbacks):
    for p in [os.path.join(TOOLS, name)] + [q for f in fallbacks for q in glob.glob(os.path.expandvars(f))]:
        if os.path.exists(p):
            return p
    return name


MPV = find_tool("mpv.exe", r"C:\Program Files\MPV Player\mpv.exe")
YTDLP = find_tool("yt-dlp.exe",
                  r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\yt-dlp.yt-dlp_*\yt-dlp.exe")
# o yt-dlp precisa de um runtime de JavaScript (deno) para o YouTube; o mpv herda este PATH
os.environ["PATH"] = TOOLS + os.pathsep + os.environ.get("PATH", "")
DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "MusicaLeve")
DATA_FILE = os.path.join(DATA_DIR, "dados.json")
DL_DIR = os.path.join(os.path.expanduser("~"), "Music", "MusicaLeve")
NOWIN = 0x08000000  # CREATE_NO_WINDOW
SEP = "|||"
VID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|shorts/)([\w-]{11})")
ID_RE = re.compile(r"[\w-]{11}")
FILES = {}  # id do vídeo -> caminho do ficheiro descarregado

# o YouTube dá 403 nos links diretos; os formatos HLS (m3u8) funcionam
FMT_AUDIO = "bestaudio[protocol^=m3u8]/bestaudio/best"
FMT_VIDEO = ("bestvideo[height<=480][protocol^=m3u8]+bestaudio[protocol^=m3u8]"
             "/best[height<=480][protocol^=m3u8]/18/best")
REPEAT_TEXT = ["Repetir: não", "Repetir: fila", "Repetir: uma"]


def fmt(s):
    if s is None:
        return "--:--"
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def vid_of(url):
    m = VID_RE.search(url)
    return m.group(1) if m else None


def local_file(url):
    """Ficheiro já descarregado para este link (ou None)."""
    p = FILES.get(vid_of(url))
    return p if p and os.path.exists(p) else None


class Mpv:
    """Um processo mpv controlado por um named pipe (JSON IPC)."""

    def __init__(self, on_event):
        self.on_event = on_event
        self.proc = None
        self.f = None
        self.lock = threading.Lock()
        self.pipe = r"\\.\pipe\musicaleve-%d-%d" % (os.getpid(), int(time.time()))

    def alive(self):
        return self.proc is not None and self.f is not None and self.proc.poll() is None

    def start(self, audio_only, volume):
        args = [MPV, "--idle=yes", "--force-window=no", "--no-terminal", "--keep-open=no",
                "--input-ipc-server=" + self.pipe, "--volume=%d" % volume,
                "--demuxer-max-bytes=32MiB", "--demuxer-max-back-bytes=8MiB",
                "--ytdl-format=" + (FMT_AUDIO if audio_only else FMT_VIDEO),
                "--script-opts=ytdl_hook-ytdl_path=" + YTDLP]
        if audio_only:
            args.append("--vid=no")
        self.proc = subprocess.Popen(args, creationflags=NOWIN)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                self.f = open(self.pipe, "r+b", buffering=0)
                break
            except OSError:
                time.sleep(0.1)
        else:
            self.proc.kill()
            raise RuntimeError("O mpv não arrancou.")
        threading.Thread(target=self._read, daemon=True).start()
        for i, prop in enumerate(("time-pos", "duration", "pause", "media-title"), 1):
            self.cmd("observe_property", i, prop)

    def cmd(self, *args):
        if not self.alive():
            return
        data = (json.dumps({"command": list(args)}) + "\n").encode()
        try:
            with self.lock:
                self.f.write(data)
        except OSError:
            pass

    def _read(self):
        # Num pipe síncrono uma leitura bloqueada impede as escritas; por isso só
        # se lê quando o PeekNamedPipe diz que há dados.
        kernel = ctypes.windll.kernel32
        kernel.PeekNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                                         ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
                                         ctypes.c_void_p]
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(self.f.fileno()))
        avail = ctypes.c_ulong(0)
        buf = b""
        while self.proc.poll() is None:
            if not kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None):
                break
            if not avail.value:
                time.sleep(0.05)
                continue
            try:
                with self.lock:
                    buf += self.f.read(avail.value)
            except OSError:
                break
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    self.on_event(json.loads(line))
                except ValueError:
                    pass
        self.on_event({"event": "mpv-closed"})

    def close(self):
        self.cmd("quit")
        if self.proc is not None:
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class App:
    def __init__(self, root):
        self.root = root
        d = self.load()
        self.queue = [u for u in d.get("queue", []) if isinstance(u, str)]
        self.idx = d.get("idx", -1)
        if not 0 <= self.idx < len(self.queue):
            self.idx = -1
        self.meta = d.get("meta", {})   # url -> {title, dur}
        self.hist = d.get("hist", {})   # url -> {when, pos, done}
        self.mpv = None
        self.ev_q = queue.Queue()
        self.active = False    # há uma faixa carregada/a carregar
        self.loaded = False
        self.pos = 0.0
        self.dur = 0.0
        self.paused = False
        self.dragging = False
        self.errors = 0
        self.pending = set()
        self.hw = None
        self.ticks = 0
        self.playing_local = False
        self.dl_list = []
        self.dl_lock = threading.Lock()
        self.dl_event = threading.Event()
        self.dl_proc = None
        self.dl_cur = None
        self.dl_pct = 0
        self.closing = False
        self.dl_dir = d.get("dl_dir", DL_DIR)
        self.dl_total = self.dl_n = 0
        FILES.update(d.get("files", {}))
        self.local_ids = set()
        self.scan_local()
        threading.Thread(target=self.dl_worker, daemon=True).start()

        root.title("Rui Caetano Player")
        root.geometry("660x600")
        root.minsize(560, 480)
        self.build(d)
        self.refresh_queue()
        self.fetch_meta(self.queue)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.tick)

    # ---------- dados ----------
    @staticmethod
    def load():
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self):
        keep = set(self.queue) | set(self.hist)
        d = {"queue": self.queue, "idx": self.idx,
             "meta": {u: m for u, m in self.meta.items() if u in keep},
             "hist": self.hist, "audio": self.audio.get(), "shuffle": self.shuffle.get(),
             "repeat": self.repeat, "volume": self.vol.get(), "speed": self.speed.get(),
             "autodl": self.autodl.get(), "dl_dir": self.dl_dir, "files": FILES}
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, DATA_FILE)
        except OSError:
            pass

    # ---------- interface ----------
    def build(self, d):
        r = self.root
        r.columnconfigure(0, weight=1)
        r.rowconfigure(5, weight=1)

        top = ttk.Frame(r)
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 0))
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text="Cola aqui links do YouTube (um por linha; também aceita links de playlist):"
                  ).grid(row=0, column=0, columnspan=2, sticky="w")
        self.text = tk.Text(top, height=3, width=10, wrap="none")
        self.text.grid(row=1, column=0, sticky="ew")
        self.text.bind("<Control-Return>", lambda e: (self.add(), "break")[1])
        self.text.bind("<Return>", lambda e: (self.add(), "break")[1])
        # colar um link adiciona-o logo (o texto só aparece depois do evento de colar)
        self.text.bind("<<Paste>>", lambda e: self.root.after(50, self.add))
        side = ttk.Frame(top)
        side.grid(row=1, column=1, sticky="ns", padx=(6, 0))
        ttk.Button(side, text="＋ Adicionar", command=self.add, takefocus=0).pack(fill="x")
        ttk.Button(side, text="Limpar e tocar", command=lambda: self.add(replace=True),
                   takefocus=0).pack(fill="x", pady=(4, 0))

        self.title_var = tk.StringVar(value="Nada a tocar")
        ttk.Label(r, textvariable=self.title_var, font=("Segoe UI", 11, "bold"),
                  wraplength=620).grid(row=1, column=0, sticky="w", padx=8, pady=(10, 2))

        bar = ttk.Frame(r)
        bar.grid(row=2, column=0, sticky="ew", padx=8)
        bar.columnconfigure(0, weight=1)
        self.scale = ttk.Scale(bar, from_=0, to=100, orient="horizontal", command=self.on_scale)
        self.scale.grid(row=0, column=0, sticky="ew")
        self.scale.bind("<ButtonPress-1>", lambda e: setattr(self, "dragging", True), add="+")
        self.scale.bind("<ButtonRelease-1>", self.on_scale_release, add="+")
        self.time_var = tk.StringVar(value="0:00 / --:--")
        ttk.Label(bar, textvariable=self.time_var, width=16, anchor="e").grid(row=0, column=1, padx=(8, 0))

        ctl = ttk.Frame(r)
        ctl.grid(row=3, column=0, sticky="ew", padx=8, pady=6)
        ttk.Button(ctl, text="⏮", width=3, command=self.prev, takefocus=0).pack(side="left")
        ttk.Button(ctl, text="−10s", width=5, command=lambda: self.seek(-10), takefocus=0).pack(side="left", padx=(4, 0))
        self.btn_pause = ttk.Button(ctl, text="▶", width=3, command=self.toggle_pause, takefocus=0)
        self.btn_pause.pack(side="left", padx=4)
        ttk.Button(ctl, text="+10s", width=5, command=lambda: self.seek(10), takefocus=0).pack(side="left")
        ttk.Button(ctl, text="⏭", width=3, command=self.next, takefocus=0).pack(side="left", padx=4)
        ttk.Button(ctl, text="■", width=3, command=self.stop, takefocus=0).pack(side="left")
        ttk.Label(ctl, text="  Vol").pack(side="left")
        self.vol = tk.IntVar(value=d.get("volume", 70))
        ttk.Scale(ctl, from_=0, to=100, variable=self.vol, length=90,
                  command=lambda v: self.set_volume()).pack(side="left")
        ttk.Button(ctl, text="Histórico", command=self.open_history, takefocus=0).pack(side="right")

        opt = ttk.Frame(r)
        opt.grid(row=4, column=0, sticky="ew", padx=8)
        self.audio = tk.BooleanVar(value=d.get("audio", True))
        self.shuffle = tk.BooleanVar(value=d.get("shuffle", False))
        self.repeat = d.get("repeat", 0)
        self.autodl = tk.BooleanVar(value=d.get("autodl", True))
        ttk.Checkbutton(opt, text="Só áudio (mais leve)", variable=self.audio,
                        command=self.on_audio_toggle, takefocus=0).pack(side="left")
        ttk.Checkbutton(opt, text="Descarregar auto.", variable=self.autodl, takefocus=0).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(opt, text="Aleatório", variable=self.shuffle, takefocus=0).pack(side="left", padx=8)
        self.btn_repeat = ttk.Button(opt, text=REPEAT_TEXT[self.repeat], command=self.cycle_repeat, takefocus=0)
        self.btn_repeat.pack(side="left")
        ttk.Label(opt, text="  Velocidade").pack(side="left")
        self.speed = tk.StringVar(value=d.get("speed", "1.0"))
        cb = ttk.Combobox(opt, textvariable=self.speed, state="readonly", width=5,
                          values=["0.75", "1.0", "1.25", "1.5", "2.0"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self.set_speed())

        qf = ttk.Frame(r)
        qf.grid(row=5, column=0, sticky="nsew", padx=8, pady=6)
        qf.columnconfigure(0, weight=1)
        qf.rowconfigure(0, weight=1)
        self.lb = tk.Listbox(qf, selectmode="extended", activestyle="none", exportselection=False)
        self.lb.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(qf, command=self.lb.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.lb.config(yscrollcommand=sb.set)
        self.lb.bind("<Double-Button-1>", self.on_lb_double)
        self.lb.bind("<Delete>", lambda e: self.remove_selected())
        qb = ttk.Frame(qf)
        qb.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        for label, fn in [("Remover", self.remove_selected), ("▲", lambda: self.move(-1)),
                          ("▼", lambda: self.move(1)), ("Limpar fila", self.clear_queue),
                          ("⬇ Descarregar", self.download_selected), ("Pasta ▾", self.folder_menu)]:
            ttk.Button(qb, text=label, command=fn, takefocus=0).pack(side="left", padx=(0, 4))

        self.status = tk.StringVar(value="Pronto. Cola um link (adiciona logo). Espaço = pausa, ←/→ = ±5s.")
        ttk.Label(r, textvariable=self.status, anchor="w").grid(row=6, column=0, sticky="ew", padx=8, pady=(0, 6))

        self.dl_frame = ttk.Frame(r)
        self.dl_frame.grid(row=7, column=0, sticky="ew", padx=8, pady=(0, 6))
        self.dl_frame.columnconfigure(0, weight=1)
        self.dl_text = tk.StringVar()
        ttk.Label(self.dl_frame, textvariable=self.dl_text, anchor="w").grid(row=0, column=0, sticky="ew")
        self.dl_bar = ttk.Progressbar(self.dl_frame, maximum=100)
        self.dl_bar.grid(row=1, column=0, sticky="ew")
        self.dl_frame.grid_remove()

        r.bind_all("<space>", self.key(self.toggle_pause))
        r.bind_all("<Left>", self.key(lambda: self.seek(-5)))
        r.bind_all("<Right>", self.key(lambda: self.seek(5)))

    def key(self, fn):
        def handler(_e):
            try:
                if isinstance(self.root.focus_get(), (tk.Text, ttk.Combobox)):
                    return None
            except KeyError:
                pass
            fn()
            return "break"
        return handler

    # ---------- fila ----------
    def title_of(self, url):
        return self.meta.get(url, {}).get("title") or url

    def refresh_queue(self):
        sel = self.lb.curselection()
        y = self.lb.yview()[0]
        self.lb.delete(0, "end")
        for i, u in enumerate(self.queue):
            now = i == self.idx and self.active
            s = ("▶ " if now else "   ") + f"{i + 1}. {self.title_of(u)}"
            dur = self.meta.get(u, {}).get("dur")
            if dur:
                s += f"  [{fmt(dur)}]"
            if vid_of(u) in self.local_ids:
                s += "  ✔ offline"
            elif u == self.dl_cur:
                s += f"  ⬇ {int(self.dl_pct)}%"
            elif u in self.dl_list:
                s += "  ⬇ na fila"
            h = self.hist.get(u)
            if h and not h.get("done") and h.get("pos", 0) >= 5 and not now:
                s += f"  ↺ {fmt(h['pos'])}"
            self.lb.insert("end", s)
            if now:
                self.lb.itemconfig(i, background="#cde8ff")
        for i in sel:
            if i < len(self.queue):
                self.lb.selection_set(i)
        self.lb.yview_moveto(y)
        cur = self.cur_url()
        self.title_var.set(self.title_of(cur) if cur and self.active else "Nada a tocar")

    def cur_url(self):
        return self.queue[self.idx] if 0 <= self.idx < len(self.queue) else None

    def add(self, replace=False):
        urls = [l.strip() for l in self.text.get("1.0", "end").splitlines() if l.strip().startswith("http")]
        if not urls:
            self.status.set("Cola primeiro um ou mais links (um por linha).")
            return
        self.text.delete("1.0", "end")
        if replace:
            self.clear_queue()
        start = len(self.queue)
        for u in urls:
            if "/playlist?" in u:
                threading.Thread(target=self.expand_worker, args=(u,), daemon=True).start()
                self.status.set("A ler a playlist…")
            else:
                self.queue.append(u)
        if self.autodl.get():
            self.enqueue_dl(self.queue[start:])
        self.refresh_queue()
        self.fetch_meta(self.queue[start:])
        if len(self.queue) > start and not self.active:
            self.play(start)

    def on_lb_double(self, _e):
        sel = self.lb.curselection()
        if sel:
            self.play(sel[0])

    def remove_selected(self):
        rem = set(self.lb.curselection())
        if not rem:
            return
        was_current = self.active and self.idx in rem
        self.idx -= sum(1 for r in rem if r < self.idx)
        self.queue = [u for i, u in enumerate(self.queue) if i not in rem]
        if was_current:
            if self.idx < len(self.queue):
                self.play(self.idx)
            else:
                self.stop()
                self.idx = -1
        elif self.idx >= len(self.queue):
            self.idx = -1
        self.refresh_queue()

    def move(self, delta):
        sel = self.lb.curselection()
        if len(sel) != 1:
            return
        i, j = sel[0], sel[0] + delta
        if not 0 <= j < len(self.queue):
            return
        self.queue[i], self.queue[j] = self.queue[j], self.queue[i]
        if self.idx == i:
            self.idx = j
        elif self.idx == j:
            self.idx = i
        self.refresh_queue()
        self.lb.selection_clear(0, "end")
        self.lb.selection_set(j)

    def clear_queue(self):
        self.stop()
        self.queue = []
        self.idx = -1
        self.refresh_queue()

    # ---------- reprodução ----------
    def ensure_mpv(self):
        if self.mpv is not None and self.mpv.alive():
            return True
        m = Mpv(lambda e: self.ev_q.put((m, e)))
        try:
            m.start(self.audio.get(), self.vol.get())
        except (OSError, RuntimeError) as exc:
            messagebox.showerror("Rui Caetano Player", f"Não consegui iniciar o mpv:\n{exc}")
            return False
        self.mpv = m
        self.set_speed()
        return True

    def resume_pos(self, url):
        h = self.hist.get(url)
        if not h or h.get("done"):
            return 0
        p = h.get("pos", 0)
        d = self.meta.get(url, {}).get("dur")
        return p if p >= 5 and (not d or p < d - 10) else 0

    def play(self, i, fresh=False):
        if not 0 <= i < len(self.queue) or not self.ensure_mpv():
            return
        url = self.queue[i]
        start = 0 if fresh else self.resume_pos(url)
        self.idx = i
        self.active = True
        self.loaded = False
        self.pos = float(start)
        self.dur = self.meta.get(url, {}).get("dur") or 0
        self.hist[url] = {"when": time.time(), "pos": start, "done": False}
        if len(self.hist) > 500:
            for u in sorted(self.hist, key=lambda k: self.hist[k].get("when", 0))[:len(self.hist) - 500]:
                del self.hist[u]
        src, self.playing_local = url, False
        lf = local_file(url) if self.audio.get() else None
        if lf:
            src, self.playing_local = lf, True
        self.mpv.cmd("loadfile", src, "replace", -1, f"start={start:.1f}" if start else "")
        self.mpv.cmd("set_property", "pause", False)
        if self.autodl.get() and self.audio.get():
            self.enqueue_dl(reversed(self.queue[i:i + 3]), front=True)
        self.status.set("A carregar…" + (f" (a continuar em {fmt(start)})" if start else ""))
        self.update_bar()
        self.refresh_queue()
        self.refresh_hist()
        self.save()

    def pick_next(self, auto):
        n = len(self.queue)
        if n == 0:
            return None
        if auto and self.repeat == 2:
            return self.idx
        if self.shuffle.get() and n > 1:
            return random.choice([i for i in range(n) if i != self.idx])
        ni = self.idx + 1
        if ni >= n:
            return 0 if self.repeat == 1 else None
        return ni

    def next(self):
        if not self.active:
            return self.play(max(self.idx, 0))
        ni = self.pick_next(False)
        if ni is None:
            self.status.set("Fim da fila.")
        else:
            self.play(ni)

    def prev(self):
        if not self.active:
            return self.play(max(self.idx, 0))
        if self.pos > 3 or self.idx <= 0:
            self.seek_to(0)
        else:
            self.play(self.idx - 1)

    def stop(self):
        if self.mpv is not None:
            self.mpv.cmd("stop")
        self.active = False
        self.loaded = False
        self.pos = self.dur = 0
        self.update_bar()
        self.btn_pause.config(text="▶")
        self.refresh_queue()
        self.save()

    def toggle_pause(self):
        if not self.active:
            if "http" in self.text.get("1.0", "end"):
                return self.add()
            if self.queue:
                self.play(self.idx if self.idx >= 0 else 0)
            return
        self.mpv.cmd("cycle", "pause")

    def seek(self, delta):
        if self.active and self.loaded:
            self.mpv.cmd("seek", delta, "relative")

    def seek_to(self, t):
        if self.active and self.loaded:
            self.mpv.cmd("seek", float(t), "absolute")

    def set_volume(self):
        if self.mpv is not None:
            self.mpv.cmd("set_property", "volume", float(self.vol.get()))

    def set_speed(self):
        if self.mpv is not None:
            self.mpv.cmd("set_property", "speed", float(self.speed.get()))

    def cycle_repeat(self):
        self.repeat = (self.repeat + 1) % 3
        self.btn_repeat.config(text=REPEAT_TEXT[self.repeat])

    def on_audio_toggle(self):
        audio = self.audio.get()
        if self.mpv is not None and self.mpv.alive():
            self.mpv.cmd("set_property", "ytdl-format", FMT_AUDIO if audio else FMT_VIDEO)
            self.mpv.cmd("set_property", "vid", "no" if audio else "auto")
            if self.active and self.loaded:
                self.play(self.idx)

    def on_scale(self, v):
        if self.dragging:
            self.time_var.set(f"{fmt(to_float(v))} / {fmt(self.dur) if self.dur else '--:--'}")

    def on_scale_release(self, _e):
        self.seek_to(self.scale.get())
        self.pos = float(self.scale.get())
        self.dragging = False

    def update_bar(self):
        self.scale.config(to=max(self.dur, 1))
        if not self.dragging:
            self.scale.set(self.pos)
            self.time_var.set(f"{fmt(self.pos)} / {fmt(self.dur) if self.dur else '--:--'}")

    # ---------- eventos ----------
    def tick(self):
        try:
            while True:
                src, e = self.ev_q.get_nowait()
                if src is None:
                    self.on_app_event(e)
                elif src is self.mpv:
                    self.on_mpv(e)
        except queue.Empty:
            pass
        self.ticks += 1
        if self.ticks % 100 == 0:
            self.save()
        self.root.after(100, self.tick)

    def on_mpv(self, e):
        ev = e.get("event")
        url = self.cur_url()
        if ev == "property-change":
            name, v = e.get("name"), e.get("data")
            if name == "time-pos" and v is not None and self.loaded and url:
                self.pos = v
                h = self.hist.setdefault(url, {"when": time.time()})
                h["pos"], h["done"] = v, False
                self.update_bar()
            elif name == "duration" and self.loaded and url:
                self.dur = v or 0
                if v:
                    self.meta.setdefault(url, {})["dur"] = v
                self.update_bar()
            elif name == "pause":
                self.paused = bool(v)
                self.btn_pause.config(text="▶" if self.paused or not self.active else "⏸")
            elif (name == "media-title" and v and url and self.active and not self.playing_local
                  and not v.startswith("http")):
                if self.meta.get(url, {}).get("title") != v:
                    self.meta.setdefault(url, {})["title"] = v
                    self.refresh_queue()
        elif ev == "file-loaded":
            self.loaded = True
            self.errors = 0
            self.status.set("A tocar.")
            self.btn_pause.config(text="⏸")
        elif ev == "end-file":
            reason = e.get("reason")
            if reason == "eof" and url:
                self.hist[url] = {"when": time.time(), "pos": 0, "done": True}
                self.finish()
            elif reason == "error":
                self.fail()
        elif ev == "mpv-closed":
            if self.active:
                self.status.set("O leitor fechou. Carrega ▶ para continuar.")
            self.active = self.loaded = False
            self.btn_pause.config(text="▶")
            self.refresh_queue()

    def finish(self):
        ni = self.pick_next(True)
        if ni is None:
            self.active = self.loaded = False
            self.btn_pause.config(text="▶")
            self.status.set("Fim da fila.")
            self.refresh_queue()
            self.refresh_hist()
        else:
            self.play(ni, fresh=ni == self.idx)

    def fail(self):
        self.errors += 1
        self.status.set(f"Não consegui abrir: {self.title_of(self.cur_url())}")
        if self.errors < len(self.queue):
            ni = self.pick_next(False)
            if ni is not None:
                self.play(ni)
                return
        self.active = self.loaded = False
        self.refresh_queue()

    # ---------- downloads (para ouvir offline) ----------
    def scan_local(self):
        # ficheiros antigos guardados com o id no nome (versão anterior)
        try:
            for n in os.listdir(self.dl_dir):
                stem = n.rsplit(".", 1)[0]
                if ID_RE.fullmatch(stem) and ".part" not in n:
                    FILES.setdefault(stem, os.path.join(self.dl_dir, n))
        except OSError:
            pass
        self.local_ids = {v for v, p in FILES.items() if os.path.exists(p)}

    def enqueue_dl(self, urls, front=False):
        with self.dl_lock:
            if not self.dl_list and self.dl_cur is None:
                self.dl_total = self.dl_n = 0
            for u in urls:
                if vid_of(u) and u not in self.dl_list and u != self.dl_cur and not local_file(u):
                    self.dl_total += 1
                    if front:
                        self.dl_list.insert(0, u)
                    else:
                        self.dl_list.append(u)
        self.dl_event.set()

    def download_selected(self):
        sel = self.lb.curselection() or range(len(self.queue))
        urls = [self.queue[i] for i in sel]
        self.enqueue_dl(urls)
        self.status.set(f"{len(urls)} faixa(s) na fila de download.")
        self.refresh_queue()

    def folder_menu(self):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Abrir pasta de downloads", command=self.open_folder)
        menu.add_command(label="Alterar pasta de downloads…", command=self.change_folder)
        menu.add_separator()
        menu.add_command(label=self.dl_dir, state="disabled")
        menu.tk_popup(*self.root.winfo_pointerxy())

    def open_folder(self):
        os.makedirs(self.dl_dir, exist_ok=True)
        os.startfile(self.dl_dir)

    def change_folder(self):
        d = filedialog.askdirectory(parent=self.root, initialdir=self.dl_dir,
                                    title="Pasta para os downloads")
        if d:
            self.dl_dir = os.path.normpath(d)
            self.scan_local()
            self.status.set(f"Os próximos downloads vão para: {self.dl_dir}")
            self.save()
            self.refresh_queue()

    def show_dl(self, url, pct):
        self.dl_frame.grid()
        self.dl_bar["value"] = pct
        self.dl_text.set(f"Download {min(self.dl_n + 1, self.dl_total)}/{self.dl_total}: "
                         f"{self.title_of(url)} — {int(pct)}%")

    def dl_worker(self):
        while not self.closing:
            self.dl_event.wait()
            with self.dl_lock:
                if not self.dl_list:
                    self.dl_event.clear()
                    continue
                url = self.dl_list.pop(0)
                self.dl_cur, self.dl_pct = url, 0
            ok = False
            for _ in range(5):
                ok = self.dl_one(url)
                if ok or self.closing:
                    break
                time.sleep(15)  # a net costuma voltar; o yt-dlp continua do .part
            self.dl_cur = None
            self.dl_n += 1
            self.ev_q.put((None, {"event": "dl-done" if ok else "dl-fail", "url": url}))

    def dl_one(self, url):
        folder = self.dl_dir
        os.makedirs(folder, exist_ok=True)
        args = [YTDLP, "--no-warnings", "--no-playlist", "--no-simulate", "--progress", "--newline",
                "--windows-filenames", "-f", "ba[ext=m4a]/ba",
                "-o", os.path.join(folder, "%(title).120B.%(ext)s"), "--retries", "infinite",
                "--fragment-retries", "infinite", "--socket-timeout", "20",
                "--progress-template", "download:%(progress._percent_str)s",
                "--print", "after_move:filepath", url]
        t0 = time.time()
        try:
            self.dl_proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            text=True, encoding="utf-8", errors="replace",
                                            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                                            creationflags=NOWIN)
        except OSError:
            return False
        last, path = -1, None
        for line in self.dl_proc.stdout:
            line = line.strip()
            pct = to_float(line[:-1]) if line.endswith("%") else None
            if pct is not None:
                if int(pct) // 5 != last:
                    last = int(pct) // 5
                    self.ev_q.put((None, {"event": "dl-progress", "url": url, "pct": pct}))
            elif line:
                path = line
        if self.dl_proc.wait() != 0:
            return False
        if not (path and os.path.exists(path)):
            # nome com caracteres que o pipe estragou: usa o ficheiro mais recente da pasta
            recent = [os.path.join(folder, n) for n in os.listdir(folder)
                      if ".part" not in n and os.path.getmtime(os.path.join(folder, n)) >= t0 - 5]
            path = max(recent, key=os.path.getmtime) if recent else None
        if not path:
            return False
        FILES[vid_of(url)] = path
        return True

    # ---------- títulos e playlists (yt-dlp em segundo plano) ----------
    def fetch_meta(self, urls):
        todo = [u for u in urls if not self.meta.get(u, {}).get("title") and u not in self.pending]
        if todo:
            self.pending.update(todo)
            threading.Thread(target=self.meta_worker, args=(todo,), daemon=True).start()

    def meta_worker(self, urls):
        for u in urls:
            try:
                r = subprocess.run([YTDLP, "--no-warnings", "--no-playlist", "--print",
                                    f"%(title)s{SEP}%(duration)s", u], capture_output=True,
                                   text=True, encoding="utf-8", timeout=45, creationflags=NOWIN)
                title, _, dur = (r.stdout.strip().splitlines() or [""])[-1].partition(SEP)
                if title:
                    self.ev_q.put((None, {"event": "meta", "url": u, "title": title, "dur": to_float(dur)}))
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.pending.discard(u)

    def expand_worker(self, url):
        items = []
        try:
            r = subprocess.run([YTDLP, "--no-warnings", "--flat-playlist", "--print",
                                f"%(id)s{SEP}%(title)s{SEP}%(duration)s", url], capture_output=True,
                               text=True, encoding="utf-8", timeout=120, creationflags=NOWIN)
            for line in r.stdout.splitlines():
                vid, title, dur = (line.split(SEP) + ["", ""])[:3]
                if vid:
                    items.append((f"https://www.youtube.com/watch?v={vid}", title, to_float(dur)))
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.ev_q.put((None, {"event": "expanded", "items": items}))

    def on_app_event(self, e):
        if e["event"] == "dl-progress":
            self.dl_pct = e["pct"]
            self.show_dl(e["url"], e["pct"])
            self.refresh_queue()
        elif e["event"] in ("dl-done", "dl-fail"):
            self.scan_local()
            ok = e["event"] == "dl-done"
            self.status.set(("✔ Descarregado: " if ok else "Falhou o download (sem net?): ")
                            + self.title_of(e["url"]))
            if self.dl_cur is None and not self.dl_list:
                self.dl_frame.grid_remove()
            else:
                self.show_dl(self.dl_list[0] if self.dl_list else e["url"], 0)
            self.refresh_queue()
        elif e["event"] == "meta":
            m = self.meta.setdefault(e["url"], {})
            m["title"] = e["title"]
            if e.get("dur"):
                m["dur"] = e["dur"]
            self.refresh_queue()
            self.refresh_hist()
        elif e["event"] == "expanded":
            if not e["items"]:
                self.status.set("Não consegui ler a playlist.")
                return
            start = len(self.queue)
            for url, title, dur in e["items"]:
                self.queue.append(url)
                m = self.meta.setdefault(url, {})
                if title and title != "NA":
                    m["title"] = title
                if dur:
                    m["dur"] = dur
            self.status.set(f"Playlist: {len(e['items'])} faixas adicionadas.")
            self.refresh_queue()
            if not self.active:
                self.play(start)

    # ---------- histórico ----------
    def open_history(self):
        if self.hw is not None and self.hw.winfo_exists():
            self.hw.lift()
            return self.refresh_hist()
        w = self.hw = tk.Toplevel(self.root)
        w.title("Histórico")
        w.geometry("680x380")
        w.columnconfigure(0, weight=1)
        w.rowconfigure(0, weight=1)
        self.tv = ttk.Treeview(w, columns=("t", "p", "q"), show="headings", selectmode="extended")
        for c, text, width in (("t", "Título", 400), ("p", "Parou em", 90), ("q", "Quando", 110)):
            self.tv.heading(c, text=text)
            self.tv.column(c, width=width, stretch=c == "t")
        self.tv.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        sb = ttk.Scrollbar(w, command=self.tv.yview)
        sb.grid(row=0, column=1, sticky="ns", pady=8)
        self.tv.config(yscrollcommand=sb.set)
        self.tv.bind("<Double-Button-1>", lambda e: self.hist_play())
        bf = ttk.Frame(w)
        bf.grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))
        for label, fn in [("▶ Tocar (continua onde ficou)", self.hist_play),
                          ("＋ Adicionar à fila", self.hist_add),
                          ("Apagar selecionados", self.hist_delete),
                          ("Limpar tudo", self.hist_clear)]:
            ttk.Button(bf, text=label, command=fn).pack(side="left", padx=(0, 4))
        self.refresh_hist()

    def refresh_hist(self):
        if self.hw is None or not self.hw.winfo_exists():
            return
        sel = set(self.tv.selection())
        self.tv.delete(*self.tv.get_children())
        for u, h in sorted(self.hist.items(), key=lambda kv: kv[1].get("when", 0), reverse=True):
            if h.get("done"):
                where = "Terminou"
            else:
                where = fmt(h["pos"]) if h.get("pos", 0) >= 5 else "—"
            when = time.strftime("%d/%m %H:%M", time.localtime(h.get("when", 0)))
            self.tv.insert("", "end", iid=u, values=(self.title_of(u), where, when))
        keep = [u for u in sel if self.tv.exists(u)]
        if keep:
            self.tv.selection_set(keep)

    def hist_play(self):
        sel = self.tv.selection()
        if not sel:
            return
        url = sel[0]
        if url not in self.queue:
            self.queue.append(url)
        self.play(self.queue.index(url))

    def hist_add(self):
        new = [u for u in self.tv.selection() if u not in self.queue]
        self.queue.extend(new)
        self.refresh_queue()
        if new and not self.active:
            self.play(len(self.queue) - len(new))

    def hist_delete(self):
        for u in self.tv.selection():
            self.hist.pop(u, None)
        self.refresh_hist()
        self.refresh_queue()

    def hist_clear(self):
        if messagebox.askyesno("Histórico", "Apagar todo o histórico (e as posições memorizadas)?",
                               parent=self.hw):
            self.hist.clear()
            self.refresh_hist()
            self.refresh_queue()

    # ---------- fim ----------
    def close(self):
        self.closing = True
        self.dl_event.set()
        if self.dl_proc is not None and self.dl_proc.poll() is None:
            self.dl_proc.kill()
        self.save()
        if self.mpv is not None:
            self.mpv.close()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
