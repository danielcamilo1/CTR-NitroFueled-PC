# SPDX-License-Identifier: GPL-2.0-or-later
"""The Linux launcher's work that needs no GTK: what a game folder holds, preparing the game (base
+ 1.21 update, or a PS4 PKG Tool dump), saves and backups, DLC, keyboard bindings, patches and the
text in Spanish or English. It does what launcher/windows/launcher.cpp does, so the two launchers
behave alike; launcher/sacredfire_launcher.py draws the tabs over it. tests/test_launcher_core.py
covers it.
"""
import json
import os
import shutil
import struct
import sys
import time
from pathlib import Path

PORT_DIR = Path(__file__).resolve().parent.parent
for _folder in ("scripts", "tools"):
    if str(PORT_DIR / _folder) not in sys.path:
        sys.path.insert(0, str(PORT_DIR / _folder))
import content_profile  # noqa: E402
import merge_update  # noqa: E402
import patches as game_patches  # noqa: E402
from prepare import sfo  # noqa: E402

TITLE_ID = "CUSA13795"
UPDATE_VERSION = "01.21"
# A complete 1.21 game is far larger than the update alone (the Windows launcher's threshold).
COMPLETE_GAME_MIN_BYTES = 10 << 30
REPO_URL = "https://github.com/danielcamilo1/CTR-NitroFueled-PC"

# --- text ----------------------------------------------------------------------------------------
# Spanish or English, as the Windows launcher: ui_language "es", "en" or "" (the system's).

_spanish = False


def system_spanish():
    for key in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(key, "")
        if value:
            return value.lower().startswith("es")
    return False


def set_language(choice):
    global _spanish
    _spanish = choice == "es" if choice in ("es", "en") else system_spanish()


def spanish():
    return _spanish


def T(es, en):
    return es if _spanish else en


# --- settings files ------------------------------------------------------------------------------

class Ini:
    """bbport.ini as lines: values are read, set and erased in place, other lines are kept (the
    in-game menu writes the same file)."""

    def __init__(self, path, default=None):
        self.path = Path(path)
        try:
            self.lines = self.path.read_text().splitlines()
        except OSError:
            try:
                self.lines = Path(default).read_text().splitlines() if default else []
            except OSError:
                self.lines = []

    @staticmethod
    def _key(line):
        if "=" in line and not line.lstrip().startswith("#"):
            return line.split("=", 1)[0].strip()
        return None

    def has(self, key):
        return any(self._key(line) == key for line in self.lines)

    def get(self, key, fallback=""):
        for line in self.lines:
            if self._key(line) == key:
                return line.split("=", 1)[1].strip()
        return fallback

    def flag(self, key, fallback=False):
        return self.get(key, "1" if fallback else "0") == "1"

    def set(self, key, value):
        for i, line in enumerate(self.lines):
            if self._key(line) == key:
                self.lines[i] = f"{key}={value}"
                return
        if not self.lines:
            self.lines.append("# bbport settings (in-game menu: Insert / L3+R3)")
        self.lines.append(f"{key}={value}")

    def erase(self, key):
        self.lines = [line for line in self.lines if self._key(line) != key]

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text("\n".join(self.lines) + "\n")
        os.replace(temp, self.path)


# --- game folders --------------------------------------------------------------------------------

EMPTY, MISSING, NOT_GAME, OTHER_TITLE, OTHER_VERSION, BASE, UPDATE, COMPLETE = (
    "empty", "missing", "not_game", "other_title", "other_version", "base", "update", "complete")


def folder_bytes(path):
    total = 0
    for directory, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(directory) / name).stat().st_size
            except OSError:
                pass
    return total


def format_bytes(count):
    if count >= 1 << 30:
        return f"{count / (1 << 30):.1f} GB"
    if count >= 1 << 20:
        return f"{count / (1 << 20):.1f} MB"
    return f"{count / 1024:.0f} KB"


def _param_sfo(trees):
    for root, prefix in trees:
        candidate = root / "param.sfo" if prefix == Path("sce_sys") else root / "sce_sys/param.sfo"
        if candidate.is_file():
            return candidate
    return None


def analyze(folder):
    """What a folder holds: dict(kind, title, version, bytes, split). split: the PS4 PKG Tool
    layout (Image0 + Sc0), which preparing the game flattens."""
    info = dict(kind=EMPTY, title="", version="", bytes=0, split=False)
    if not folder:
        return info
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        info["kind"] = MISSING
        return info
    trees = merge_update.layout(folder)
    sfo_path = _param_sfo(trees) if trees else None
    if not trees or not sfo_path:
        info["kind"] = NOT_GAME
        return info
    info["split"] = trees[0][0] != folder
    try:
        values = sfo(sfo_path.read_bytes())
    except (OSError, ValueError, IndexError, struct.error):
        values = {}
    info["title"], info["version"] = values.get("TITLE_ID", ""), values.get("APP_VER", "")
    if info["title"] != TITLE_ID:
        info["kind"] = OTHER_TITLE
    elif values.get("CATEGORY") == "gd":
        info["kind"] = BASE
    elif info["version"] != UPDATE_VERSION:
        info["kind"] = OTHER_VERSION
    else:
        info["bytes"] = folder_bytes(trees[0][0])
        info["kind"] = COMPLETE if info["bytes"] >= COMPLETE_GAME_MIN_BYTES else UPDATE
    return info


def describe(info):
    kind = info["kind"]
    if kind == EMPTY:
        return T("sin elegir", "not chosen")
    if kind == MISSING:
        return T("la carpeta no existe", "the folder does not exist")
    if kind == NOT_GAME:
        return T("no tiene eboot.bin y sce_sys/param.sfo (ni Image0 y Sc0)",
                 "no eboot.bin and sce_sys/param.sfo (nor Image0 and Sc0)")
    if kind == OTHER_TITLE:
        return T("es otro juego o región: ", "another game or region: ") + info["title"]
    if kind == OTHER_VERSION:
        return T("actualización ", "update ") + info["version"] + T(" (se necesita la 01.21)", " (01.21 is needed)")
    if kind == BASE:
        return T("juego base ", "base game ") + f"{info['title']} v{info['version']}"
    if kind == UPDATE:
        return T("actualización ", "update ") + f"{info['version']} ({format_bytes(info['bytes'])})"
    return f"{info['title']} v{info['version']} ({format_bytes(info['bytes'])})"


def game_root(folder):
    """A folder picked one level too high (the one that holds CUSA13795) stands for its only game;
    Image0 picked from a PS4 PKG Tool dump stands for the dump."""
    folder = Path(folder)
    if merge_update.layout(folder):
        return folder
    if folder.name.lower() == "image0" and merge_update.layout(folder.parent):
        return folder.parent
    found = None
    try:
        children = sorted(p for p in folder.iterdir() if p.is_dir())
    except OSError:
        return folder
    for child in children:
        if merge_update.layout(child):
            if found:
                return folder  # more than one: the player chooses
            found = child
    return found or folder


def merge_target(near, taken=()):
    """Where the prepared game goes: CUSA13795-1.21 next to the source (-merged if that is taken)."""
    near = Path(near)
    target = near.parent / "CUSA13795-1.21"
    if target == near or target in [Path(p) for p in taken if p]:
        target = near.parent / "CUSA13795-1.21-merged"
    return target


def merge_files(sources):
    """(source file, path in the PS4 layout) of the dumps in order, later ones over earlier ones."""
    files = {}
    for folder in sources:
        for root, prefix in merge_update.layout(Path(folder)) or []:
            for directory, _dirs, names in os.walk(root):
                for name in names:
                    source = Path(directory) / name
                    files[prefix / source.relative_to(root)] = source
    return [(source, relative) for relative, source in files.items()]


def merge(sources, target, progress=None):
    """The dumps merged into target as hard links (copies across drives). Returns (files, copied)."""
    files = merge_files(sources)
    counts = {"linked": 0, "copied": 0}
    for done, (source, relative) in enumerate(files, 1):
        destination = Path(target) / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        merge_update.link_or_copy(source, destination, counts)
        if progress:
            progress(done, len(files))
    return len(files), counts["copied"]


# --- saves ---------------------------------------------------------------------------------------
# <user>/savedata/<user id>/CUSA13795/<dir> holds the game's files and <dir>.sce_sys its param.bin
# (OrbisSaveDataParam: title, subtitle, detail) and icon0.png, as src/runtime_savedata.c keeps them.

SAVE_PARAM = struct.Struct("<128s128s1024sIiq32s")


def save_root(user_dir):
    base = Path(user_dir) / "savedata"
    try:
        for child in sorted(base.iterdir()):
            if (child / TITLE_ID).is_dir():
                return child / TITLE_ID
    except OSError:
        pass
    return base / "1" / TITLE_ID


def _meta_name(name):
    return name.endswith(".sce_sys") or name.endswith(".memory")


def _stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def _modified(path):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
    except OSError:
        return ""


def list_saves(root):
    slots = []
    try:
        children = sorted(p for p in Path(root).iterdir() if p.is_dir() and not _meta_name(p.name))
    except OSError:
        return slots
    for child in children:
        slot = dict(dir=child.name, title="", subtitle="", detail="", bytes=folder_bytes(child),
                    modified=_modified(child))
        try:
            raw = (child.parent / (child.name + ".sce_sys") / "param.bin").read_bytes()
            title, subtitle, detail, *_ = SAVE_PARAM.unpack_from(raw)
            for key, value in (("title", title), ("subtitle", subtitle), ("detail", detail)):
                slot[key] = value.split(b"\0", 1)[0].decode("utf8", "replace")
        except (OSError, struct.error):
            pass
        for directory, _dirs, names in os.walk(child):
            for name in names:
                slot["modified"] = max(slot["modified"], _modified(Path(directory) / name))
        slots.append(slot)
    return slots


def list_backups(backup_root):
    try:
        found = [p for p in Path(backup_root).iterdir() if p.is_dir() and p.name.startswith("saves-")]
    except OSError:
        return []
    return [dict(path=p, name=p.name, modified=_modified(p), bytes=folder_bytes(p))
            for p in sorted(found, key=lambda p: p.name, reverse=True)]


def has_saves(root):
    try:
        return any(Path(root).iterdir())
    except OSError:
        return False


def backup_saves(root, backup_root, label=None):
    """A copy of all saves in backups/saves-<date>[-label]; None when there is nothing to copy."""
    if not has_saves(root):
        return None
    target = Path(backup_root) / ("saves-" + _stamp() + (f"-{label}" if label else ""))
    shutil.copytree(root, target, dirs_exist_ok=True)
    return target


def restore_backup(backup, root, backup_root):
    backup_saves(root, backup_root, "auto")
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(backup, root, dirs_exist_ok=True)


def export_saves(root, destination):
    target = Path(destination) / ("CTR-saves-" + _stamp())
    shutil.copytree(root, target, dirs_exist_ok=True)
    return target


def import_saves(source, root, backup_root):
    """A save folder from elsewhere: this launcher's backup or another PC's CUSA13795 folder (copied
    as it is), or a decrypted PS4 save (Apollo Save Tool, Save Wizard: its files plus
    sce_sys/param.sfo), converted to the runtime's layout. ValueError: not a save folder."""
    source = Path(source)
    sfo_path = source / "sce_sys/param.sfo"
    if sfo_path.is_file():
        values = sfo(sfo_path.read_bytes())
        name = str(values.get("SAVEDATA_DIRECTORY", ""))
        if not name or len(name) >= 32 or any(c in name for c in "\\/:"):
            raise ValueError(T("param.sfo no dice en qué ranura va la partida (SAVEDATA_DIRECTORY).",
                               "param.sfo does not name the save slot (SAVEDATA_DIRECTORY)."))
        backup_saves(root, backup_root, "antes-de-importar")
        target = Path(root) / name
        meta = Path(root) / (name + ".sce_sys")
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        meta.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            if item.name == "sce_sys":
                continue
            if item.is_dir():
                shutil.copytree(item, target / item.name, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target / item.name)
        def text(key, size):
            return str(values.get(key, "")).encode("utf8")[:size - 1]
        (meta / "param.bin").write_bytes(SAVE_PARAM.pack(text("MAINTITLE", 128), text("SUBTITLE", 128),
                                                         text("DETAIL", 1024), 0, 0, int(time.time()), b""))
        if (source / "sce_sys/icon0.png").is_file():
            shutil.copy2(source / "sce_sys/icon0.png", meta / "icon0.png")
        return
    ours = any(not _meta_name(p.name) and (source / (p.name + ".sce_sys")).is_dir() for p in source.iterdir())
    if not ours:
        raise ValueError(T("No reconozco esa carpeta: elige una copia de seguridad, la carpeta CUSA13795 de las "
                           "partidas de otro PC o una partida de PS4 descifrada (con sce_sys/param.sfo).",
                           "This folder is not recognized: choose a backup, the CUSA13795 saves folder from another "
                           "PC or a decrypted PS4 save (with sce_sys/param.sfo)."))
    backup_saves(root, backup_root, "antes-de-importar")
    shutil.copytree(source, root, dirs_exist_ok=True)


# --- DLC -----------------------------------------------------------------------------------------
# The player's add-on packages in the dlc folder (.pkg files or extracted folders). CTR
# Nitro-Fueled's DLC hold only the license; scripts/content_profile.py reads the same folder.

def describe_dlc(path):
    path = Path(path)
    d = dict(path=path, file=path.name, label="", title="", ok=False, problem="")
    try:
        if path.is_dir():
            if not (path / "sce_sys/param.sfo").is_file():
                d["problem"] = T("la carpeta no tiene sce_sys/param.sfo", "the folder has no sce_sys/param.sfo")
                return d
            values = sfo((path / "sce_sys/param.sfo").read_bytes())
        else:
            values = content_profile.pkg_param(path)
    except (OSError, ValueError, struct.error, IndexError) as error:
        d["problem"] = str(error)
        return d
    content = str(values.get("CONTENT_ID", ""))
    d["label"] = content.rsplit("-", 1)[-1] if "-" in content else ""
    d["title"] = str(values.get("TITLE", "")) or d["label"]
    if values.get("TITLE_ID") != TITLE_ID or values.get("CATEGORY") != "ac" or len(d["label"]) != 16:
        d["problem"] = T("no es un DLC de CTR Nitro-Fueled (CUSA13795)", "not a CTR Nitro-Fueled (CUSA13795) DLC")
        return d
    d["ok"] = True
    return d


def scan_dlc(folder):
    try:
        items = [p for p in Path(folder).iterdir() if p.is_dir() or p.suffix.lower() == ".pkg"]
    except OSError:
        return []
    return sorted((describe_dlc(p) for p in items), key=lambda d: d["title"] or d["file"])


def add_dlc(files, folder):
    """Copies the packages that are this game's DLC into folder: (added, [problems])."""
    Path(folder).mkdir(parents=True, exist_ok=True)
    added, problems = 0, []
    for file in files:
        d = describe_dlc(file)
        if not d["ok"]:
            problems.append(f"{d['file']}: {d['problem']}")
            continue
        try:
            shutil.copy2(file, Path(folder) / Path(file).name)
            added += 1
        except OSError as error:
            problems.append(f"{d['file']}: {error}")
    return added, problems


def split_list(text):
    return [item.strip() for item in (text or "").split(",") if item.strip()]


# --- keyboard ------------------------------------------------------------------------------------
# bbport.ini key.<input>=<SDL key names> (src/runtime_pad.c reads them at start; a missing line
# keeps the runtime's default). Keys are captured by their place on the keyboard: GTK's hardware
# keycode is the Linux evdev code + 8, and evdev codes 1-88 are the PC scan codes the Windows
# launcher maps, so both launchers write the same names.

KEY_INPUTS = [
    ("move_up", "Stick izquierdo arriba", "Left stick up", "W"),
    ("move_down", "Stick izquierdo abajo", "Left stick down", "S"),
    ("move_left", "Stick izquierdo izquierda (girar)", "Left stick left (steer)", "A"),
    ("move_right", "Stick izquierdo derecha (girar)", "Left stick right (steer)", "D"),
    ("cross", "X (acelerar)", "Cross (accelerate)", "Space"),
    ("square", "Cuadrado (frenar)", "Square (brake)", "E"),
    ("circle", "Círculo", "Circle", "Left Shift"),
    ("triangle", "Triángulo", "Triangle", "Q"),
    ("r1", "R1", "R1", "3"),
    ("l1", "L1", "L1", "1"),
    ("r2", "R2", "R2", "F"),
    ("l2", "L2", "L2", "R"),
    ("l3", "L3", "L3", "Z"),
    ("r3", "R3", "R3", "C"),
    ("options", "Options (pausa)", "Options (pause)", "Return"),
    ("touchpad", "Panel táctil", "Touchpad", "Tab"),
    ("touchpad_right", "Panel táctil (derecha)", "Touchpad (right)", "Backspace"),
    ("up", "Cruceta arriba", "D-pad up", "I"),
    ("down", "Cruceta abajo", "D-pad down", "K"),
    ("left", "Cruceta izquierda", "D-pad left", "J"),
    ("right", "Cruceta derecha", "D-pad right", "L"),
    ("look_up", "Stick derecho arriba", "Right stick up", "Up"),
    ("look_down", "Stick derecho abajo", "Right stick down", "Down"),
    ("look_left", "Stick derecho izquierda", "Right stick left", "Left"),
    ("look_right", "Stick derecho derecha", "Right stick right", "Right"),
]
MAX_KEYS = 4
ESCAPE = 1  # evdev KEY_ESC: cancels; KEY_INSERT (110) opens the in-game menu and is not listed

EVDEV_KEYS = {
    0x02: "1", 0x03: "2", 0x04: "3", 0x05: "4", 0x06: "5", 0x07: "6", 0x08: "7", 0x09: "8",
    0x0A: "9", 0x0B: "0", 0x0C: "-", 0x0D: "=", 0x0E: "Backspace", 0x0F: "Tab", 0x10: "Q",
    0x11: "W", 0x12: "E", 0x13: "R", 0x14: "T", 0x15: "Y", 0x16: "U", 0x17: "I", 0x18: "O",
    0x19: "P", 0x1A: "[", 0x1B: "]", 0x1C: "Return", 0x1D: "Left Ctrl", 0x1E: "A", 0x1F: "S",
    0x20: "D", 0x21: "F", 0x22: "G", 0x23: "H", 0x24: "J", 0x25: "K", 0x26: "L", 0x27: ";",
    0x28: "'", 0x29: "`", 0x2A: "Left Shift", 0x2B: "\\", 0x2C: "Z", 0x2D: "X", 0x2E: "C",
    0x2F: "V", 0x30: "B", 0x31: "N", 0x32: "M", 0x33: ",", 0x34: ".", 0x35: "/",
    0x36: "Right Shift", 0x37: "Keypad *", 0x38: "Left Alt", 0x39: "Space", 0x3A: "CapsLock",
    0x3B: "F1", 0x3C: "F2", 0x3D: "F3", 0x3E: "F4", 0x3F: "F5", 0x40: "F6", 0x41: "F7",
    0x42: "F8", 0x43: "F9", 0x44: "F10", 0x57: "F11", 0x58: "F12", 0x47: "Keypad 7",
    0x48: "Keypad 8", 0x49: "Keypad 9", 0x4A: "Keypad -", 0x4B: "Keypad 4", 0x4C: "Keypad 5",
    0x4D: "Keypad 6", 0x4E: "Keypad +", 0x4F: "Keypad 1", 0x50: "Keypad 2", 0x51: "Keypad 3",
    0x52: "Keypad 0", 0x53: "Keypad .", 0x56: "NonUSBackslash",
    # The keys Windows reports with the E0 prefix have their own evdev codes.
    96: "Keypad Enter", 97: "Right Ctrl", 98: "Keypad /", 100: "Right Alt", 102: "Home",
    103: "Up", 104: "PageUp", 105: "Left", 106: "Right", 107: "End", 108: "Down",
    109: "PageDown", 111: "Delete",
}

KEY_LABELS = {
    "Space": ("Espacio", "Space"), "Return": ("Enter", "Enter"),
    "Backspace": ("Retroceso", "Backspace"), "Left Shift": ("Mayús izq.", "Left Shift"),
    "Right Shift": ("Mayús der.", "Right Shift"), "Left Ctrl": ("Ctrl izq.", "Left Ctrl"),
    "Right Ctrl": ("Ctrl der.", "Right Ctrl"), "Left Alt": ("Alt", "Left Alt"),
    "Right Alt": ("Alt Gr", "Right Alt"), "Up": ("Flecha arriba", "Up arrow"),
    "Down": ("Flecha abajo", "Down arrow"), "Left": ("Flecha izquierda", "Left arrow"),
    "Right": ("Flecha derecha", "Right arrow"), "CapsLock": ("Bloq Mayús", "Caps Lock"),
    "Delete": ("Supr", "Delete"), "Home": ("Inicio", "Home"), "End": ("Fin", "End"),
    "PageUp": ("Re Pág", "Page Up"), "PageDown": ("Av Pág", "Page Down"),
    "`": ("Tecla a la izquierda del 1", "` key"), "NonUSBackslash": ("Tecla < >", "< > key"),
    "Keypad Enter": ("Enter (teclado num.)", "Keypad Enter"),
}


def key_name(hardware_keycode):
    """The SDL key name of a GTK hardware keycode (evdev + 8), None if it cannot be bound."""
    return EVDEV_KEYS.get(hardware_keycode - 8)


def key_label(name):
    if name in KEY_LABELS:
        return T(*KEY_LABELS[name])
    if name.startswith("Keypad "):
        return "Num " + name[7:]
    return name


def split_keys(value):
    return [key.strip() for key in (value or "").split(",") if key.strip()]


def bind_key(ini, input_id, name, replace):
    """Adds a key to an input (up to MAX_KEYS, the oldest goes); replace: the key replaces the
    default binding."""
    key = f"key.{input_id}"
    keys = [] if replace else split_keys(ini.get(key))
    if name not in keys:
        if len(keys) >= MAX_KEYS:
            keys.pop(0)
        keys.append(name)
    ini.set(key, ",".join(keys))


# --- patches -------------------------------------------------------------------------------------
# Every eboot patch for CUSA13795 01.21 in the patches folders' XML files (shadPS4/GoldHEN format).
# The community patches keep their bbport.ini keys (shared with the in-game menu); others are
# switched in patches.json ({"enabled": [...], "disabled": [...]}), as scripts/patches.py reads.

PATCH_INI_KEYS = {patch: key for key, patch in game_patches.GAME_PATCH_KEYS.items()}
# Settings > Internal resolution decides the 1080p Pro patch (as in the Windows launcher).
HIDDEN_PATCHES = {game_patches.GAME_PATCH_KEYS["patch_pro_1080p"]}


def list_patches(folders):
    """[dict(key, name, author, version, note, default_on, ini_key)] of the folders' patches."""
    found = []
    for folder in folders:
        for key, _path, meta in game_patches.external_patches(folder):
            if key in HIDDEN_PATCHES or any(p["key"] == key for p in found):
                continue
            found.append(dict(key=key, name=meta.get("Name", key), author=meta.get("Author", ""),
                              version=meta.get("PatchVer", ""), note=meta.get("Note", ""),
                              default_on=meta.get("isEnabled", "false").lower() == "true",
                              ini_key=PATCH_INI_KEYS.get(key)))
    return found


def load_patch_config(path):
    try:
        config = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        config = {}
    return {"enabled": list(config.get("enabled", [])), "disabled": list(config.get("disabled", []))}


def patch_on(patch, ini, config):
    if patch["ini_key"]:
        return ini.flag(patch["ini_key"])
    return patch["key"] in config["enabled"] or (patch["default_on"] and patch["key"] not in config["disabled"])


def set_patch(patch, on, ini, config):
    """Switches a patch: its bbport.ini key, or its place in patches.json's lists (config)."""
    if patch["ini_key"]:
        ini.set(patch["ini_key"], "1" if on else "0")
        return
    for listing in (config["enabled"], config["disabled"]):
        while patch["key"] in listing:
            listing.remove(patch["key"])
    if on and not patch["default_on"]:
        config["enabled"].append(patch["key"])
    elif not on and patch["default_on"]:
        config["disabled"].append(patch["key"])


def save_patch_config(path, config):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(config, indent=2) + "\n")
