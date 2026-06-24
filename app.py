"""
DiskIndexer - Outil d'indexation et de recherche de fichiers pour Windows 10
============================================================================

Fonctionnalités:
- Identification automatique de tous les disques disponibles
- Indexation récursive des répertoires, sous-répertoires et fichiers
- Stockage dans une base SQLite
- Interface web (http://localhost:8765) pour rechercher dans l'index
- Liens cliquables qui ouvrent le fichier ou le dossier directement dans Windows
- Recherche multi-critères : nom (texte ou regex), extension, type, taille, emplacement
- Sélection des emplacements de recherche (par disque ou chemin)

Lancement: python app.py
Build .exe:  voir build.bat (PyInstaller)
"""
import os
import sys
import re
import sqlite3
import string
import ctypes
import threading
import webbrowser
import subprocess
import json
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

# Import pour la surveillance en temps réel
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    print("⚠ watchdog non installé. La surveillance en temps réel ne sera pas disponible.")
    print("  Installez-le avec : pip install watchdog")

# --- Configuration -----------------------------------------------------------
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "DiskIndexer")
os.makedirs(APP_DIR, exist_ok=True)
DB_PATH = os.path.join(APP_DIR, "index.db")
PORT = 8765

# Variable globale pour stocker l'observateur watchdog actuel
WATCHDOG_OBSERVER = None
WATCHDOG_DRIVES = set()

# Répertoires à ignorer (système / inaccessibles)
SKIP_DIRS = {
    "$Recycle.Bin", "System Volume Information", "Windows", "WinSxS",
    "Recovery", "Config.Msi", "ProgramData",
    "node_modules", ".git", "__pycache__",
}

# --- Surveillance en temps réel avec watchdog ------------------------------
class DiskIndexerHandler(FileSystemEventHandler):
    """Gestionnaire d'événements pour la surveillance en temps réel des fichiers."""
    
    def __init__(self, conn, skip_dirs=None):
        self.conn = conn
        self.skip_dirs = skip_dirs or set()
        self.lock = threading.Lock()

    def _should_ignore(self, path):
        """Vérifie si un chemin doit être ignoré."""
        for skip_dir in self.skip_dirs:
            if skip_dir in path:
                return True
        return False

    def on_created(self, event):
        """Appelé lorsqu'un fichier ou dossier est créé."""
        if self._should_ignore(event.src_path):
            return
        if not event.is_directory:
            self._index_file(event.src_path)

    def on_modified(self, event):
        """Appelé lorsqu'un fichier ou dossier est modifié."""
        if self._should_ignore(event.src_path):
            return
        if not event.is_directory:
            self._index_file(event.src_path)

    def on_deleted(self, event):
        """Appelé lorsqu'un fichier ou dossier est supprimé."""
        if self._should_ignore(event.src_path):
            return
        self._remove_file(event.src_path)

    def on_moved(self, event):
        """Appelé lorsqu'un fichier ou dossier est déplacé/renommé."""
        if self._should_ignore(event.src_path) or self._should_ignore(event.dest_path):
            return
        self._remove_file(event.src_path)
        if not event.is_directory:
            self._index_file(event.dest_path)

    def _index_file(self, path):
        """Indexe un fichier individuel dans la base de données."""
        try:
            if os.name == "nt":
                drive = os.path.splitdrive(path)[0] + "\\"
            else:
                drive = "/"
            parent = os.path.dirname(path)
            name = os.path.basename(path)
            ext = os.path.splitext(name)[1].lstrip(".").lower()
            
            try:
                st = os.stat(path)
                sz = st.st_size
                mt = int(st.st_mtime)
            except OSError:
                sz = 0
                mt = 0
            
            name_lower = name.lower()

            with self.lock:
                c = self.conn.cursor()
                c.execute("""
                    INSERT OR REPLACE INTO entries
                    (drive, path, name, name_lower, ext, is_dir, size, mtime, parent)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                """, (drive, path, name, name_lower, ext, sz, mt, parent))
                self.conn.commit()
        except Exception as e:
            print(f"[Watchdog] Erreur lors de l'indexation de {path}: {e}")

    def _remove_file(self, path):
        """Supprime un fichier de l'index."""
        try:
            with self.lock:
                c = self.conn.cursor()
                c.execute("DELETE FROM entries WHERE path = ?", (path,))
                self.conn.commit()
        except Exception as e:
            print(f"[Watchdog] Erreur lors de la suppression de {path}: {e}")


# --- Fonction pour lancer l'observateur watchdog -----------------------------
def start_watchdog_observer(drives, skip_dirs=None):
    """
    Lance un observateur watchdog pour surveiller les répertoires en temps réel.
    
    Args:
        drives: Liste des répertoires à surveiller (ex: ['C:\\', 'D:\\'])
        skip_dirs: Répertoires à ignorer (par défaut, SKIP_DIRS)
    
    Returns:
        Observer: L'instance de l'observateur (ou None si watchdog n'est pas disponible)
    """
    global WATCHDOG_OBSERVER, WATCHDOG_DRIVES
    
    if not WATCHDOG_AVAILABLE:
        return None
    
    # Arrêter l'observateur existant s'il y en a un
    if WATCHDOG_OBSERVER:
        WATCHDOG_OBSERVER.stop()
        WATCHDOG_OBSERVER.join()
    
    conn = db_connect()
    observer = Observer()
    skip_dirs = skip_dirs or SKIP_DIRS
    
    # Répertoires à exclure de la surveillance (système, virtuels, etc.)
    forbidden_dirs = {"/proc", "/sys", "/dev", "/run", "/boot"}
    
    for drive in drives:
        if os.path.exists(drive):
            # Sous Linux, éviter de surveiller la racine / ou les répertoires système
            if os.name != "nt" and drive == "/":
                print(f"[Watchdog] Ignore la racine / sous Linux (trop large)")
                continue
            
            # Vérifier si le répertoire est dans les répertoires interdits
            if any(forbidden in drive for forbidden in forbidden_dirs):
                print(f"[Watchdog] Ignore {drive} (répertoire système)")
                continue
            
            try:
                handler = DiskIndexerHandler(conn, skip_dirs=skip_dirs)
                observer.schedule(handler, drive, recursive=True)
                print(f"[Watchdog] Surveillance activée pour : {drive}")
                WATCHDOG_DRIVES.add(drive)
            except Exception as e:
                print(f"[Watchdog] Impossible de surveiller {drive}: {e}")
    
    if len(WATCHDOG_DRIVES) == 0:
        print("[Watchdog] Aucun répertoire valide à surveiller")
        return None
    
    observer.start()
    WATCHDOG_OBSERVER = observer
    return observer


def start_watchdog_for_path(path, skip_dirs=None):
    """
    Lance la surveillance watchdog pour un répertoire spécifique.
    
    Args:
        path: Chemin du répertoire à surveiller
        skip_dirs: Répertoires à ignorer (par défaut, SKIP_DIRS)
    
    Returns:
        bool: True si la surveillance a démarré, False sinon
    """
    global WATCHDOG_OBSERVER, WATCHDOG_DRIVES
    
    if not WATCHDOG_AVAILABLE:
        return False
    
    # Normaliser le chemin
    path = os.path.abspath(path)
    
    # Vérifier si le chemin existe et est un répertoire
    if not os.path.isdir(path):
        print(f"[Watchdog] {path} n'est pas un répertoire valide")
        return False
    
    # Répertoires à exclure de la surveillance (système, virtuels, etc.)
    forbidden_dirs = {"/proc", "/sys", "/dev", "/run", "/boot"}
    
    # Sous Linux, éviter de surveiller la racine / ou les répertoires système
    if os.name != "nt" and path == "/":
        print(f"[Watchdog] Ignore la racine / sous Linux (trop large)")
        return False
    
    if any(forbidden in path for forbidden in forbidden_dirs):
        print(f"[Watchdog] Ignore {path} (répertoire système)")
        return False
    
    # Arrêter l'observateur existant s'il y en a un
    stop_watchdog_observer()
    
    try:
        conn = db_connect()
        observer = Observer()
        skip_dirs = skip_dirs or SKIP_DIRS
        
        handler = DiskIndexerHandler(conn, skip_dirs=skip_dirs)
        observer.schedule(handler, path, recursive=True)
        
        observer.start()
        WATCHDOG_OBSERVER = observer
        WATCHDOG_DRIVES = {path}
        
        print(f"[Watchdog] Surveillance activée pour : {path}")
        return True
        
    except Exception as e:
        print(f"[Watchdog] Impossible de surveiller {path}: {e}")
        return False


def stop_watchdog_observer():
    """Arrête l'observateur watchdog actuel."""
    global WATCHDOG_OBSERVER, WATCHDOG_DRIVES
    
    if WATCHDOG_OBSERVER:
        WATCHDOG_OBSERVER.stop()
        WATCHDOG_OBSERVER.join()
        WATCHDOG_OBSERVER = None
        WATCHDOG_DRIVES.clear()
        print("[Watchdog] Surveillance arrêtée")


# --- Détection des disques (Windows) -----------------------------------------
def list_drives():
    """Retourne la liste des lettres de lecteurs disponibles, ex: ['C:\\\\', 'D:\\\\']."""
    drives = []
    if os.name == "nt":
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if bitmask & (1 << i):
                drives.append(f"{letter}:\\")
    else:
        # Mode dev hors Windows : on expose la racine
        drives.append("/")
    return drives

# --- Base de données ---------------------------------------------------------
def _regexp(pattern, value):
    """Fonction REGEXP pour SQLite."""
    if value is None:
        return False
    try:
        return re.search(pattern, value, re.IGNORECASE) is not None
    except re.error:
        return False

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.create_function("REGEXP", 2, _regexp)
    return conn

def db_init():
    conn = db_connect()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            drive TEXT NOT NULL,
            path TEXT NOT NULL,
            name TEXT NOT NULL,
            name_lower TEXT NOT NULL,
            ext TEXT NOT NULL DEFAULT '',
            is_dir INTEGER NOT NULL,
            size INTEGER DEFAULT 0,
            mtime INTEGER DEFAULT 0,
            parent TEXT
        )
    """)
    # Migration : colonne ext
    try:
        c.execute("ALTER TABLE entries ADD COLUMN ext TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    # Migration : colonne mtime
    try:
        c.execute("ALTER TABLE entries ADD COLUMN mtime INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Correction ext mal remplie (noms multi-points, ancienne indexation)
    bad_rows = c.execute(
        "SELECT id, name FROM entries WHERE is_dir=0 AND (ext='' OR ext LIKE '%.%')"
    ).fetchall()
    if bad_rows:
        corrections = []
        for row_id, name in bad_rows:
            ext = os.path.splitext(name)[1].lstrip(".").lower()
            corrections.append((ext, row_id))
        c.executemany("UPDATE entries SET ext=? WHERE id=?", corrections)
        conn.commit()

    c.execute("CREATE INDEX IF NOT EXISTS idx_name_lower ON entries(name_lower)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_drive ON entries(drive)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_is_dir ON entries(is_dir)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ext ON entries(ext)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mtime ON entries(mtime)")
    conn.commit()
    conn.close()

# --- Indexation --------------------------------------------------------------
INDEX_STATE = {"running": False, "current": "", "count": 0, "drive": ""}

def index_drives(drives, progress_cb=None):
    INDEX_STATE.update(running=True, current="", count=0, drive="")
    conn = db_connect()
    c = conn.cursor()
    # On purge les disques concernés pour réindexer proprement
    for d in drives:
        c.execute("DELETE FROM entries WHERE drive = ?", (d,))
    conn.commit()

    batch = []
    BATCH_SIZE = 2000
    total = 0
    dir_sizes = {}  # Stocke la taille cumulée pour chaque répertoire

    def flush():
        nonlocal batch
        if batch:
            c.executemany(
                "INSERT INTO entries(drive, path, name, name_lower, ext, is_dir, size, mtime, parent) VALUES (?,?,?,?,?,?,?,?,?)",
                batch,
            )
            conn.commit()
            batch = []

    # Première passe : indexer tous les fichiers et dossiers
    for drive in drives:
        INDEX_STATE["drive"] = drive
        for root, dirs, files in os.walk(drive, topdown=True, onerror=lambda e: None):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith("$")]
            INDEX_STATE["current"] = root
            # Dossiers
            for d in dirs:
                full = os.path.join(root, d)
                try:
                    mt = int(os.path.getmtime(full))
                except OSError:
                    mt = 0
                # Initialiser la taille du dossier à 0 (sera calculée après)
                batch.append((drive, full, d, d.lower(), "", 1, 0, mt, root))
                dir_sizes[full] = 0
                total += 1
            # Fichiers
            for f in files:
                full = os.path.join(root, f)
                try:
                    st = os.stat(full)
                    sz = st.st_size
                    mt = int(st.st_mtime)
                except OSError:
                    sz = 0
                    mt = 0
                ext = os.path.splitext(f)[1].lstrip(".").lower()
                batch.append((drive, full, f, f.lower(), ext, 0, sz, mt, root))
                # Ajouter la taille du fichier au dossier parent
                parent_dir = root
                while parent_dir and parent_dir in dir_sizes:
                    dir_sizes[parent_dir] += sz
                    parent_dir = os.path.dirname(parent_dir)
                total += 1
            INDEX_STATE["count"] = total
            if len(batch) >= BATCH_SIZE:
                flush()
    flush()

    # Deuxième passe : mettre à jour les tailles des dossiers
    for path, size in dir_sizes.items():
        c.execute("UPDATE entries SET size = ? WHERE path = ? AND is_dir = 1", (size, path))
    conn.commit()

    conn.close()
    INDEX_STATE.update(running=False, current="Terminé", count=total)

def start_index_async(drives):
    if INDEX_STATE["running"]:
        return False
    t = threading.Thread(target=index_drives, args=(drives,), daemon=True)
    t.start()
    return True

# --- Ouverture native --------------------------------------------------------
def _foreground_after_process(hProcess):
    """
    Dans un thread : attend que le processus crée une fenêtre visible,
    puis la met au premier plan en contournant la restriction Windows
    sur SetForegroundWindow via AttachThreadInput.
    """
    if os.name != "nt":
        return

    import time
    kernel32 = ctypes.windll.kernel32
    user32   = ctypes.windll.user32

    pid = kernel32.GetProcessId(hProcess)
    if not pid:
        kernel32.CloseHandle(hProcess)
        return

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

    def _find_window():
        """Retourne le hwnd de la première fenêtre visible appartenant au PID."""
        found = []
        def _cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            buf_pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(buf_pid))
            if buf_pid.value == pid:
                found.append(hwnd)
            return True
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        return found[0] if found else None

    def _force_foreground(hwnd):
        """
        Force la fenêtre au premier plan en attachant notre thread
        au thread propriétaire de la fenêtre cible, ce qui lève la
        restriction Windows sur SetForegroundWindow.
        """
        SW_RESTORE = 9
        # TID du thread qui possède la fenêtre cible
        target_tid = user32.GetWindowThreadProcessId(hwnd, None)
        # TID du thread courant
        current_tid = kernel32.GetCurrentThreadId()
        # TID du thread qui possède actuellement le focus
        fg_hwnd = user32.GetForegroundWindow()
        fg_tid  = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0

        attached_fg  = False
        attached_tgt = False

        try:
            # Attacher notre thread au thread qui a le focus
            if fg_tid and fg_tid != current_tid:
                attached_fg = bool(user32.AttachThreadInput(current_tid, fg_tid, True))
            # Attacher notre thread au thread cible
            if target_tid and target_tid != current_tid:
                attached_tgt = bool(user32.AttachThreadInput(current_tid, target_tid, True))

            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            # Toujours détacher pour éviter de bloquer les threads
            if attached_fg:
                user32.AttachThreadInput(current_tid, fg_tid, False)
            if attached_tgt:
                user32.AttachThreadInput(current_tid, target_tid, False)

    # Sonder toutes les 100 ms pendant 5 s
    deadline = time.time() + 5.0
    while time.time() < deadline:
        time.sleep(0.1)
        hwnd = _find_window()
        if hwnd:
            _force_foreground(hwnd)
            kernel32.CloseHandle(hProcess)
            return

    kernel32.CloseHandle(hProcess)


def _shell_execute_foreground(verb, path, params=None):
    """
    Lance une action via ShellExecuteExW puis met la fenêtre créée
    au premier plan dès qu'elle apparaît.
    """
    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_SHOWNORMAL = 1

    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize",        ctypes.c_ulong),
            ("fMask",         ctypes.c_ulong),
            ("hwnd",          ctypes.c_void_p),
            ("lpVerb",        ctypes.c_wchar_p),
            ("lpFile",        ctypes.c_wchar_p),
            ("lpParameters",  ctypes.c_wchar_p),
            ("lpDirectory",   ctypes.c_wchar_p),
            ("nShow",         ctypes.c_int),
            ("hInstApp",      ctypes.c_void_p),
            ("lpIDList",      ctypes.c_void_p),
            ("lpClass",       ctypes.c_wchar_p),
            ("hkeyClass",     ctypes.c_void_p),
            ("dwHotKey",      ctypes.c_ulong),
            ("hIconOrMonitor",ctypes.c_void_p),
            ("hProcess",      ctypes.c_void_p),
        ]

    sei = SHELLEXECUTEINFO()
    sei.cbSize       = ctypes.sizeof(sei)
    sei.fMask        = SEE_MASK_NOCLOSEPROCESS
    sei.hwnd         = None
    sei.lpVerb       = verb
    sei.lpFile       = path
    sei.lpParameters = params
    sei.lpDirectory  = None
    sei.nShow        = SW_SHOWNORMAL
    sei.hInstApp     = None
    sei.hProcess     = None

    ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))

    if sei.hProcess:
        t = threading.Thread(
            target=_foreground_after_process,
            args=(sei.hProcess,),
            daemon=True,
        )
        t.start()


def open_in_explorer(path):
    if not os.path.exists(path):
        return False
    try:
        if os.name == "nt":
            verb = "explore" if os.path.isdir(path) else "open"
            _shell_execute_foreground(verb, path)
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception as e:
        print("open error:", e)
        return False


def reveal_in_explorer(path):
    """Ouvre l'explorateur, sélectionne l'élément et met la fenêtre au premier plan."""
    if os.name == "nt" and os.path.exists(path):
        try:
            # explorer /select, n'est pas compatible avec ShellExecuteEx,
            # on le lance via Popen et on remonte la fenêtre par PID.
            proc = subprocess.Popen(["explorer", "/select,", path])
            # proc.pid est le PID du processus explorer.exe
            # On ouvre un handle pour pouvoir l'utiliser dans _foreground_after_process
            SYNCHRONIZE         = 0x00100000
            PROCESS_QUERY_INFO  = 0x00001000
            handle = ctypes.windll.kernel32.OpenProcess(
                SYNCHRONIZE | PROCESS_QUERY_INFO, False, proc.pid
            )
            if handle:
                t = threading.Thread(
                    target=_foreground_after_process,
                    args=(handle,),
                    daemon=True,
                )
                t.start()
            return True
        except Exception as e:
            print("reveal error:", e)
    return open_in_explorer(os.path.dirname(path))

# --- Formatage taille --------------------------------------------------------
def human_size(n):
    if n is None:
        return ""
    for u in ["o", "Ko", "Mo", "Go", "To"]:
        if n < 1024:
            return f"{n:.1f} {u}" if u != "o" else f"{int(n)} {u}"
        n /= 1024
    return f"{n:.1f} Po"

def parse_size(s):
    """Convertit une chaîne '10Mo', '500Ko', '1Go' en octets. Retourne None si vide."""
    if not s:
        return None
    s = s.strip().upper().replace(" ", "")
    multipliers = {"O": 1, "KO": 1024, "MO": 1024**2, "GO": 1024**3, "TO": 1024**4,
                   "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4,
                   "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    for suffix in sorted(multipliers, key=len, reverse=True):
        if s.endswith(suffix):
            try:
                return int(float(s[:-len(suffix)]) * multipliers[suffix])
            except ValueError:
                return None
    try:
        return int(s)
    except ValueError:
        return None

def parse_date(s):
    """Convertit 'JJ/MM/AAAA' en timestamp Unix. Retourne None si vide ou invalide."""
    if not s:
        return None
    import datetime
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return int(datetime.datetime.strptime(s.strip(), fmt).timestamp())
        except ValueError:
            pass
    return None

# --- Recherche ---------------------------------------------------------------
def search(q, use_regex, drives, include_subdirs, dirs_only, files_only,
           ext_filter, size_min, size_max, path_filter,
           date_min=None, date_max=None, sort_by="name"):
    # Au moins un critère doit être fourni
    if not q and not ext_filter and size_min is None and size_max is None \
            and not path_filter and date_min is None and date_max is None:
        return {"total": 0, "results": [], "truncated": False, "error": None}

    conn = db_connect()
    params = []
    conditions = []

    # --- Filtre sur le nom (optionnel si autre critère présent) ---
    if q:
        if use_regex:
            try:
                re.compile(q)
            except re.error as e:
                conn.close()
                return {"total": 0, "results": [], "truncated": False, "error": f"Regex invalide : {e}"}
            conditions.append("name REGEXP ?")
            params.append(q)
        else:
            q_lower = q.lower()
            has_wildcard = ("*" in q_lower or "?" in q_lower)
            q_like = q_lower.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
            q_like = q_like.replace("*", "%").replace("?", "_")
            if has_wildcard:
                if not q_lower.startswith("*"):
                    q_like = "%" + q_like
                if not q_lower.endswith("*"):
                    q_like = q_like + "%"
            else:
                q_like = f"%{q_like}%"
            conditions.append("name_lower LIKE ? ESCAPE '\\'")
            params.append(q_like)

    # --- Filtre disques ---
    if drives:
        conditions.append("drive IN (" + ",".join("?" * len(drives)) + ")")
        params += drives

    # --- Filtre type ---
    if dirs_only and not files_only:
        conditions.append("is_dir = 1")
    elif files_only and not dirs_only:
        conditions.append("is_dir = 0")

    # --- Filtre extension ---
    if ext_filter:
        exts = [e.strip().lstrip(".").lower() for e in ext_filter.split(",") if e.strip()]
        if exts:
            # Filtre principal sur la colonne ext (indexée, rapide)
            # + fallback sur name_lower LIKE pour couvrir les cas où ext serait
            # mal rempli en base (noms multi-points, ancienne indexation)
            ext_conditions = []
            for e in exts:
                ext_conditions.append("ext = ?")
                params.append(e)
                ext_conditions.append("name_lower LIKE ?")
                params.append(f"%.{e}")
            conditions.append("is_dir = 0")   # les extensions ne concernent que les fichiers
            conditions.append("(" + " OR ".join(ext_conditions) + ")")

    # --- Filtre taille ---
    if size_min is not None:
        conditions.append("size >= ?")
        params.append(size_min)
    if size_max is not None:
        conditions.append("size <= ?")
        params.append(size_max)

    # --- Filtre date de modification ---
    if date_min is not None:
        conditions.append("mtime >= ?")
        params.append(date_min)
    if date_max is not None:
        # Fin de journée : +86399 secondes pour inclure toute la journée
        conditions.append("mtime <= ?")
        params.append(date_max + 86399)

    # --- Filtre emplacement (chemin parent) ---
    if path_filter:
        conditions.append("path LIKE ?")
        params.append(f"{path_filter.rstrip(chr(92))}%")

    # --- Sous-répertoires ---
    if not include_subdirs:
        effective_drives = drives if drives else list_drives()
        conditions.append("parent IN (" + ",".join("?" * len(effective_drives)) + ")")
        params += [d.rstrip("\\") + "\\" for d in effective_drives]

    # --- Tri ---
    order = "mtime DESC" if sort_by == "date_desc" else \
            "mtime ASC"  if sort_by == "date_asc"  else \
            "is_dir DESC, name"

    sql = "SELECT path, name, is_dir, size, parent, drive, mtime FROM entries"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += f" ORDER BY {order} LIMIT 1001"

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    truncated = len(rows) > 1000
    rows = rows[:1000]
    results = [{
        "path": r[0], "name": r[1], "is_dir": bool(r[2]),
        "size": r[3], "size_h": human_size(r[3]),
        "parent": r[4], "drive": r[5],
        "mtime": r[6] or 0,
    } for r in rows]
    return {"total": len(results), "results": results, "truncated": truncated, "error": None}

# --- HTML de l'interface -----------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8" />
<title>DiskIndexer - Recherche de fichiers</title>
<style>
  :root { --bg:#0f172a; --panel:#1e293b; --text:#e2e8f0; --muted:#94a3b8;
          --accent:#3b82f6; --accent-hover:#2563eb; --border:#334155;
          --warn:#f59e0b; --error:#ef4444; --success:#22c55e;
          --row-hover:#0b1220; --input-bg:#0b1220; }

  /* ---- Thèmes ---- */

  /* Ardoise — gris bleuté neutre, texte très clair, contraste renforcé */
  body.theme-slate   { --bg:#23272f; --panel:#2c3140; --text:#e8eaf0; --muted:#9aa3b8;
                       --accent:#60a5fa; --accent-hover:#3b82f6; --border:#3d4459;
                       --row-hover:#1e2230; --input-bg:#1a1e2a; }

  /* Forêt — vert sauge doux, fond moins saturé, texte crème lisible */
  body.theme-forest  { --bg:#1c2620; --panel:#253029; --text:#ddeedd; --muted:#8ab89a;
                       --accent:#4ade80; --accent-hover:#22c55e; --border:#354a3a;
                       --row-hover:#161e19; --input-bg:#131a15; }

  /* Bordeaux → Prune — fond prune foncé désaturé, texte pêche doux */
  body.theme-bordeaux{ --bg:#251c24; --panel:#30242e; --text:#f0dded; --muted:#b89aad;
                       --accent:#e879a0; --accent-hover:#db2777; --border:#4a3345;
                       --row-hover:#1e161d; --input-bg:#1a1119; }

  /* Lumière — inchangé */
  body.theme-light   { --bg:#f1f5f9; --panel:#ffffff; --text:#1e293b; --muted:#64748b;
                       --accent:#2563eb; --accent-hover:#1d4ed8; --border:#e2e8f0;
                       --row-hover:#f8fafc; --input-bg:#f8fafc; }

  /* Sépia — fond papier plus chaud, texte brun foncé, accent ocre doux */
  body.theme-sepia   { --bg:#f0e9d8; --panel:#faf4e6; --text:#2c2010; --muted:#7a6548;
                       --accent:#a16207; --accent-hover:#854d0e; --border:#d6c9b0;
                       --row-hover:#ede6d5; --input-bg:#ede6d4; }

  /* Ajustements communs thèmes clairs (Lumière + Sépia) */
  body.theme-light .tag.dir, body.theme-sepia .tag.dir
                     { background:#dbeafe; color:#1e40af; }
  body.theme-light .tag.file, body.theme-sepia .tag.file
                     { background:#e2e8f0; color:#334155; }
  body.theme-light tr:hover td, body.theme-sepia tr:hover td
                     { background:var(--row-hover); }
  body.theme-light input[type=text], body.theme-sepia input[type=text]
                     { background:var(--input-bg); color:var(--text); }
  body.theme-light .drive-chip.on { background:#2563eb; color:#ffffff; border-color:#2563eb; }
  body.theme-sepia  .drive-chip.on { background:#a16207; color:#ffffff; border-color:#a16207; }
  body.theme-light button.secondary { background:#cbd5e1; color:#1e293b; }
  body.theme-light button.secondary:hover { background:#94a3b8; color:#1e293b; }
  body.theme-sepia  button.secondary { background:#c9b99a; color:#2c2010; }
  body.theme-sepia  button.secondary:hover { background:#b0a080; color:#2c2010; }
  body.theme-light a.link { color:#1d4ed8; }
  body.theme-sepia  a.link { color:#92400e; }
  body.theme-light .error-msg { background:#fef2f2; color:#991b1b; }
  body.theme-sepia  .error-msg { background:#fde8d0; color:#7c2d12; }

  /* Ajustements thèmes sombres colorés */
  body.theme-forest .tag.dir   { background:#14532d; color:#bbf7d0; }
  body.theme-forest .tag.file  { background:#374151; color:#d1fae5; }
  body.theme-bordeaux .tag.dir { background:#4a1942; color:#fbcfe8; }
  body.theme-bordeaux .tag.file{ background:#3f2040; color:#fce7f3; }
  body.theme-slate .tag.dir    { background:#1e3a5f; color:#bfdbfe; }

  /* Liens colorés selon thème */
  body.theme-forest   a.link { color:#86efac; }
  body.theme-bordeaux a.link { color:#f9a8d4; }
  body.theme-slate    a.link { color:#93c5fd; }
  body.theme-sepia    a.link { color:#a16207; }

  /* Sélecteur de thème */
  .theme-bar { display:flex; gap:8px; align-items:center; }
  .theme-dot { width:22px; height:22px; border-radius:50%; cursor:pointer;
               border:3px solid transparent; transition:border-color .15s; flex-shrink:0; }
  .theme-dot:hover, .theme-dot.active { border-color:var(--text); }
  .theme-dot[data-theme="default"]   { background:linear-gradient(135deg,#0f172a 50%,#3b82f6 50%); }
  .theme-dot[data-theme="slate"]     { background:linear-gradient(135deg,#23272f 50%,#60a5fa 50%); }
  .theme-dot[data-theme="forest"]    { background:linear-gradient(135deg,#1c2620 50%,#4ade80 50%); }
  .theme-dot[data-theme="bordeaux"]  { background:linear-gradient(135deg,#251c24 50%,#e879a0 50%); }
  .theme-dot[data-theme="light"]     { background:linear-gradient(135deg,#f1f5f9 50%,#2563eb 50%); }
  .theme-dot[data-theme="sepia"]     { background:linear-gradient(135deg,#f0e9d8 50%,#a16207 50%); }

  .btn-quit{padding:7px 14px;background:#7f1d1d;color:#fecaca;border:0;border-radius:8px;
            cursor:pointer;font-weight:600;font-size:13px;}
  .btn-quit:hover{background:#991b1b;}
  *{box-sizing:border-box} body{margin:0;font-family:Segoe UI,system-ui,sans-serif;background:var(--bg);color:var(--text)}
  header{padding:20px 28px;background:var(--panel);border-bottom:1px solid var(--border)}
  h1{margin:0;font-size:20px}
  main{max-width:1280px;margin:0 auto;padding:24px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:20px}
  .row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
  label{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:6px;cursor:pointer}
  input[type=text]{padding:10px 14px;background:var(--input-bg);color:var(--text);
                   border:1px solid var(--border);border-radius:8px;font-size:14px;width:100%}
  input[type=text]:focus{outline:none;border-color:var(--accent)}
  button{padding:9px 18px;background:var(--accent);color:#fff;border:0;border-radius:8px;cursor:pointer;font-weight:600;font-size:14px;white-space:nowrap}
  button:hover{background:var(--accent-hover)} button:disabled{opacity:.5;cursor:not-allowed}
  button.secondary{background:#334155} button.secondary:hover{background:#475569}
  button.warn{background:#b45309} button.warn:hover{background:#92400e}
  .drives{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
  .drive-chip{padding:5px 14px;background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;cursor:pointer;font-size:13px;user-select:none}
  .drive-chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
  table{width:100%;border-collapse:collapse;margin-top:12px;font-size:14px}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border)}
  th{color:var(--muted);font-weight:500;font-size:12px;text-transform:uppercase}
  tr:hover td{background:var(--row-hover)}
  a.link{color:#60a5fa;text-decoration:none;cursor:pointer}
  a.link:hover{text-decoration:underline}
  .tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;margin-right:6px;font-weight:600}
  .tag.dir{background:#1e40af;color:#dbeafe} .tag.file{background:#475569;color:#e2e8f0}
  .status{color:var(--muted);font-size:13px;margin-top:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .meta{color:var(--muted);font-size:12px;margin-top:8px}
  .opts{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .grid3{display:grid;grid-template-columns:2fr 1fr 1fr;gap:12px}
  @media(max-width:700px){.grid2,.grid3{grid-template-columns:1fr}}
  .field-label{font-size:12px;color:var(--muted);margin-bottom:4px}
  .field{display:flex;flex-direction:column}
  .badge-regex{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;
               background:#7c3aed;color:#ede9fe;margin-left:8px;vertical-align:middle}
  .error-msg{color:var(--error);font-size:13px;margin-top:8px;padding:8px 12px;
             background:#450a0a;border-radius:6px;border:1px solid var(--error)}
  .toggle-advanced{background:none;border:none;color:var(--accent);font-size:13px;cursor:pointer;padding:0;margin-top:8px}
  .toggle-advanced:hover{text-decoration:underline}
  #advanced-panel{display:none;margin-top:14px;padding-top:14px;border-top:1px solid var(--border)}
  #advanced-panel.open{display:block}
  .hint{font-size:11px;color:var(--muted);margin-top:3px}
  /* --- Groupes de types de fichiers --- */
  .ftype-group { margin-bottom:10px; }
  .ftype-group-label { font-size:11px; color:var(--muted); text-transform:uppercase;
                       letter-spacing:.5px; margin-bottom:5px; }
  .ftype-chips { display:flex; gap:6px; flex-wrap:wrap; }
  .ftype-chip  { padding:4px 11px; border:1px solid var(--border); border-radius:20px;
                 font-size:12px; cursor:pointer; user-select:none;
                 background:var(--input-bg); color:var(--text); transition:all .12s; }
  .ftype-chip:hover { border-color:var(--accent); color:var(--accent); }
  .ftype-chip.on    { background:var(--accent); border-color:var(--accent); color:#fff; }
  .ftype-chip.all-chip { font-weight:600; }
  /* Disques recherche visibles dans la zone principale */
  .search-drives-bar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:14px; }
  .sdrive-chip { padding:5px 16px; border:1px solid var(--border); border-radius:6px;
                 font-size:13px; cursor:pointer; user-select:none;
                 background:var(--input-bg); color:var(--text); font-weight:600; }
  .sdrive-chip.on  { background:var(--accent); border-color:var(--accent); color:#fff; }
  .sdrive-chip:hover { border-color:var(--accent); }
</style>
</head>
<body>
<header style="display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap">
  <h1 style="margin:0">🔍 DiskIndexer — Indexation &amp; recherche locale</h1>
  <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
    <div class="theme-bar" title="Choisir un thème">
      <span style="font-size:12px;color:var(--muted)">Thème :</span>
      <span class="theme-dot active" data-theme="default"  title="Nuit bleue"></span>
      <span class="theme-dot"        data-theme="slate"    title="Ardoise"></span>
      <span class="theme-dot"        data-theme="forest"   title="Forêt"></span>
      <span class="theme-dot"        data-theme="bordeaux" title="Bordeaux"></span>
      <span class="theme-dot"        data-theme="light"    title="Lumière"></span>
      <span class="theme-dot"        data-theme="sepia"    title="Sépia"></span>
    </div>
    <button class="btn-quit" id="btn-quit" title="Arrêter le serveur et fermer">⏻ Quitter</button>
  </div>
</header>
<main>

  <section class="card">
    <h3 style="margin-top:0">Disques</h3>
    <div id="drives" class="drives"></div>
    <div class="opts" style="margin-top:12px">
      <label><input type="checkbox" id="opt-subdirs-idx" checked> Inclure les sous-répertoires</label>
    </div>
    <div class="row" style="margin-top:14px">
      <button id="btn-index">Lancer l'indexation</button>
      <button id="btn-refresh" class="secondary">Actualiser état</button>
      <div class="status" id="status"></div>
    </div>
  </section>

  <!-- Surveillance en temps réel -->
  <section class="card">
    <h3 style="margin-top:0">🔍 Surveillance en temps réel</h3>
    <div class="field-label" style="margin-bottom:8px">
      Sélectionnez un répertoire à surveiller avec watchdog
      <span style="font-size:11px;color:var(--muted);margin-left:8px">
        (Entrez manuellement le chemin ou utilisez le bouton "Parcourir" si votre navigateur le permet)
      </span>
    </div>
    <div class="row" style="margin-bottom:12px; gap:8px">
      <input type="text" id="watchdog-path" placeholder="Ex: C:\\MonDossier ou /home/user/Documents" 
             style="flex:1; background:var(--input-bg); color:var(--text); border:1px solid var(--border); border-radius:8px; padding:10px 14px; font-size:14px">
      <button id="btn-select-dir" class="secondary">Parcourir...</button>
      <button id="btn-paste-path" class="secondary">Coller</button>
    </div>
    <div class="row" style="gap:8px">
      <button id="btn-start-watchdog" disabled>Démarrer la surveillance</button>
      <button id="btn-stop-watchdog" class="warn" disabled>Arrêter la surveillance</button>
    </div>
    <div class="status" id="watchdog-status" style="margin-top:12px">
      Surveillance en temps réel : <span id="watchdog-state">inactive</span>
    </div>
    <div id="watchdog-drives-list" style="margin-top:8px; font-size:13px; color:var(--muted)"></div>
  </section>

  <section class="card">
    <h3 style="margin-top:0">Recherche multi-critères</h3>

    <!-- Sélection des disques de recherche (visible en permanence) -->
    <div style="margin-bottom:14px">
      <div class="field-label" style="margin-bottom:6px">
        💾 Disques à rechercher
        <span style="font-size:11px;color:var(--muted);margin-left:8px">(cliquez pour sélectionner / désélectionner)</span>
      </div>
      <div class="search-drives-bar" id="search-drives"></div>
    </div>

    <!-- Sélecteur de types de fichiers -->
    <div style="margin-bottom:14px">
      <div class="field-label" style="margin-bottom:8px">📂 Types de fichiers</div>

      <div class="ftype-group">
        <div class="ftype-group-label">Sélection rapide</div>
        <div class="ftype-chips">
          <span class="ftype-chip all-chip on" data-exts="">Tous</span>
          <span class="ftype-chip" data-exts="pdf">PDF</span>
          <span class="ftype-chip" data-exts="doc,docx,odt,rtf">Word / Texte</span>
          <span class="ftype-chip" data-exts="xls,xlsx,ods,csv">Tableurs</span>
          <span class="ftype-chip" data-exts="ppt,pptx,odp">Présentations</span>
          <span class="ftype-chip" data-exts="py,js,ts,html,css,json,xml,yaml,yml,sh,bat,ps1,c,cpp,h,java,cs,php,rb,go,rs,sql">Code</span>
          <span class="ftype-chip" data-exts="jpg,jpeg,png,gif,bmp,webp,svg,tif,tiff,ico,heic">Images</span>
          <span class="ftype-chip" data-exts="mp4,mkv,avi,mov,wmv,flv,webm,m4v">Vidéo</span>
          <span class="ftype-chip" data-exts="mp3,wav,flac,aac,ogg,m4a,wma,aiff">Audio</span>
          <span class="ftype-chip" data-exts="zip,rar,7z,tar,gz,bz2,xz,iso">Archives</span>
          <span class="ftype-chip" data-exts="exe,msi,dll,sys">Exécutables</span>
          <span class="ftype-chip" data-exts="txt,md,log,ini,cfg,conf,toml">Texte / Config</span>
        </div>
      </div>

      <div class="ftype-group" style="margin-top:8px">
        <div class="ftype-group-label">Extensions personnalisées</div>
        <div style="display:flex;gap:8px;align-items:center">
          <input id="f-ext" type="text" placeholder="ex : pdf, xlsx, mp3 (séparées par des virgules)"
                 style="max-width:420px" oninput="onExtInput()"/>
          <span style="font-size:12px;color:var(--muted)">← remplace la sélection rapide si renseigné</span>
        </div>
      </div>
    </div>

    <!-- Champ nom + regex -->
    <div class="field" style="margin-bottom:12px">
      <div class="field-label">
        🔎 Nom du fichier / dossier
        <span id="badge-regex" class="badge-regex" style="display:none">REGEX actif</span>
      </div>
      <div class="row" style="gap:8px">
        <input id="q" type="text" placeholder="Texte, wildcards (* ?) ou regex…" autofocus style="flex:1"/>
        <label style="color:var(--text);white-space:nowrap">
          <input type="checkbox" id="opt-regex"> Regex
        </label>
        <button id="btn-search">Rechercher</button>
        <button id="btn-reset" class="secondary">Réinitialiser</button>
      </div>
      <div class="hint" id="text-hint">
        Wildcards : <code>*</code> = n'importe quelle suite · <code>?</code> = un caractère
        &nbsp;|&nbsp; Ex : <code>rapport*</code> · <code>*.pdf</code> · <code>facture_202?.xlsx</code>
        · Sans wildcard : recherche « contient »
      </div>
      <div class="hint" id="regex-hint" style="display:none">
        Regex (insensible à la casse) — Ex : <code>\.pdf$</code> · <code>^rapport_202[34]</code> · <code>(facture|devis).*\.xlsx$</code>
      </div>
    </div>

    <!-- Filtre rapide par date de modification -->
    <div class="field" style="margin-bottom:12px">
      <div class="field-label">📅 Filtrer par date de modification</div>
      <div class="grid3">
        <div class="field">
          <input id="f-dmin-quick" type="text" placeholder="JJ/MM/AAAA (après)" title="Modifié après cette date"/>
        </div>
        <div class="field">
          <input id="f-dmax-quick" type="text" placeholder="JJ/MM/AAAA (avant)" title="Modifié avant cette date"/>
        </div>
        <div class="field">
          <select id="f-date-preset" style="padding:10px 14px;background:var(--input-bg);color:var(--text);
                  border:1px solid var(--border);border-radius:8px;font-size:14px;width:100%">
            <option value="">Prédéfini...</option>
            <option value="today">Aujourd'hui</option>
            <option value="yesterday">Hier</option>
            <option value="week">7 derniers jours</option>
            <option value="month">30 derniers jours</option>
            <option value="year">12 derniers mois</option>
          </select>
        </div>
      </div>
    </div>

    <!-- Options de base -->
    <div class="opts">
      <label><input type="checkbox" id="opt-sizes" checked> Afficher les tailles</label>
      <label><input type="checkbox" id="opt-mtime" checked> Afficher modifié le</label>
      <label><input type="checkbox" id="opt-dirs-only"> Dossiers uniquement</label>
      <label><input type="checkbox" id="opt-files-only"> Fichiers uniquement</label>
      <label><input type="checkbox" id="opt-subdirs" checked> Inclure les sous-répertoires</label>
    </div>

    <!-- Panneau avancé (taille + chemin) -->
    <button class="toggle-advanced" id="btn-advanced" onclick="toggleAdvanced()">▶ Critères avancés (taille, date, chemin)</button>
    <div id="advanced-panel">
      <hr class="sep"/>
      <div class="grid3">
        <div class="field">
          <div class="field-label">Taille minimale</div>
          <input id="f-smin" type="text" placeholder="ex : 100Ko, 5Mo"/>
        </div>
        <div class="field">
          <div class="field-label">Taille maximale</div>
          <input id="f-smax" type="text" placeholder="ex : 1Go"/>
        </div>
        <div class="field">
          <div class="field-label">Limiter à un chemin</div>
          <input id="f-path" type="text" placeholder="ex : D:\Documents\Projets"/>
          <div class="hint">Restreint la recherche à ce dossier et ses sous-dossiers</div>
        </div>
      </div>
      <hr class="sep"/>
      <div class="grid3">
        <div class="field">
          <div class="field-label">Modifié après le</div>
          <input id="f-dmin" type="text" placeholder="JJ/MM/AAAA"/>
        </div>
        <div class="field">
          <div class="field-label">Modifié avant le</div>
          <input id="f-dmax" type="text" placeholder="JJ/MM/AAAA"/>
        </div>
        <div class="field">
          <div class="field-label">Trier par</div>
          <select id="f-sort" style="padding:10px 14px;background:var(--input-bg);color:var(--text);
                  border:1px solid var(--border);border-radius:8px;font-size:14px;width:100%">
            <option value="name">Nom (A→Z)</option>
            <option value="date_desc">Date (plus récent d'abord)</option>
            <option value="date_asc">Date (plus ancien d'abord)</option>
          </select>
        </div>
      </div>
    </div>

    <div class="meta" id="meta"></div>
    <div id="error-box" class="error-msg" style="display:none"></div>
    <div id="results"></div>
  </section>

</main>

<script>
let DRIVES = [];
let SELECTED_IDX    = new Set();
let SELECTED_SEARCH = new Set();

// --- Types de fichiers -------------------------------------------------------
// getSelectedExts() retourne la liste des extensions actives ([] = toutes)
function getSelectedExts(){
  const manual = document.getElementById('f-ext').value.trim();
  if(manual){
    return manual.split(',').map(e=>e.trim().replace(/^\./,'')).filter(Boolean);
  }
  const active = [...document.querySelectorAll('.ftype-chip.on')];
  // Si "Tous" est dans la sélection ou aucun chip actif → pas de filtre
  if(active.some(c=>c.dataset.exts==='')||active.length===0) return [];
  const exts = new Set();
  active.forEach(c=>c.dataset.exts.split(',').forEach(e=>exts.add(e)));
  return [...exts];
}

function onExtInput(){
  // Si l'utilisateur saisit manuellement, on désactive les chips visuellement
  const hasManual = document.getElementById('f-ext').value.trim().length > 0;
  document.querySelectorAll('.ftype-chip').forEach(c=>{
    c.style.opacity = hasManual ? '0.4' : '';
    c.style.pointerEvents = hasManual ? 'none' : '';
  });
}

document.querySelectorAll('.ftype-chip').forEach(chip=>{
  chip.addEventListener('click', ()=>{
    if(chip.dataset.exts === ''){
      // "Tous" → désélectionner tout le reste
      document.querySelectorAll('.ftype-chip').forEach(c=>c.classList.remove('on'));
      chip.classList.add('on');
    } else {
      // Désactiver "Tous"
      document.querySelector('.ftype-chip[data-exts=""]').classList.remove('on');
      chip.classList.toggle('on');
      // Si plus rien de sélectionné → remettre "Tous"
      if(!document.querySelector('.ftype-chip.on')){
        document.querySelector('.ftype-chip[data-exts=""]').classList.add('on');
      }
    }
  });
});

// --- Disques -----------------------------------------------------------------
function toggleAdvanced(){
  const p = document.getElementById('advanced-panel');
  const b = document.getElementById('btn-advanced');
  p.classList.toggle('open');
  b.textContent = p.classList.contains('open')
    ? '▼ Critères avancés (taille, date, chemin)'
    : '▶ Critères avancés (taille, date, chemin)';
}

async function loadDrives(){
  const r = await fetch('/api/drives'); const j = await r.json();
  DRIVES = j.drives;
  SELECTED_IDX    = new Set(DRIVES);
  SELECTED_SEARCH = new Set(DRIVES);

  // Chips indexation
  const el = document.getElementById('drives'); el.innerHTML='';
  DRIVES.forEach(d=>{
    const b=document.createElement('span'); b.className='drive-chip on'; b.textContent=d;
    b.onclick=()=>{ if(SELECTED_IDX.has(d)){SELECTED_IDX.delete(d);b.classList.remove('on')}
                    else{SELECTED_IDX.add(d);b.classList.add('on')} };
    el.appendChild(b);
  });

  // Chips recherche (zone principale)
  const el2 = document.getElementById('search-drives'); el2.innerHTML='';
  DRIVES.forEach(d=>{
    const b=document.createElement('span'); b.className='sdrive-chip on'; b.textContent=d;
    b.onclick=()=>{ if(SELECTED_SEARCH.has(d)){SELECTED_SEARCH.delete(d);b.classList.remove('on')}
                    else{SELECTED_SEARCH.add(d);b.classList.add('on')} };
    el2.appendChild(b);
  });
}

async function refreshStatus(){
  const r = await fetch('/api/status'); const j = await r.json();
  const s = document.getElementById('status');
  if(j.running){
    s.textContent = `Indexation ${j.drive} — ${j.count.toLocaleString()} entrées — ${j.current}`;
    document.getElementById('btn-index').disabled = true;
    setTimeout(refreshStatus, 1000);
  } else {
    s.textContent = j.count ? `Prêt. Dernière indexation : ${j.count.toLocaleString()} entrées.` : 'Prêt.';
    document.getElementById('btn-index').disabled = false;
  }
}

// Toggle regex hint
document.getElementById('opt-regex').addEventListener('change', function(){
  document.getElementById('regex-hint').style.display  = this.checked ? 'block' : 'none';
  document.getElementById('text-hint').style.display   = this.checked ? 'none'  : 'block';
  document.getElementById('badge-regex').style.display = this.checked ? 'inline-block' : 'none';
});

// Exclusivité dirs_only / files_only
document.getElementById('opt-dirs-only').addEventListener('change', function(){
  if(this.checked) document.getElementById('opt-files-only').checked = false;
});
document.getElementById('opt-files-only').addEventListener('change', function(){
  if(this.checked) document.getElementById('opt-dirs-only').checked = false;
});

// Réinitialisation
document.getElementById('btn-reset').onclick = ()=>{
  document.getElementById('q').value='';
  document.getElementById('opt-regex').checked=false;
  document.getElementById('opt-sizes').checked=true;
  document.getElementById('opt-mtime').checked=true;
  document.getElementById('opt-dirs-only').checked=false;
  document.getElementById('opt-files-only').checked=false;
  document.getElementById('opt-subdirs').checked=true;
  document.getElementById('f-ext').value='';
  document.getElementById('f-smin').value='';
  document.getElementById('f-smax').value='';
  document.getElementById('f-path').value='';
  document.getElementById('f-dmin').value='';
  document.getElementById('f-dmax').value='';
  document.getElementById('f-dmin-quick').value='';
  document.getElementById('f-dmax-quick').value='';
  document.getElementById('f-date-preset').value='';
  document.getElementById('f-sort').value='name';
  document.getElementById('regex-hint').style.display='none';
  document.getElementById('text-hint').style.display='block';
  document.getElementById('badge-regex').style.display='none';
  document.getElementById('meta').textContent='';
  document.getElementById('results').innerHTML='';
  document.getElementById('error-box').style.display='none';
  // Réinitialiser types de fichiers → "Tous"
  document.querySelectorAll('.ftype-chip').forEach(c=>{
    c.classList.remove('on');
    c.style.opacity=''; c.style.pointerEvents='';
  });
  document.querySelector('.ftype-chip[data-exts=""]').classList.add('on');
  // Réinitialiser disques recherche
  SELECTED_SEARCH = new Set(DRIVES);
  document.querySelectorAll('#search-drives .sdrive-chip').forEach(b=>b.classList.add('on'));
};

// Synchronisation des champs de date rapide avec les champs avancés
function syncDateFields(){
  const dminQuick = document.getElementById('f-dmin-quick').value;
  const dmaxQuick = document.getElementById('f-dmax-quick').value;
  const dminAdv = document.getElementById('f-dmin').value;
  const dmaxAdv = document.getElementById('f-dmax').value;
  
  // Si les champs rapides sont remplis, les copier vers les champs avancés
  if(dminQuick || dmaxQuick){
    if(!dminAdv) document.getElementById('f-dmin').value = dminQuick;
    if(!dmaxAdv) document.getElementById('f-dmax').value = dmaxQuick;
  }
}

// Gestion des prédéfini de date
document.getElementById('f-date-preset').addEventListener('change', function(){
  const preset = this.value;
  const today = new Date();
  const dmin = document.getElementById('f-dmin-quick');
  const dmax = document.getElementById('f-dmax-quick');
  
  if(!preset) return;
  
  const pad = n => String(n).padStart(2,'0');
  const todayStr = `${pad(today.getDate())}/${pad(today.getMonth()+1)}/${today.getFullYear()}`;
  
  switch(preset){
    case 'today':
      dmin.value = todayStr;
      dmax.value = todayStr;
      break;
    case 'yesterday':
      const yesterday = new Date(today);
      yesterday.setDate(yesterday.getDate() - 1);
      dmin.value = `${pad(yesterday.getDate())}/${pad(yesterday.getMonth()+1)}/${yesterday.getFullYear()}`;
      dmax.value = `${pad(yesterday.getDate())}/${pad(yesterday.getMonth()+1)}/${yesterday.getFullYear()}`;
      break;
    case 'week':
      const weekAgo = new Date(today);
      weekAgo.setDate(weekAgo.getDate() - 7);
      dmin.value = `${pad(weekAgo.getDate())}/${pad(weekAgo.getMonth()+1)}/${weekAgo.getFullYear()}`;
      dmax.value = todayStr;
      break;
    case 'month':
      const monthAgo = new Date(today);
      monthAgo.setDate(monthAgo.getDate() - 30);
      dmin.value = `${pad(monthAgo.getDate())}/${pad(monthAgo.getMonth()+1)}/${monthAgo.getFullYear()}`;
      dmax.value = todayStr;
      break;
    case 'year':
      const yearAgo = new Date(today);
      yearAgo.setDate(yearAgo.getDate() - 365);
      dmin.value = `${pad(yearAgo.getDate())}/${pad(yearAgo.getMonth()+1)}/${yearAgo.getFullYear()}`;
      dmax.value = todayStr;
      break;
  }
  
  // Réinitialiser le sélecteur
  this.value = '';
});

// Synchroniser avant la recherche
document.getElementById('btn-search').onclick = ()=>{
  syncDateFields();
  doSearch();
};

document.getElementById('q').addEventListener('keydown',e=>{
  if(e.key==='Enter'){
    syncDateFields();
    doSearch();
  }
});

document.getElementById('btn-index').onclick = async ()=>{
  if(SELECTED_IDX.size===0){alert('Sélectionnez au moins un disque.');return;}
  const body = { drives:[...SELECTED_IDX] };
  await fetch('/api/index',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  refreshStatus();
};
document.getElementById('btn-refresh').onclick = refreshStatus;

async function doSearch(){
  const q = document.getElementById('q').value.trim();
  if(!q && getSelectedExts().length===0){
    document.getElementById('meta').textContent='Saisissez un nom ou sélectionnez au moins un type de fichier.';
    return;
  }
  const useRegex = document.getElementById('opt-regex').checked;
  const exts = getSelectedExts();
  const params = new URLSearchParams({
    q: q,
    regex: useRegex ? '1' : '0',
    drives: [...SELECTED_SEARCH].join('|'),
    subdirs: document.getElementById('opt-subdirs').checked ? '1':'0',
    dirs_only: document.getElementById('opt-dirs-only').checked ? '1':'0',
    files_only: document.getElementById('opt-files-only').checked ? '1':'0',
    ext: exts.join(','),
    smin: document.getElementById('f-smin').value.trim(),
    smax: document.getElementById('f-smax').value.trim(),
    path_filter: document.getElementById('f-path').value.trim(),
    dmin: document.getElementById('f-dmin').value.trim(),
    dmax: document.getElementById('f-dmax').value.trim(),
    sort: document.getElementById('f-sort').value,
  });

  document.getElementById('error-box').style.display='none';
  document.getElementById('meta').textContent='Recherche en cours…';
  document.getElementById('results').innerHTML='';

  const r = await fetch('/api/search?'+params);
  const j = await r.json();
  const showSize = document.getElementById('opt-sizes').checked;
  const showMtime = document.getElementById('opt-mtime').checked;

  if(j.error){
    document.getElementById('error-box').textContent='⚠ ' + j.error;
    document.getElementById('error-box').style.display='block';
    document.getElementById('meta').textContent='';
    return;
  }

  // Résumé des filtres actifs
  let filterSummary = [];
  if(SELECTED_SEARCH.size < DRIVES.length)
    filterSummary.push(`disques: ${[...SELECTED_SEARCH].join(' ')}`);
  if(exts.length) filterSummary.push(`types: .${exts.join(' .')}`);
  
  // Ajouter le filtre par date si actif
  const dmin = document.getElementById('f-dmin').value.trim();
  const dmax = document.getElementById('f-dmax').value.trim();
  if(dmin || dmax){
    const dateFilter = [];
    if(dmin) dateFilter.push(`après ${dmin}`);
    if(dmax) dateFilter.push(`avant ${dmax}`);
    filterSummary.push(`date: ${dateFilter.join(' et ')}`);
  }
  
  const summary = filterSummary.length ? ` [${filterSummary.join(' | ')}]` : '';

  document.getElementById('meta').textContent =
    `${j.total} résultat(s)${summary}` + (j.truncated?' — limités à 1 000 (affinez les critères)':'');

  function fmtDate(ts){
    if(!ts || ts <= 0) return '<span style="color:var(--border)">—</span>';
    const d = new Date(ts * 1000);
    const pad = n => String(n).padStart(2,'0');
    return `${pad(d.getDate())}/${pad(d.getMonth()+1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  // Avertissement si aucune date disponible (base indexée avant la mise à jour)
  if(showMtime && j.results.length > 0 && j.results.every(r => !r.mtime)){
    document.getElementById('meta').textContent +=
      ' ⚠ Dates de modification non disponibles — relancez une indexation.';
  }

  const rows = j.results.map(it=>{
    const sz   = showSize ? `<td>${it.size_h}</td>` : '';
    const dt   = showMtime ? `<td style="white-space:nowrap;font-size:12px;color:var(--muted)">${fmtDate(it.mtime)}</td>` : '';
    const tag  = it.is_dir ? '<span class="tag dir">DOSSIER</span>' : '<span class="tag file">FICHIER</span>';
    const openHref   = '/open?path='  +encodeURIComponent(it.path);
    const revealHref = '/reveal?path='+encodeURIComponent(it.path);
    return `<tr>
      <td>${tag}<a class="link" href="${openHref}" target="_blank">${escapeHtml(it.name)}</a></td>
      <td><a class="link" href="${revealHref}" target="_blank">${escapeHtml(it.parent)}</a></td>
      ${sz}${dt}
    </tr>`;
  }).join('');

  const szTh = showSize ? '<th>Taille</th>' : '';
  const currentSort = document.getElementById('f-sort').value;
  const dtArrow = currentSort === 'date_desc' ? ' ▼' : currentSort === 'date_asc' ? ' ▲' : '';
  const dtTh = showMtime
    ? `<th style="cursor:pointer;user-select:none" title="Cliquer pour trier" onclick="cycleSort()">`
      + `Modifié le${dtArrow}</th>`
    : '';
  document.getElementById('results').innerHTML = j.results.length
    ? `<table><thead><tr><th>Nom</th><th>Emplacement</th>${szTh}${dtTh}</tr></thead>
       <tbody>${rows}</tbody></table>`
    : '<p style="color:var(--muted);margin-top:12px">Aucun résultat.</p>';
}

function cycleSort(){
  const sel = document.getElementById('f-sort');
  if(sel.value === 'date_desc') sel.value = 'date_asc';
  else sel.value = 'date_desc';
  doSearch();
}

function escapeHtml(s){return s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}



// --- Thèmes ------------------------------------------------------------------
const THEMES = ['default','slate','forest','bordeaux','light','sepia'];

function applyTheme(name){
  document.body.className = name === 'default' ? '' : 'theme-' + name;
  document.querySelectorAll('.theme-dot').forEach(d=>{
    d.classList.toggle('active', d.dataset.theme === name);
  });
  try { localStorage.setItem('di-theme', name); } catch(e){}
}

document.querySelectorAll('.theme-dot').forEach(dot=>{
  dot.addEventListener('click', ()=> applyTheme(dot.dataset.theme));
});

// Restaurer le thème mémorisé
(function(){
  try {
    const saved = localStorage.getItem('di-theme');
    if(saved && THEMES.includes(saved)) applyTheme(saved);
  } catch(e){}
})();

// --- Bouton Quitter ----------------------------------------------------------
document.getElementById('btn-quit').onclick = async ()=>{
  if(!confirm('Arrêter DiskIndexer ?\nLe navigateur restera ouvert mais l\'outil ne sera plus accessible.')) return;
  try { await fetch('/api/shutdown'); } catch(e){}
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:Segoe UI,sans-serif;color:#94a3b8;background:#0f172a"><div style="text-align:center"><div style="font-size:48px;margin-bottom:16px">⏻</div><div style="font-size:20px">DiskIndexer arrêté.</div><div style="font-size:14px;margin-top:8px">Vous pouvez fermer cet onglet.</div></div></div>';
};

// --- Surveillance en temps réel -------------------------------------------
let WATCHDOG_SELECTED_PATH = null;

// Mettre à jour l'interface de surveillance
function updateWatchdogUI() {
  const pathInput = document.getElementById('watchdog-path');
  const startBtn = document.getElementById('btn-start-watchdog');
  const stopBtn = document.getElementById('btn-stop-watchdog');
  const stateEl = document.getElementById('watchdog-state');
  const drivesListEl = document.getElementById('watchdog-drives-list');

  // Mettre à jour le champ de chemin depuis l'input
  const currentPath = pathInput.value.trim();
  if (currentPath) {
    WATCHDOG_SELECTED_PATH = currentPath;
  }

  // Vérifier l'état de la surveillance
  fetch('/api/watchdog/status')
    .then(r => r.json())
    .then(data => {
      const isRunning = data.running;
      const drives = data.drives || [];
      const available = data.available;

      // Mettre à jour l'état
      stateEl.textContent = isRunning ? 'active' : 'inactive';
      stateEl.style.color = isRunning ? 'var(--success)' : 'var(--muted)';

      // Mettre à jour la liste des répertoires surveillés
      if (drives.length > 0) {
        drivesListEl.textContent = 'Répertoires surveillés : ' + drives.join(', ');
      } else {
        drivesListEl.textContent = '';
      }

      // Gérer les boutons
      stopBtn.disabled = !isRunning;
      startBtn.disabled = !WATCHDOG_SELECTED_PATH || isRunning;

      // Si la surveillance est active mais qu'aucun chemin n'est sélectionné,
      // c'est que la surveillance a été démarrée automatiquement au démarrage
      if (isRunning && !WATCHDOG_SELECTED_PATH && drives.length > 0) {
        WATCHDOG_SELECTED_PATH = drives[0];
        pathInput.value = WATCHDOG_SELECTED_PATH;
      }

      // Si la surveillance n'est pas disponible
      if (!available) {
        startBtn.disabled = true;
        startBtn.title = 'watchdog non installé - installez-le avec : pip install watchdog';
      } else {
        startBtn.title = '';
      }
    })
    .catch(() => {
      stateEl.textContent = 'erreur';
      stateEl.style.color = 'var(--error)';
    });
}

// Sélectionner un répertoire (pour Electron ou navigateurs avec accès au système de fichiers)
async function selectDirectory() {
  // Essayer d'utiliser un input de type file avec webkitdirectory
  return new Promise((resolve) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.webkitdirectory = true;
    input.directory = true;
    input.multiple = false;
    input.style.display = 'none';
    
    input.addEventListener('change', async (e) => {
      if (e.target.files && e.target.files.length > 0) {
        const file = e.target.files[0];
        
        // Pour Electron ou navigateurs avec accès au système de fichiers
        if (file.path) {
          // Extraire le chemin du répertoire parent
          let dirPath = file.path;
          // Si c'est un fichier, obtenir le répertoire parent
          if (file.path.includes('/') || file.path.includes('\\')) {
            const lastSlash = Math.max(
              file.path.lastIndexOf('/'),
              file.path.lastIndexOf('\\')
            );
            dirPath = file.path.substring(0, lastSlash);
          }
          document.getElementById('watchdog-path').value = dirPath;
          updateWatchdogUI();
          document.body.removeChild(input);
          resolve();
          return;
        }
        
        // Pour les navigateurs standards, on ne peut pas obtenir le chemin absolu
        alert('Votre navigateur ne permet pas d\'obtenir le chemin absolu du répertoire.\n\n' +
              'Veuillez entrer manuellement le chemin du répertoire dans le champ de texte.');
      }
      document.body.removeChild(input);
      resolve();
    });
    
    document.body.appendChild(input);
    input.click();
  });
}

// Démarrer la surveillance
async function startWatchdog() {
  if (!WATCHDOG_SELECTED_PATH) {
    alert('Veuillez d\'abord sélectionner un répertoire.');
    return;
  }

  try {
    const response = await fetch('/api/watchdog/start/path', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ path: WATCHDOG_SELECTED_PATH })
    });

    const data = await response.json();
    
    if (data.started) {
      alert('Surveillance démarrée avec succès pour: ' + data.path);
      updateWatchdogUI();
    } else {
      alert('Erreur: ' + (data.error || 'Impossible de démarrer la surveillance'));
    }
  } catch (error) {
    console.error('Erreur:', error);
    alert('Erreur lors du démarrage de la surveillance: ' + error.message);
  }
}

// Arrêter la surveillance
async function stopWatchdog() {
  try {
    const response = await fetch('/api/watchdog/stop', {
      method: 'POST'
    });

    const data = await response.json();
    
    if (data.stopped) {
      alert('Surveillance arrêtée');
      updateWatchdogUI();
    }
  } catch (error) {
    console.error('Erreur:', error);
    alert('Erreur lors de l\'arrêt de la surveillance: ' + error.message);
  }
}

// Initialiser les écouteurs d'événements
document.getElementById('btn-select-dir').addEventListener('click', selectDirectory);
document.getElementById('btn-start-watchdog').addEventListener('click', startWatchdog);
document.getElementById('btn-stop-watchdog').addEventListener('click', stopWatchdog);
document.getElementById('btn-paste-path').addEventListener('click', pastePath);
document.getElementById('watchdog-path').addEventListener('input', updateWatchdogUI);

// Coller le chemin depuis le presse-papiers
async function pastePath() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      document.getElementById('watchdog-path').value = text;
      updateWatchdogUI();
    }
  } catch (error) {
    console.error('Impossible de coller:', error);
    // Méthode alternative pour les navigateurs plus anciens
    const text = prompt('Collez le chemin du répertoire ici:');
    if (text) {
      document.getElementById('watchdog-path').value = text;
      updateWatchdogUI();
    }
  }
}

// Mettre à jour l'UI au chargement
updateWatchdogUI();

loadDrives(); refreshStatus();
</script>
</body></html>
"""

# --- Serveur HTTP ------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        if u.path == "/api/drives":
            return self._send(200, json.dumps({"drives": list_drives()}))
        if u.path == "/api/status":
            conn = db_connect()
            count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            conn.close()
            st = dict(INDEX_STATE); st["count"] = st["count"] if st["running"] else count
            return self._send(200, json.dumps(st))
        if u.path == "/api/search":
            qs = parse_qs(u.query)
            q         = (qs.get("q", [""])[0] or "").strip()
            use_regex = qs.get("regex", ["0"])[0] == "1"
            drives    = [d for d in (qs.get("drives", [""])[0].split("|")) if d]
            subdirs   = qs.get("subdirs", ["1"])[0] == "1"
            dirs_only = qs.get("dirs_only", ["0"])[0] == "1"
            files_only= qs.get("files_only", ["0"])[0] == "1"
            ext_filter= qs.get("ext", [""])[0]
            smin_str  = qs.get("smin", [""])[0]
            smax_str  = qs.get("smax", [""])[0]
            path_filter = qs.get("path_filter", [""])[0].strip()
            size_min  = parse_size(smin_str)
            size_max  = parse_size(smax_str)
            date_min  = parse_date(qs.get("dmin", [""])[0])
            date_max  = parse_date(qs.get("dmax", [""])[0])
            sort_by   = qs.get("sort", ["name"])[0]
            return self._send(200, json.dumps(
                search(q, use_regex, drives, subdirs, dirs_only, files_only,
                       ext_filter, size_min, size_max, path_filter,
                       date_min, date_max, sort_by)
            ))
        if u.path == "/api/shutdown":
            # Arrêt propre : on coupe le serveur et l'observateur watchdog dans un thread séparé
            def shutdown_all():
                stop_watchdog_observer()
                self.server_ref.shutdown()
            threading.Thread(target=shutdown_all, daemon=True).start()
            return self._send(200, json.dumps({"bye": True}))
        if u.path == "/open":
            path = parse_qs(u.query).get("path", [""])[0]
            ok = open_in_explorer(path)
            return self._send(200, f"<script>window.close()</script>{'OK' if ok else 'Introuvable'}",
                              "text/html; charset=utf-8")
        if u.path == "/reveal":
            path = parse_qs(u.query).get("path", [""])[0]
            reveal_in_explorer(path)
            return self._send(200, "<script>window.close()</script>OK", "text/html; charset=utf-8")
        return self._send(404, "Not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try: data = json.loads(raw or b"{}")
        except: data = {}
        if u.path == "/api/index":
            drives = data.get("drives") or list_drives()
            started = start_index_async(drives)
            return self._send(200, json.dumps({"started": started}))
        if u.path == "/api/watchdog/start":
            drives = data.get("drives") or list_drives()
            if WATCHDOG_AVAILABLE:
                observer = start_watchdog_observer(drives)
                return self._send(200, json.dumps({
                    "started": observer is not None,
                    "drives": list(WATCHDOG_DRIVES)
                }))
            else:
                return self._send(200, json.dumps({
                    "started": False,
                    "error": "watchdog non installé"
                }))
        if u.path == "/api/watchdog/start/path":
            # Démarrer la surveillance pour un répertoire spécifique
            path = data.get("path")
            if not path:
                return self._send(400, json.dumps({"error": "Aucun chemin spécifié"}))
            if WATCHDOG_AVAILABLE:
                success = start_watchdog_for_path(path)
                return self._send(200, json.dumps({
                    "started": success,
                    "path": path if success else None,
                    "drives": list(WATCHDOG_DRIVES) if success else []
                }))
            else:
                return self._send(200, json.dumps({
                    "started": False,
                    "error": "watchdog non installé"
                }))
        if u.path == "/api/watchdog/stop":
            stop_watchdog_observer()
            return self._send(200, json.dumps({"stopped": True}))
        if u.path == "/api/watchdog/status":
            return self._send(200, json.dumps({
                "running": WATCHDOG_OBSERVER is not None,
                "drives": list(WATCHDOG_DRIVES),
                "available": WATCHDOG_AVAILABLE
            }))
        return self._send(404, "Not found", "text/plain")

def hide_console():
    """Masque la fenêtre console Windows sans passer par --noconsole PyInstaller."""
    if os.name == "nt":
        try:
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE = 0
        except Exception:
            pass

def main():
    db_init()
    
    # Lancer l'observateur watchdog pour la surveillance en temps réel
    drives = list_drives()
    global WATCHDOG_OBSERVER
    
    if WATCHDOG_AVAILABLE:
        WATCHDOG_OBSERVER = start_watchdog_observer(drives)
        if WATCHDOG_OBSERVER:
            print("✓ watchdog est installé - surveillance en temps réel activée")
        else:
            print("⚠ watchdog non disponible - pas de surveillance en temps réel")
    else:
        print("⚠ watchdog non installé - pas de surveillance en temps réel")
        print("  Installez-le avec : pip install watchdog")
    
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    Handler.server_ref = srv  # expose le serveur au Handler pour /api/shutdown
    url = f"http://127.0.0.1:{PORT}/"
    print(f"DiskIndexer démarré : {url}")
    print(f"Base de données : {DB_PATH}")
    hide_console()
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop_watchdog_observer()
    print("Arrêt.")

if __name__ == "__main__":
    main()
