#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""CTR Nitro-Fueled PC launcher for Linux (GTK4 / libadwaita): runs Crash Team Racing: Nitro-Fueled
natively.

The same tabs as the Windows launcher (launcher/windows/launcher.cpp), in Spanish or English: Play
(the game folder, preparing it from the base game and the 1.21 update, the log), Settings,
Keyboard, Patches, Saves, DLC and Tools. launcher/ctr_core.py does the work; this file draws it.
Launcher settings live in ~/.config/sacredfire-launcher/settings.json, the game's in the data
directory's bbport.ini (shared with the in-game menu); every change is saved at once. `--play`
starts the game with the saved settings without a window (Steam Deck game mode).
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

PORT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PORT_DIR / "scripts"))
import ctr_core as core  # noqa: E402
import game_check  # noqa: E402
import steam_shortcuts  # noqa: E402
from ctr_core import T  # noqa: E402

PACKAGED = bool(os.environ.get("BB_PREBUILT"))
DATA_DIR = Path(os.environ.get("BB_DATA_DIR", PORT_DIR))
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sacredfire-launcher"
CONFIG_FILE = CONFIG_DIR / "settings.json"
# The first bbport.ini, shared with run.sh and the Windows package (60 FPS limit, FSR 1, ...).
DEFAULT_INI = PORT_DIR / "packaging" / "bbport.default.ini"
MAX_LOG_LINES = 5000
TITLE = "CTR Nitro-Fueled PC"
APP_ID = "io.github.sacredfire.Launcher"
# The game's log names this when the known clobbered-dword bug shows itself (gpu/shim/
# bbport_clobber.cpp AskForReport); the log then asks for it to be attached to the issue.
ISSUE2_MARKER = "SacredFire issue #2"
ISSUE2_URL = "https://github.com/T3CHNOLOG1C/SacredFire/issues/2"

# Choices: (Spanish, English, value), as the Windows launcher's.
UI_LANGUAGES = [("Como el sistema", "System", ""), ("Español", "Español", "es"), ("English", "English", "en")]
# PS4 system language codes (BB_LANGUAGE) of the languages CUSA13795 (the Americas build) has.
GAME_LANGUAGES = [("Inglés", "English", "1"), ("Español latinoamericano", "Spanish (Latin America)", "20"),
                  ("Francés", "French", "2"), ("Portugués (Brasil)", "Portuguese (Brazil)", "17")]
PRESENT_MODES = [("Mailbox (recomendado)", "Mailbox (recommended)", "Mailbox"),
                 ("FIFO (VSync)", "FIFO (VSync)", "Fifo"), ("FIFO relajado", "FIFO Relaxed", "FifoRelaxed"),
                 ("Inmediato (sin VSync)", "Immediate (no VSync)", "Immediate")]
FPS_LIMITS = [("Sin límite", "No limit", "0")] + [(f"{v} FPS", f"{v} FPS", str(v)) for v in (30, 60, 72, 90, 120, 144, 240)]
# bbport.ini render_res: the scene size (scripts/patches.py render_plan, docs/sacredfire/13-resolution.md).
RENDER_RESOLUTIONS = [("480p (854 x 480, PCs muy modestos)", "480p (854 x 480, very low-end PCs)", "480"),
                      ("720p (1280 x 720, PCs modestos)", "720p (1280 x 720, low-end PCs)", "720"),
                      ("1080p (1920 x 1080)", "1080p (1920 x 1080)", "1080"),
                      ("1440p (2560 x 1440, PS4 Pro)", "1440p (2560 x 1440, PS4 Pro)", "1440"),
                      ("1620p (2880 x 1620)", "1620p (2880 x 1620)", "1620"),
                      ("4K (3840 x 2160)", "4K (3840 x 2160)", "2160")]
UPSCALERS = [("Desactivado", "Off", "off"), ("AMD FSR 1", "AMD FSR 1", "fsr1"),
             ("AMD FSR 3 (experimental)", "AMD FSR 3 (experimental)", "fsr3"),
             ("NVIDIA DLSS (experimental)", "NVIDIA DLSS (experimental)", "dlss")]
DLSS_QUALITIES = [("Automática (resolución interna)", "Auto (internal resolution)", "auto"),
                  ("DLAA (nativa)", "DLAA (native)", "dlaa"), ("Calidad", "Quality", "quality"),
                  ("Equilibrado", "Balanced", "balanced"), ("Rendimiento", "Performance", "performance"),
                  ("Ultra rendimiento", "Ultra Performance", "ultra_performance")]
DLSS_OUTPUTS = [("1920 x 1080", "1920 x 1080", "1080"), ("3840 x 2160 (4K)", "3840 x 2160 (4K)", "2160")]
DLSS_MODELS = [("Transformer (recomendado)", "Transformer (recommended)", "transformer"), ("CNN", "CNN", "cnn")]

DEFAULTS = {
    "ui_language": "",
    "game_dir": "" if PACKAGED else str(PORT_DIR / "orig" / "CUSA13795"),
    "base_dir": "",
    "update_dir": "",
    "user_dir": "",
    "language": "",
    "present_mode": "Mailbox",
    "display": "",
    "gamepad": "",
    "gamepad_name": "",
    "dlc": True,
    "dlc_disabled": "",
    "save_log": False,
    "frame_stats": False,
    "mangohud": False,
    "vk_validation": False,
    "extra_env": "",
}


def port_version():
    """The build's source revision: VERSION in the package, out/VERSION in a checkout."""
    for name in ("VERSION", "out/VERSION"):
        try:
            return (PORT_DIR / name).read_text().strip()
        except OSError:
            continue
    return ""


def load_settings():
    settings = dict(DEFAULTS)
    try:
        settings.update(json.loads(CONFIG_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(settings, indent=2, ensure_ascii=False))


def ini_path():
    return Path(os.environ.get("BB_CONFIG", DATA_DIR / "bbport.ini"))


def user_dir(settings):
    return Path(settings["user_dir"]).expanduser() if settings.get("user_dir") else DATA_DIR / "user"


def patches_config_path():
    return Path(os.environ.get("BB_PATCHES_CONFIG", DATA_DIR / "patches.json"))


def user_patches_dir():
    return DATA_DIR / "patches"


def dlc_dir():
    return Path(os.environ.get("BB_DLC_DIR", DATA_DIR / "dlc"))


def backup_dir():
    return DATA_DIR / "backups"


def game_language(settings):
    """The saved game language when this build has it, else the default for the launcher's language
    (older launchers saved codes this build lacks, such as 3 for Spain's Spanish)."""
    saved = settings.get("language", "")
    if saved in [value for _, _, value in GAME_LANGUAGES]:
        return saved
    if saved == "3":
        return "20"
    return "20" if core.spanish() else "1"


def game_environment(s):
    """Environment for run.sh from the launcher settings."""
    env = dict(os.environ)
    env["BB_GAME_DIR"] = str(Path(s["game_dir"]).expanduser())
    if s.get("user_dir"):
        env["BB_USER_DIR"] = s["user_dir"]
    env["BB_LANGUAGE"] = game_language(s)
    env["BB_PRESENT_MODE"] = s.get("present_mode") or "Mailbox"
    env["BB_DLC"] = "1" if s.get("dlc", True) else "0"
    for key, value in (("BB_DISPLAY", s.get("display")), ("BB_GAMEPAD", s.get("gamepad")),
                       ("BB_DLC_DISABLED", s.get("dlc_disabled"))):
        if value:
            env[key] = value
    for key, setting in (("BB_SAVE_LOG", "save_log"), ("BB_FRAME_STATS", "frame_stats"),
                         ("MANGOHUD", "mangohud"), ("BB_VK_VALIDATION", "vk_validation")):
        if s.get(setting):
            env[key] = "1"
    for item in s.get("extra_env", "").split():
        if "=" in item:
            key, value = item.split("=", 1)
            env[key] = value
    return env


def capabilities(option):
    """Lines of bb-gpu-capabilities `option`, split at tabs; [] if unknown."""
    tool = PORT_DIR / ("bin" if PACKAGED else "out") / "bb-gpu-capabilities"
    try:
        run = subprocess.run([str(tool), option], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.split("\t") for line in run.stdout.splitlines() if "\t" in line]


def connected_gamepads():
    """(GUID, name) of the connected gamepads."""
    return [(fields[0], "\t".join(fields[1:])) for fields in capabilities("--gamepads")]


def connected_displays():
    """(BB_DISPLAY value, label) of the monitors in SDL's order, numbered from 1."""
    displays = []
    for number, fields in enumerate(capabilities("--displays"), 1):
        label = f"{number}: {fields[0]}" + (f" ({fields[1]})" if len(fields) > 1 else "")
        if len(fields) > 2 and fields[2] == "1":
            label += T(" - principal", " - primary")
        displays.append((str(number), label))
    return displays


def game_icon_path():
    share = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return share / "icons/hicolor/512x512/apps" / f"{APP_ID}.png"


def install_game_icon(game_dir):
    """The game's own icon (sce_sys/icon0.png) as this app's icon, in the user's icon theme under
    the application id (launcher/install-desktop.sh does the same). Game files never enter the
    repository."""
    try:
        source = Path(game_dir).expanduser() / "sce_sys/icon0.png"
        if not source.is_file():
            return False
        target = game_icon_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.stat().st_size != source.stat().st_size:
            target.write_bytes(source.read_bytes())
        return True
    except OSError:
        return False


def open_folder(window, path):
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    Gtk.FileLauncher.new(Gio.File.new_for_path(str(path))).launch(window, None, None)


def labelled(choices):
    """(label, value) in the launcher's language from (Spanish, English, value)."""
    return [(T(es, en), value) for es, en, value in choices]


def combo_row(title, subtitle, choices, current, changed=None):
    """A list row over (label, value) choices; changed(value) when the player picks another."""
    row = Adw.ComboRow(title=title, subtitle=subtitle or "",
                       model=Gtk.StringList.new([label for label, _ in choices]))
    row.values = [value for _, value in choices]
    row.set_selected(row.values.index(current) if current in row.values else 0)
    if changed:
        row.connect("notify::selected", lambda r, _p: changed(r.values[r.get_selected()]))
    return row


def switch_row(title, subtitle, active, changed):
    row = Adw.SwitchRow(title=title, subtitle=subtitle or "", active=active)
    row.connect("notify::active", lambda r, _p: changed(r.get_active()))
    return row


def button(label, handler, *css):
    widget = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    for name in css:
        widget.add_css_class(name)
    widget.connect("clicked", lambda _b: handler())
    return widget


def icon_button(icon, tooltip, handler):
    widget = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, tooltip_text=tooltip)
    widget.add_css_class("flat")
    widget.connect("clicked", lambda _b: handler())
    return widget


def button_bar(*buttons):
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_top=12)
    for widget in buttons:
        box.append(widget)
    return box


def note(text, *css):
    label = Gtk.Label(label=text, wrap=True, xalign=0, margin_top=6)
    for name in css or ("dim-label",):
        label.add_css_class(name)
    return label


class LauncherWindow(Adw.ApplicationWindow):
    PAGES = ("play", "settings", "keyboard", "patches", "saves", "dlc", "tools")

    def __init__(self, app):
        super().__init__(application=app)
        self.settings = load_settings()
        core.set_language(self.settings.get("ui_language", ""))
        self.ini = core.Ini(ini_path(), DEFAULT_INI)
        self.patch_config = core.load_patch_config(patches_config_path())
        self.set_default_size(1000, 820)
        if install_game_icon(self.settings["game_dir"]):
            self.set_icon_name(APP_ID)
        self.process = None
        self.stream = None
        self.exit_text = ""
        self.last_status = ""
        self.merging = None  # (done, total) while preparing the game
        self.check = None    # the game folder's check: None running, "" fine, else the problem
        self.check_dir = None
        self.analyzed = {}   # folder -> ctr_core.analyze(), until a folder is picked or prepared
        self.merge_bar = None
        self.stopped = False
        self.capturing = -1  # the keyboard input waiting for a key
        self.capture_replace = False
        self.displays = connected_displays()
        self.gamepads = connected_gamepads()

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        header.set_title_widget(Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        toolbar.add_top_bar(header)
        self.toasts = Adw.ToastOverlay(child=self.stack)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)
        self.build_log_area()
        self.build_pages()
        self.stack.connect("notify::visible-child-name", lambda *_: self.cancel_capture())
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)
        self.connect("close-request", self.on_close)
        self.start_check()

    # --- pages ---------------------------------------------------------------------------

    def build_pages(self, names=PAGES):
        """(Re)builds the named tabs in place (after a setting that changes them, the launcher
        language, or the game exiting: the in-game menu may have changed bbport.ini)."""
        self.set_title(f"{TITLE} {port_version()}".rstrip())
        visible = self.stack.get_visible_child_name()
        # The stack keeps its pages in the order they were added: all come out, the named ones
        # are built again and the others go back as they were.
        kept = {}
        for name in self.PAGES:
            child = self.stack.get_child_by_name(name)
            if child:
                self.stack.remove(child)
                if name not in names:
                    kept[name] = child
        titles = {
            "play": (T("Jugar", "Play"), "media-playback-start-symbolic", self.build_play),
            "settings": (T("Ajustes", "Settings"), "preferences-system-symbolic", self.build_settings),
            "keyboard": (T("Teclado", "Keyboard"), "input-keyboard-symbolic", self.build_keyboard),
            "patches": (T("Parches", "Patches"), "applications-engineering-symbolic", self.build_patches),
            "saves": (T("Guardados", "Saves"), "document-save-symbolic", self.build_saves),
            "dlc": ("DLC", "package-x-generic-symbolic", self.build_dlc),
            "tools": (T("Herramientas", "Tools"), "applications-utilities-symbolic", self.build_tools),
        }
        for name in self.PAGES:
            title, icon, build = titles[name]
            self.stack.add_titled_with_icon(kept.get(name) or build(), name, title, icon)
        if visible:
            self.stack.set_visible_child_name(visible)

    def toast(self, text, timeout=5):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout))

    def save_ini(self):
        try:
            self.ini.save()
        except OSError as error:
            self.toast(T("No se pudo guardar bbport.ini: {}", "Could not save bbport.ini: {}").format(error))

    def ini_set(self, key, value, rebuild=()):
        self.ini.set(key, value)
        self.save_ini()
        if rebuild:
            GLib.idle_add(lambda: self.build_pages(rebuild) and False)

    def setting(self, key, value, rebuild=()):
        self.settings[key] = value
        save_settings(self.settings)
        if rebuild:
            GLib.idle_add(lambda: self.build_pages(rebuild) and False)

    def confirm(self, heading, body, action, callback):
        if hasattr(Adw, "AlertDialog"):
            dialog = Adw.AlertDialog(heading=heading, body=body)
        else:
            dialog = Adw.MessageDialog(transient_for=self, heading=heading, body=body)
        dialog.add_response("cancel", T("Cancelar", "Cancel"))
        dialog.add_response("ok", action)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, response: callback() if response == "ok" else None)
        if hasattr(Adw, "AlertDialog"):
            dialog.present(self)
        else:
            dialog.present()

    def choose_folder(self, title, initial, chosen):
        dialog = Gtk.FileDialog(title=title, modal=True)
        if initial and Path(initial).is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(initial)))

        def done(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return
            if folder:
                chosen(Path(folder.get_path()))
        dialog.select_folder(self, None, done)

    # --- Play --------------------------------------------------------------------------------

    def build_play(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18, margin_bottom=12,
                       margin_start=18, margin_end=18)
        self.game_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.append(self.game_box)
        self.play_button = Gtk.Button(height_request=64)
        self.play_button.add_css_class("pill")
        self.play_button.connect("clicked", lambda _b: self.stop_game() if self.process else self.start_game())
        page.append(self.play_button)
        self.status_label = Gtk.Label(wrap=True, xalign=0)
        page.append(self.status_label)
        self.last_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.last_label.add_css_class("dim-label")
        page.append(self.last_label)
        expander = Gtk.Expander(label=T("Registro", "Log"), expanded=True, vexpand=True)
        # The log outlives the tab: it moves from the previous Play tab's expander to this one.
        if getattr(self, "log_expander", None):
            self.log_expander.set_child(None)
        expander.set_child(self.log_area)
        self.log_expander = expander
        page.append(expander)
        self.update_play()
        return page

    def update_play(self):
        """The Play tab's game section and button, from the folders and the jobs."""
        if not hasattr(self, "game_box"):
            return
        while child := self.game_box.get_first_child():
            self.game_box.remove(child)
        s = self.settings
        busy = self.merging is not None
        game = self.analyze(s["game_dir"])
        group = Adw.PreferencesGroup(title=T("Juego", "Game"))
        self.merge_bar = None
        self.game_box.append(group)
        ready = game["kind"] == core.COMPLETE and not game["split"] and self.check == ""
        if game["kind"] == core.COMPLETE:
            path = str(Path(s["game_dir"]).expanduser())
            if busy:
                row = Adw.ActionRow(title=T("Preparando el juego...", "Preparing the game..."), subtitle=path)
            elif game["split"]:
                row = Adw.ActionRow(
                    title=T("Esta carpeta tiene el formato de PS4 PKG Tool (Image0 y Sc0). Hay que prepararla",
                            "This folder has the PS4 PKG Tool layout (Image0 and Sc0). It needs preparing"),
                    subtitle=T("Se crea una carpeta nueva con enlaces duros, sin tocar la tuya: ",
                               "A new folder of hard links is created, yours is not changed: ")
                    + str(core.merge_target(path, self.taken_folders())))
                row.add_suffix(button(T("Preparar el juego", "Prepare the game"),
                                      lambda: self.start_merge([path]), "suggested-action"))
            elif self.check is None:
                row = Adw.ActionRow(title=T("Comprobando el juego...", "Checking the game..."), subtitle=path)
            elif self.check == "":
                row = Adw.ActionRow(title=T("Listo: ", "Ready: ") + core.describe(game), subtitle=path)
                row.add_prefix(Gtk.Image(icon_name="object-select-symbolic", css_classes=["success"]))
            else:
                row = Adw.ActionRow(title=T("Estos archivos no sirven:", "These files do not work:"),
                                    subtitle=f"{self.check}\n{path}")
                row.add_prefix(Gtk.Image(icon_name="dialog-error-symbolic", css_classes=["error"]))
            if not busy and not self.process:
                row.add_suffix(button(T("Cambiar carpeta...", "Change folder..."), lambda: self.pick("game")))
            group.add(row)
        else:
            problems = {core.UPDATE: T("solo tiene la actualización 1.21, falta el juego base",
                                       "only has the 1.21 update, the base game is missing"),
                        core.BASE: T("es el juego base sin la actualización 1.21", "is the base game without the 1.21 update")}
            if game["kind"] != core.EMPTY:
                row = Adw.ActionRow(title=problems.get(game["kind"], core.describe(game)), subtitle=s["game_dir"])
                row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["warning"]))
                group.add(row)
            group.set_description(T("Necesitas tu propio dump de PS4: el juego base (CUSA13795) y la actualización 1.21 "
                                    "(CUSA13795-patch). Si ya los tienes juntos en una carpeta, elige esa.",
                                    "You need your own PS4 dump: the base game (CUSA13795) and the 1.21 update "
                                    "(CUSA13795-patch). If they are already merged in one folder, choose that one."))
            base, update = self.analyze(s["base_dir"]), self.analyze(s["update_dir"])
            for title, info, good, slot in ((T("Juego base", "Base game"), base, core.BASE, "base"),
                                            (T("Actualización 1.21", "Update 1.21"), update, core.UPDATE, "update")):
                row = Adw.ActionRow(title=title, subtitle=core.describe(info) + (f"\n{s[slot + '_dir']}" if s[slot + "_dir"] else ""))
                icon, css = (("object-select-symbolic", "success") if info["kind"] == good else
                             ("radio-symbolic", "dim-label") if info["kind"] == core.EMPTY else ("dialog-warning-symbolic", "warning"))
                row.add_prefix(Gtk.Image(icon_name=icon, css_classes=[css]))
                choose = button(T("Elegir...", "Choose..."), lambda slot=slot: self.pick(slot))
                choose.set_sensitive(not busy)
                row.add_suffix(choose)
                group.add(row)
            can_merge = base["kind"] == core.BASE and update["kind"] == core.UPDATE
            if not busy:
                prepare = Adw.ActionRow(
                    title=T("Preparar el juego (base + 1.21)", "Prepare the game (base + 1.21)"),
                    subtitle=T("Une la actualización con el juego base en una carpeta nueva usando enlaces duros: no ocupa "
                               "espacio extra ni cambia tus carpetas originales (si están en discos distintos, se copia).",
                               "Merges the update over the base game into a new folder with hard links: no extra disk "
                               "space and your original folders are not changed (copied if they are on different drives).")
                    + (T("\nSe crea ", "\nCreates ") + str(core.merge_target(s["base_dir"], self.taken_folders()))
                       if can_merge else ""))
                go = button(T("Preparar", "Prepare"), lambda: self.start_merge([s["base_dir"], s["update_dir"]]),
                            "suggested-action")
                go.set_sensitive(can_merge)
                prepare.add_suffix(go)
                group.add(prepare)
            merged = Adw.ActionRow(title=T("Ya tengo el juego preparado", "I already have the merged game"))
            merged.add_suffix(button(T("Elegir...", "Choose..."), lambda: self.pick("game")))
            merged.set_sensitive(not busy)
            group.add(merged)
        if busy:
            done, total = self.merging
            bar = Gtk.ProgressBar(show_text=True, text=f"{done} / {total}", fraction=done / total if total else 0,
                                  margin_top=8)
            group.add(bar)
            self.merge_bar = bar

        running = self.process is not None
        self.play_button.set_label(T("Cerrar el juego", "Stop the game") if running else T("JUGAR", "PLAY"))
        for css in ("suggested-action", "destructive-action", "title-2"):
            self.play_button.remove_css_class(css)
        self.play_button.add_css_class("destructive-action" if running else "suggested-action")
        if not running:
            self.play_button.add_css_class("title-2")
        self.play_button.set_sensitive(running or (ready and not busy))
        if running:
            self.status_label.set_label(T("El juego está abierto. El primer arranque compila shaders y tarda un poco.",
                                          "The game is running. The first start compiles shaders and takes a while."))
        else:
            self.status_label.set_label(self.exit_text)
        self.last_label.set_label(self.last_status)

    def analyze(self, folder):
        """ctr_core.analyze() once per folder (it adds up a whole game's size)."""
        if folder not in self.analyzed:
            self.analyzed[folder] = core.analyze(folder)
        return self.analyzed[folder]

    def taken_folders(self):
        s = self.settings
        return [Path(s[key]).expanduser() for key in ("game_dir", "base_dir", "update_dir") if s.get(key)]

    def pick(self, slot):
        """A folder for the game, the base game or the update; whatever was picked goes where it
        belongs (a prepared game becomes the game folder, a split one is prepared)."""
        s = self.settings
        start = s.get(f"{slot}_dir") or s.get("base_dir") or s.get("game_dir")
        titles = {"game": "CUSA13795-1.21", "base": "CUSA13795", "update": "CUSA13795-patch"}

        def chosen(path):
            path = core.game_root(path)
            self.analyzed.clear()
            found = self.analyze(str(path))
            if found["kind"] == core.COMPLETE:
                s["game_dir"] = str(path)
                save_settings(s)
                if install_game_icon(path):
                    self.set_icon_name(APP_ID)
                if found["split"]:
                    self.start_merge([str(path)])
                else:
                    self.start_check()
                return
            key = {core.BASE: "base_dir", core.UPDATE: "update_dir"}.get(found["kind"], f"{slot}_dir")
            s[key] = str(path)
            save_settings(s)
            if key == "game_dir":
                self.start_check()
            self.update_play()
        self.choose_folder(titles[slot], Path(start).expanduser().parent if start else None, chosen)

    def start_check(self):
        """The game folder's check (scripts/game_check.py: the executable's image hash) in the
        background."""
        game = self.analyze(self.settings["game_dir"])
        path = Path(self.settings["game_dir"]).expanduser()
        self.check_dir = path
        if game["kind"] != core.COMPLETE or game["split"]:
            self.check = None
            self.update_play()
            return
        self.check = None
        self.update_play()

        def work():
            problem = game_check.problem(path)
            result = game_check.explain(*problem) if problem else ""
            GLib.idle_add(finish, result)

        def finish(result):
            if self.check_dir == path:
                self.check = result
                self.update_play()
            return False
        threading.Thread(target=work, daemon=True).start()

    def start_merge(self, sources):
        """The base game + update (or one PS4 PKG Tool dump) -> a new folder of hard links."""
        if self.merging is not None:
            return
        sources = [Path(p).expanduser() for p in sources]
        target = core.merge_target(sources[0], self.taken_folders())
        self.merging = (0, 0)
        self.update_play()
        last = [0.0]

        def progress(done, total):
            now = time.monotonic()
            if done == total or now - last[0] > 0.1:
                last[0] = now
                GLib.idle_add(step, done, total)

        def step(done, total):
            self.merging = (done, total)
            if self.merge_bar:
                self.merge_bar.set_fraction(done / total if total else 0)
                self.merge_bar.set_text(f"{done} / {total}")
            else:
                self.update_play()
            return False

        def work():
            try:
                files, copied = core.merge(sources, target, progress)
                GLib.idle_add(finish, None, files, copied)
            except OSError as error:
                GLib.idle_add(finish, error, 0, 0)

        def finish(error, files, copied):
            self.merging = None
            self.analyzed.clear()
            if error:
                self.toast(T("No se pudo preparar el juego: {}", "Could not prepare the game: {}").format(error), 10)
            else:
                self.settings["game_dir"] = str(target)
                save_settings(self.settings)
                if install_game_icon(target):
                    self.set_icon_name(APP_ID)
                self.toast(T("{} archivos unidos en {}", "Merged {} files into {}").format(files, target)
                           + (T(" ({} copiados: otro disco)", " ({} copied: different drive)").format(copied) if copied else ""), 8)
            self.start_check()
            return False
        threading.Thread(target=work, daemon=True).start()

    # --- game process ------------------------------------------------------------------------

    def start_game(self):
        if self.process:
            return
        self.save_ini()
        save_settings(self.settings)
        self.log_view.get_buffer().set_text("")
        self.issue_notice.set_visible(False)
        self.exit_text = self.last_status = ""
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        launcher.set_environ([f"{k}={v}" for k, v in game_environment(self.settings).items()])
        launcher.set_cwd(str(PORT_DIR))
        try:
            self.process = launcher.spawnv(["setsid", "bash", str(PORT_DIR / "run.sh")])
        except GLib.Error as error:
            self.toast(T("No se pudo iniciar: {}", "Could not start: {}").format(error.message))
            return
        self.stream = Gio.DataInputStream.new(self.process.get_stdout_pipe())
        self.read_line()
        self.process.wait_async(None, self.on_exit)
        self.append_log(T("Iniciando el juego...", "Starting the game...") + "\n")
        self.build_pages(("saves", "dlc"))  # locked while the game runs
        self.update_play()

    def read_line(self):
        self.stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.on_line)

    def on_line(self, stream, result):
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error:
            return
        if line is None:
            return
        self.append_log(line + "\n")
        if line.strip():
            self.last_status = line.strip()
            self.last_label.set_label(self.last_status)
        if ISSUE2_MARKER in line and not self.issue_notice.get_visible():
            self.issue_notice.set_visible(True)
            self.toast(T("Apareció el fallo conocido (issue #2): exporta el registro y adjúntalo al issue",
                         "Known bug (issue #2) hit: please export the log and attach it to the issue"), 10)
        self.read_line()

    def stop_game(self):
        if not self.process:
            return
        self.stopped = True
        pid = int(self.process.get_identifier())
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            self.process.force_exit()
        GLib.timeout_add_seconds(3, self.kill_if_running, pid)

    def kill_if_running(self, pid):
        if self.process:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        return False

    def on_exit(self, process, result):
        try:
            process.wait_finish(result)
        except GLib.Error:
            pass
        status = process.get_exit_status() if process.get_if_exited() else -1
        stopped = getattr(self, "stopped", False)
        self.process = None
        self.stopped = False
        if status == 0 or stopped:
            self.exit_text = T("El juego se cerró.", "The game was closed.")
        else:
            self.exit_text = T("El juego se cerró con un error (código {}). Mira el registro abajo.",
                               "The game ended with an error (code {}). See the log below.").format(status)
        self.append_log(T("\n— el juego terminó (código {}) —\n", "\n— the game exited (code {}) —\n").format(status))
        # The in-game menu may have changed the settings.
        self.ini = core.Ini(ini_path(), DEFAULT_INI)
        self.build_pages(("settings", "keyboard", "patches", "saves", "dlc"))
        self.update_play()
        self.present()

    def on_close(self, _window):
        if not self.process:
            return False
        self.confirm(T("El juego sigue abierto.", "The game is still running."), "",
                     T("Cerrar el juego y el launcher", "Stop the game and quit"),
                     lambda: (self.stop_game(), self.get_application().quit()))
        return True

    # --- log -----------------------------------------------------------------------------------

    def build_log_area(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.issue_notice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_bottom=6, visible=False)
        self.issue_notice.add_css_class("card")
        self.issue_notice.append(Gtk.Label(
            label=T("El juego tuvo el fallo conocido \"clobbered command buffer\" (issue #2). Exporta este registro y "
                    "adjúntalo al issue para que se pueda corregir.",
                    "The game hit the known \"clobbered command buffer\" bug (issue #2). Please export this log and "
                    "attach it to the issue so the cause can be fixed."),
            wrap=True, xalign=0, hexpand=True, margin_top=6, margin_bottom=6, margin_start=6))
        self.issue_notice.append(button(T("Abrir el issue #2", "Open issue #2"),
                                        lambda: Gio.AppInfo.launch_default_for_uri(ISSUE2_URL, None)))
        box.append(self.issue_notice)
        self.log_view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, left_margin=8, right_margin=8)
        self.log_scroller = Gtk.ScrolledWindow(vexpand=True, min_content_height=160, child=self.log_view)
        self.log_scroller.add_css_class("card")
        box.append(self.log_scroller)
        self.export_button = button(T("Exportar registro…", "Export log…"), self.on_export_log)
        self.copy_button = button(T("Copiar", "Copy"), self.on_copy_log)
        box.append(button_bar(self.export_button, self.copy_button))
        buffer = self.log_view.get_buffer()
        buffer.connect("changed", lambda b: [w.set_sensitive(b.get_char_count() > 0)
                                             for w in (self.export_button, self.copy_button)])
        self.export_button.set_sensitive(False)
        self.copy_button.set_sensitive(False)
        self.log_area = box

    def append_log(self, text):
        buffer = self.log_view.get_buffer()
        buffer.insert(buffer.get_end_iter(), text)
        extra = buffer.get_line_count() - MAX_LOG_LINES
        if extra > 0:
            buffer.delete(buffer.get_start_iter(), buffer.get_iter_at_line(extra)[1])
        adj = self.log_scroller.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper()) and False)

    def log_text(self):
        buffer = self.log_view.get_buffer()
        return buffer.get_text(*buffer.get_bounds(), False)

    def on_copy_log(self):
        self.get_clipboard().set(self.log_text())
        self.toast(T("Registro copiado al portapapeles", "Log copied to the clipboard"))

    def on_export_log(self):
        text = self.log_text()
        dialog = Gtk.FileDialog(title=T("Exportar registro", "Export log"))
        dialog.set_initial_name(f"ctr-nitro-fueled-{time.strftime('%Y%m%d_%H%M%S')}.log")

        def finish(dialog, result):
            try:
                chosen = dialog.save_finish(result)
            except GLib.Error:
                return
            try:
                Path(chosen.get_path()).write_text(text, encoding="utf-8")
                self.toast(T("Registro guardado en {}", "Log saved to {}").format(chosen.get_path()))
            except OSError as error:
                self.toast(T("No se pudo guardar el registro: {}", "Could not save the log: {}").format(error))
        dialog.save(self, None, finish)

    # --- Settings ------------------------------------------------------------------------------

    def build_settings(self):
        page = Adw.PreferencesPage()
        ini = self.ini
        performance = Adw.PreferencesGroup(
            title=T("Rendimiento", "Performance"),
            description=T("Se aplican al pulsar Jugar. El menú del juego (Insert o L3+R3) cambia los mismos ajustes.",
                          "Applied when you press Play. The in-game menu (Insert or L3+R3) changes the same settings."))
        performance.add(combo_row(
            T("Límite de FPS", "FPS limit"),
            T("Con el parche de 60 FPS el juego va tan rápido como permita tu PC. En una pantalla de 144 Hz sin "
              "G-Sync/FreeSync, 72 reparte los fotogramas de forma pareja.",
              "With the 60 FPS patch the game runs as fast as your PC allows. On a 144 Hz screen without "
              "G-Sync/FreeSync, 72 divides the refresh evenly."),
            labelled(FPS_LIMITS), ini.get("fps_limit", "60"), lambda v: self.ini_set("fps_limit", v)))
        performance.add(switch_row(
            T("Compilar shaders en segundo plano (experimental)", "Compile shaders in the background (experimental)"),
            T("Menos tirones la primera vez que aparece un efecto; a veces un efecto tarda un fotograma en verse.",
              "Fewer hitches when an effect is first seen; an effect may show up a frame late."),
            ini.flag("async_pipelines"), lambda on: self.ini_set("async_pipelines", "1" if on else "0")))
        page.add(performance)

        display = Adw.PreferencesGroup(title=T("Pantalla", "Display"))
        temporal = ini.get("temporal")
        if temporal not in [v for _, _, v in UPSCALERS]:  # v0.3 test builds: fsr3=1, fsr1=0
            temporal = "fsr3" if ini.flag("fsr3") else ("fsr1" if ini.flag("fsr1", True) else "off")
        dlss_sized = temporal == "dlss" and ini.get("dlss_quality", "auto") != "auto"
        render_res = ini.get("render_res", "1080")
        resolution = combo_row(
            T("Resolución interna", "Internal resolution"),
            (T("(la decide la calidad de DLSS) ", "(set by the DLSS quality) ") if dlss_sized else "")
            + T("El tamaño al que el juego dibuja la escena 3D. Por encima de 1080p usa el modo PS4 Pro del juego y la "
                "imagen sale en 4K: en una pantalla 1080p eso quita los dientes de sierra. 4K necesita una GPU potente; "
                "720p y 480p alivian PCs modestos (la imagen sale a 1080p).",
                "The size the game draws its 3D scene at. Above 1080p it uses the game's PS4 Pro path (turned on "
                "automatically) and the picture is output at 4K: on a 1080p screen that removes jagged edges. 4K needs "
                "a strong GPU; 720p and 480p help low-end PCs (the picture is output at 1080p)."),
            labelled(RENDER_RESOLUTIONS), render_res if render_res in [v for _, _, v in RENDER_RESOLUTIONS] else "1080",
            lambda v: self.ini_set("render_res", v))
        resolution.set_sensitive(not dlss_sized)
        display.add(resolution)

        def upscaler(value):
            self.ini.erase("fsr1")
            self.ini.erase("fsr3")
            self.ini_set("temporal", value, rebuild=("settings",))
        display.add(combo_row(
            T("Escalado", "Upscaling"),
            T("FSR 1 agranda una escena más pequeña con bordes más nítidos que el estirado simple del juego. FSR 3 y DLSS "
              "reconstruyen el detalle a lo largo de varios cuadros con la profundidad y el movimiento del juego, y a la "
              "misma resolución también suavizan los bordes. DLSS necesita una tarjeta NVIDIA RTX (si no, se usa FSR 3). "
              "Experimentales: si ves estelas o parpadeos, cambia a FSR 1.",
              "FSR 1 enlarges a smaller scene with sharper edges than the game's plain stretch. FSR 3 and DLSS rebuild "
              "detail over several frames from the game's depth and motion, and smooth edges even at the same "
              "resolution. DLSS needs an NVIDIA RTX card (FSR 3 runs otherwise). Experimental: switch to FSR 1 if you "
              "see trails or shimmering."),
            labelled(UPSCALERS), temporal, upscaler))
        if temporal == "dlss":
            display.add(combo_row(
                T("Calidad de DLSS", "DLSS quality"),
                T("Automática usa la resolución interna de arriba. Las demás dibujan la escena a la proporción de NVIDIA "
                  "respecto a la salida de DLSS: Calidad 67 %, Equilibrado 58 %, Rendimiento 50 %, Ultra rendimiento "
                  "33 %, DLAA 100 %.",
                  "Auto uses the internal resolution above. The others draw the scene at NVIDIA's share of the DLSS "
                  "output: Quality 67%, Balanced 58%, Performance 50%, Ultra Performance 33%, DLAA 100%."),
                labelled(DLSS_QUALITIES), ini.get("dlss_quality", "auto"),
                lambda v: self.ini_set("dlss_quality", v, rebuild=("settings",))))
            if dlss_sized:
                display.add(combo_row(T("Salida de DLSS", "DLSS output"), None, labelled(DLSS_OUTPUTS),
                                      ini.get("dlss_output", "1080"), lambda v: self.ini_set("dlss_output", v)))
            display.add(combo_row(
                T("Modelo de DLSS", "DLSS model"),
                T("Transformer: el modelo actual de NVIDIA (DLSS 4), más nítido y estable, algo más pesado. CNN: el "
                  "modelo anterior (preset E), más ligero; NVIDIA lo marca como obsoleto.",
                  "Transformer: NVIDIA's current model (DLSS 4), sharper and steadier, a little heavier. CNN: the "
                  "older model (preset E), lighter; NVIDIA marks it deprecated."),
                labelled(DLSS_MODELS), ini.get("dlss_model", "transformer"), lambda v: self.ini_set("dlss_model", v)))
        if temporal == "fsr3":
            try:
                sharpness = float(ini.get("fsr3_sharpness", "0.30"))
            except ValueError:
                sharpness = 0.30
            spin = Adw.SpinRow.new_with_range(0.0, 1.0, 0.05)
            spin.set_title(T("Nitidez de FSR 3", "FSR 3 sharpness"))
            spin.set_digits(2)
            spin.set_value(sharpness)
            spin.connect("notify::value", lambda r, _p: self.ini_set("fsr3_sharpness", f"{r.get_value():.2f}"))
            display.add(spin)
        display.add(switch_row(T("Pantalla completa", "Fullscreen"), None, ini.flag("fullscreen"),
                               lambda on: self.ini_set("fullscreen", "1" if on else "0")))
        display.add(switch_row(T("Contador de FPS en la esquina", "FPS counter in the corner"), None,
                               ini.flag("show_fps"), lambda on: self.ini_set("show_fps", "1" if on else "0")))
        displays = [(T("Principal", "Primary"), "")] + [(label, value) for value, label in self.displays]
        if self.settings["display"] and self.settings["display"] not in [v for _, v in displays]:
            displays.append((T("{} (no conectado)", "{} (not connected)").format(self.settings["display"]),
                             self.settings["display"]))
        display.add(combo_row(T("Monitor", "Monitor"), None, displays, self.settings["display"],
                              lambda v: self.setting("display", v)))
        display.add(combo_row(T("Presentación", "Presentation"), None, labelled(PRESENT_MODES),
                              self.settings["present_mode"], lambda v: self.setting("present_mode", v)))
        page.add(display)

        controller = Adw.PreferencesGroup(
            title=T("Mando", "Controller"),
            description=T("El teclado funciona siempre a la vez. La pantalla dividida se asigna en el menú del juego.",
                          "The keyboard always works too. Split-screen ports are assigned in the in-game menu."))
        current = self.settings["gamepad"]
        pads = [(T("El primero conectado", "First connected"), "")] + [(name, guid) for guid, name in self.gamepads]
        if current and current not in [g for g, _ in self.gamepads]:
            pads.append(((self.settings["gamepad_name"] or current) + T(" (no conectado)", " (not connected)"), current))

        def pad(value):
            self.settings["gamepad_name"] = dict((g, n) for g, n in self.gamepads).get(value, self.settings["gamepad_name"]) if value else ""
            self.setting("gamepad", value)
        pad_row = combo_row(T("Mando", "Controller"), None, pads, current, pad)
        pad_row.add_suffix(icon_button("view-refresh-symbolic", T("Buscar mandos", "Refresh"), self.refresh_devices))
        controller.add(pad_row)
        page.add(controller)

        console = Adw.PreferencesGroup(title=T("Consola", "Console"))
        console.add(combo_row(
            T("Idioma del juego", "Game language"),
            T("Los idiomas que trae esta versión del juego (CUSA13795, América): inglés, español latinoamericano, "
              "francés y portugués de Brasil. El español de España no está en ella.",
              "The languages this build of the game has (CUSA13795, the Americas): English, Latin American Spanish, "
              "French and Brazilian Portuguese. Spain's Spanish is not in it."),
            labelled(GAME_LANGUAGES), game_language(self.settings), lambda v: self.setting("language", v)))
        page.add(console)
        return page

    def refresh_devices(self):
        self.displays = connected_displays()
        self.gamepads = connected_gamepads()
        self.build_pages(("settings",))
        self.toast(T("Mandos encontrados: {}", "Controllers found: {}").format(len(self.gamepads)))

    # --- Keyboard ------------------------------------------------------------------------------

    def build_keyboard(self):
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title=T("Controles", "Controls"),
            description=T("Teclas del jugador 1. El teclado funciona a la vez que el mando. Pulsa \"Cambiar\" y luego la "
                          "tecla; cada acción admite hasta 4 teclas. Se guarda en bbport.ini y vale desde el próximo "
                          "arranque. Insert abre el menú del juego y Esc cancela, por eso no se pueden asignar.",
                          "Player 1's keys. The keyboard works together with the controller. Press \"Change\", then the "
                          "key; each action takes up to 4 keys. Saved in bbport.ini, used from the next start. Insert "
                          "opens the in-game menu and Esc cancels, so they cannot be bound."))
        self.key_rows = []
        for index, (input_id, es, en, _fallback) in enumerate(core.KEY_INPUTS):
            row = Adw.ActionRow(title=T(es, en))
            change = button(T("Cambiar", "Change"), lambda i=index: self.begin_capture(i))
            clear = button(T("Quitar", "Clear"), lambda i=input_id: self.ini_set(f"key.{i}", "", rebuild=("keyboard",)))
            default = button(T("Original", "Default"), lambda i=input_id: self.reset_key(i))
            for widget in (change, clear, default):
                widget.add_css_class("flat")
                row.add_suffix(widget)
            row.buttons = (change, clear, default)
            group.add(row)
            self.key_rows.append(row)
        page.add(group)
        footer = Adw.PreferencesGroup()
        restore = Adw.ActionRow(title=T("Restaurar todas las teclas", "Restore all keys"),
                                subtitle=T("Gris: tecla original. Las teclas van por su posición en el teclado.",
                                           "Grey: default key. Keys go by their place on the keyboard."))
        restore.add_suffix(button(T("Restaurar", "Restore"), self.reset_all_keys))
        footer.add(restore)
        page.add(footer)
        self.update_key_rows()
        return page

    def update_key_rows(self):
        for index, row in enumerate(getattr(self, "key_rows", [])):
            input_id, _es, _en, fallback = core.KEY_INPUTS[index]
            custom = self.ini.has(f"key.{input_id}")
            keys = core.split_keys(self.ini.get(f"key.{input_id}") if custom else fallback)
            if self.capturing == index:
                text = T("Pulsa una tecla... (Esc cancela)", "Press a key... (Esc cancels)")
            elif not keys:
                text = T("(sin tecla)", "(none)")
            else:
                text = "  /  ".join(core.key_label(k) for k in keys)
            row.set_subtitle(GLib.markup_escape_text(text))
            if custom or self.capturing == index:
                row.remove_css_class("dim-label")
            else:
                row.add_css_class("dim-label")
            change, clear, default = row.buttons
            for widget in (change, clear):
                widget.set_sensitive(self.capturing < 0)
            default.set_sensitive(self.capturing < 0 and custom)

    def begin_capture(self, index):
        # A key replaces the default binding; on a custom one it is added (up to 4).
        self.capture_replace = not self.ini.has(f"key.{core.KEY_INPUTS[index][0]}")
        self.capturing = index
        self.update_key_rows()

    def cancel_capture(self):
        if self.capturing >= 0:
            self.capturing = -1
            self.update_key_rows()

    def on_key(self, _controller, _keyval, keycode, _state):
        if self.capturing < 0:
            return False
        if keycode - 8 == core.ESCAPE:
            self.cancel_capture()
            return True
        name = core.key_name(keycode)
        if name:
            core.bind_key(self.ini, core.KEY_INPUTS[self.capturing][0], name, self.capture_replace)
            self.save_ini()
        else:
            self.toast(T("Esa tecla no se puede asignar.", "That key cannot be bound."))
        self.capturing = -1
        self.update_key_rows()
        return True

    def reset_key(self, input_id):
        self.ini.erase(f"key.{input_id}")
        self.save_ini()
        self.update_key_rows()

    def reset_all_keys(self):
        for input_id, *_ in core.KEY_INPUTS:
            self.ini.erase(f"key.{input_id}")
        self.save_ini()
        self.update_key_rows()

    # --- Patches -------------------------------------------------------------------------------

    def build_patches(self):
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title=T("Parches", "Patches"),
            description=T("Se aplican al iniciar el juego (o con \"Apply and restart\" del menú del juego).",
                          "Applied when the game starts (or with the in-game menu's \"Apply and restart\")."))
        folders = [Path(os.environ.get("BB_PATCHES_DIR", PORT_DIR / "patches")), user_patches_dir()]
        patches = core.list_patches(folders)
        if not patches:
            group.add(Adw.ActionRow(title=T("No hay parches en la carpeta patches.", "No patches in the patches folder.")))
        for patch in patches:
            byline = " ".join(part for part in (
                T("de {}", "by {}").format(patch["author"]) if patch["author"] else "",
                f"v{patch['version']}" if patch["version"] else "") if part)
            subtitle = "\n".join(part for part in (byline, patch["note"], patch["key"]) if part)
            group.add(switch_row(GLib.markup_escape_text(patch["name"]), GLib.markup_escape_text(subtitle),
                                 core.patch_on(patch, self.ini, self.patch_config),
                                 lambda on, p=patch: self.switch_patch(p, on)))
        page.add(group)
        add = Adw.PreferencesGroup(
            title=T("Añadir parches", "Add patches"),
            description=T("Copia archivos .xml de parches en formato shadPS4 (como los de PS-Game-Patch o GoldHEN) en la "
                          "carpeta de parches y pulsa Recargar. Solo aparecen los que son para CUSA13795 versión 01.21. "
                          "Por ahora no existen trucos públicos para CTR Nitro-Fueled (como desbloquear todos los "
                          "personajes). Para tener el juego desbloqueado puedes importar una partida en la pestaña "
                          "Guardados.",
                          "Copy patch .xml files in shadPS4 format (like PS-Game-Patch's or GoldHEN's) into the patches "
                          "folder and press Reload. Only those for CUSA13795 version 01.21 are listed. There are no "
                          "public cheats for CTR Nitro-Fueled yet (like unlocking every character). To have the game "
                          "unlocked you can import a save in the Saves tab."))
        add.add(button_bar(button(T("Abrir la carpeta de parches", "Open the patches folder"),
                                  lambda: open_folder(self, user_patches_dir())),
                           button(T("Recargar", "Reload"), self.reload_patches)))
        page.add(add)
        return page

    def switch_patch(self, patch, on):
        core.set_patch(patch, on, self.ini, self.patch_config)
        if patch["ini_key"]:
            self.save_ini()
            return
        try:
            core.save_patch_config(patches_config_path(), self.patch_config)
        except OSError as error:
            self.toast(T("No se pudo guardar {}: {}", "Could not save {}: {}").format(patches_config_path(), error))

    def reload_patches(self):
        self.patch_config = core.load_patch_config(patches_config_path())
        self.build_pages(("patches",))

    # --- Saves ---------------------------------------------------------------------------------

    def build_saves(self):
        page = Adw.PreferencesPage()
        locked = self.process is not None
        root = core.save_root(user_dir(self.settings))
        saves = core.list_saves(root)
        group = Adw.PreferencesGroup(title=T("Partidas", "Saves"), description=str(root))
        if locked:
            group.set_description(T("Cierra el juego para copiar, importar o restaurar partidas.",
                                    "Close the game to back up, import or restore saves.") + f"\n{root}")
        if not saves:
            group.add(Adw.ActionRow(title=T("Todavía no hay partidas guardadas.", "No saves yet.")))
        for slot in saves:
            text = " - ".join(part for part in (slot["title"], slot["subtitle"], slot["detail"]) if part) or "-"
            group.add(Adw.ActionRow(title=GLib.markup_escape_text(text),
                                    subtitle=GLib.markup_escape_text(
                                        f"{slot['dir']} · {core.format_bytes(slot['bytes'])} · {slot['modified']}")))
        backup = button(T("Hacer copia de seguridad", "Back up now"), lambda: self.backup_now(root))
        export = button(T("Exportar...", "Export..."), lambda: self.export_saves(root))
        imported = button(T("Importar...", "Import..."), lambda: self.import_saves(root))
        for widget in (backup, export):
            widget.set_sensitive(bool(saves) and not locked)
        imported.set_sensitive(not locked)
        imported.set_tooltip_text(T(
            "Acepta una copia de seguridad de este launcher, la carpeta CUSA13795 de las partidas de otro PC, o una "
            "partida de PS4 descifrada (Apollo Save Tool, Save Wizard: una carpeta con sce_sys/param.sfo). Antes de "
            "importar se hace una copia de seguridad de tus partidas.",
            "Takes a backup made by this launcher, the CUSA13795 saves folder from another PC, or a decrypted PS4 "
            "save (Apollo Save Tool, Save Wizard: a folder with sce_sys/param.sfo). Your saves are backed up "
            "before importing."))
        group.add(button_bar(backup, export, imported,
                             button(T("Abrir carpeta", "Open folder"), lambda: open_folder(self, root))))
        page.add(group)

        backups = Adw.PreferencesGroup(title=T("Copias de seguridad", "Backups"), description=str(backup_dir()))
        found = core.list_backups(backup_dir())
        if not found:
            backups.add(Adw.ActionRow(title=T("Todavía no hay copias.", "No backups yet.")))
        for item in found:
            row = Adw.ActionRow(title=item["name"], subtitle=f"{core.format_bytes(item['bytes'])} · {item['modified']}")
            restore = button(T("Restaurar", "Restore"), lambda b=item: self.confirm(
                T("¿Restaurar {}?", "Restore {}?").format(b["name"]),
                T("Tus partidas actuales se guardan antes en una copia automática.",
                  "Your current saves are backed up automatically first."),
                T("Restaurar", "Restore"), lambda: self.restore_backup(b["path"], root)))
            restore.set_sensitive(not locked)
            delete = button(T("Borrar", "Delete"), lambda b=item: self.confirm(
                T("¿Borrar la copia {}?", "Delete the backup {}?").format(b["name"]),
                T("No se puede deshacer.", "This cannot be undone."),
                T("Borrar", "Delete"), lambda: self.remove_path(b["path"], ("saves",))))
            for widget in (restore, delete):
                widget.add_css_class("flat")
                row.add_suffix(widget)
            backups.add(row)
        backups.add(button_bar(button(T("Abrir carpeta de copias", "Open backups folder"),
                                      lambda: open_folder(self, backup_dir())),
                               button(T("Actualizar", "Refresh"), lambda: self.build_pages(("saves",)))))
        page.add(backups)
        return page

    def backup_now(self, root):
        try:
            made = core.backup_saves(root, backup_dir())
            self.toast(T("Copia creada: ", "Backup created: ") + made.name if made else T("No hay partidas.", "No saves."))
        except OSError as error:
            self.toast(str(error), 10)
        self.build_pages(("saves",))

    def export_saves(self, root):
        def chosen(folder):
            try:
                target = core.export_saves(root, folder)
                self.toast(T("Partidas exportadas a ", "Saves exported to ") + str(target), 8)
            except OSError as error:
                self.toast(str(error), 10)
        self.choose_folder(T("Exportar partidas", "Export saves"), None, chosen)

    def import_saves(self, root):
        def chosen(folder):
            try:
                core.import_saves(folder, root, backup_dir())
                self.toast(T("Partida importada. La anterior quedó en una copia de seguridad.",
                             "Save imported. The previous one was backed up."), 8)
            except (OSError, ValueError) as error:
                self.toast(str(error), 12)
            self.build_pages(("saves",))
        self.choose_folder(T("Importar partida", "Import save"), None, chosen)

    def restore_backup(self, backup, root):
        try:
            core.restore_backup(backup, root, backup_dir())
            self.toast(T("Restaurada: ", "Restored: ") + Path(backup).name)
        except OSError as error:
            self.toast(str(error), 10)
        self.build_pages(("saves",))

    def remove_path(self, path, rebuild):
        try:
            shutil.rmtree(path) if Path(path).is_dir() else Path(path).unlink()
        except OSError as error:
            self.toast(f"{Path(path).name}: {error}", 10)
        self.build_pages(rebuild)

    # --- DLC -----------------------------------------------------------------------------------

    def build_dlc(self):
        page = Adw.PreferencesPage()
        intro = Adw.PreferencesGroup(
            title="DLC",
            description=T("Opcional: el juego funciona igual sin DLC. Los DLC de CTR Nitro-Fueled solo traen la licencia "
                          "(el contenido ya está en el juego); el port los marca como instalados al iniciar. Usa tus "
                          "propios paquetes DLC de PS4 (.pkg) de CUSA13795. Se copian a la carpeta dlc del port; tus "
                          "archivos originales no cambian.",
                          "Optional: the game runs the same without DLC. CTR Nitro-Fueled's DLC only carry the license "
                          "(the content is already in the game); the port reports them installed at start. Use your own "
                          "PS4 DLC packages (.pkg) for CUSA13795. They are copied into the port's dlc folder; your "
                          "original files are not changed."))
        enabled = self.settings.get("dlc", True)
        intro.add(switch_row(T("Activar los DLC", "Enable DLC"), None, enabled,
                             lambda on: self.setting("dlc", on, rebuild=("dlc",))))
        page.add(intro)

        found = Adw.PreferencesGroup(title=T("DLC encontrados", "DLC found"),
                                     description=T("Los cambios se aplican la próxima vez que pulses Jugar.",
                                                   "Changes apply the next time you press Play."))
        items = core.scan_dlc(dlc_dir())
        disabled = core.split_list(self.settings.get("dlc_disabled", ""))
        if not items:
            found.add(Adw.ActionRow(title=T("No hay DLC en la carpeta dlc.", "No DLC in the dlc folder.")))
        for d in items:
            if d["ok"]:
                row = switch_row(GLib.markup_escape_text(d["title"]), d["label"], d["label"] not in disabled,
                                 lambda on, label=d["label"]: self.switch_dlc(label, on))
                row.set_sensitive(enabled)
            else:
                row = Adw.ActionRow(title=GLib.markup_escape_text(d["file"]), subtitle=GLib.markup_escape_text(d["problem"]))
                row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["warning"]))
            remove = button(T("Quitar", "Remove"), lambda d=d: self.confirm(
                T("¿Quitar {} de la carpeta dlc?", "Remove {} from the dlc folder?").format(d["file"]),
                T("Solo se borra la copia del port.", "Only the port's copy is deleted."),
                T("Quitar", "Remove"), lambda: self.remove_path(d["path"], ("dlc",))))
            remove.add_css_class("flat")
            remove.set_sensitive(self.process is None)
            row.add_suffix(remove)
            found.add(row)
        found.add(button_bar(button(T("Añadir DLC (.pkg)...", "Add DLC (.pkg)..."), self.add_dlc),
                             button(T("Abrir la carpeta dlc", "Open the dlc folder"), lambda: open_folder(self, dlc_dir())),
                             button(T("Actualizar", "Refresh"), lambda: self.build_pages(("dlc",)))))
        page.add(found)
        return page

    def switch_dlc(self, label, on):
        disabled = [x for x in core.split_list(self.settings.get("dlc_disabled", "")) if x != label]
        if not on:
            disabled.append(label)
        self.setting("dlc_disabled", ",".join(disabled))

    def add_dlc(self):
        dialog = Gtk.FileDialog(title="DLC (.pkg)", modal=True)
        pkg = Gtk.FileFilter(name="PS4 DLC (*.pkg)")
        pkg.add_pattern("*.pkg")
        pkg.add_pattern("*.PKG")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(pkg)
        dialog.set_filters(filters)

        def done(dialog, result):
            try:
                files = dialog.open_multiple_finish(result)
            except GLib.Error:
                return
            paths = [Path(files.get_item(i).get_path()) for i in range(files.get_n_items())]
            added, problems = core.add_dlc(paths, dlc_dir())
            self.toast(T("DLC añadidos: ", "DLC added: ") + str(added) + (". " + ". ".join(problems) if problems else ""), 10)
            self.build_pages(("dlc",))
        dialog.open_multiple(self, None, done)

    # --- Tools ---------------------------------------------------------------------------------

    def build_tools(self):
        page = Adw.PreferencesPage()
        folders = Adw.PreferencesGroup(title=T("Carpetas y registro", "Folders and log"))
        folders.add(button_bar(
            button(T("Abrir la carpeta de registros", "Open the logs folder"), lambda: open_folder(self, DATA_DIR / "logs")),
            button(T("Abrir la carpeta de datos", "Open the data folder"), lambda: open_folder(self, DATA_DIR)),
            button(T("Abrir partidas guardadas", "Open saves"), lambda: open_folder(self, user_dir(self.settings)))))
        folders.add(switch_row(
            T("Guardar registro detallado (logs/, con estadísticas de fotogramas)",
              "Save a detailed log (logs/, with frame statistics)"),
            T("Para reportar fallos o tirones: adjunta el archivo de logs/ en un issue.",
              "To report crashes or stutter: attach the file from logs/ to an issue."),
            self.settings["save_log"], lambda on: self.setting("save_log", on)))
        saves_row = Adw.ActionRow(title=T("Carpeta de partidas", "Saves folder"),
                                  subtitle=str(user_dir(self.settings)) + ("" if self.settings["user_dir"] else T(" (predeterminada)", " (default)")))
        saves_row.add_suffix(icon_button("folder-open-symbolic", T("Elegir la carpeta de partidas", "Choose the saves folder"),
                                         lambda: self.choose_folder(T("Carpeta de partidas", "Saves folder"), user_dir(self.settings),
                                                                    lambda p: self.setting("user_dir", str(p), rebuild=("tools", "saves")))))
        saves_row.add_suffix(icon_button("edit-undo-symbolic", T("Volver a la carpeta predeterminada", "Back to the default folder"),
                                         lambda: self.setting("user_dir", "", rebuild=("tools", "saves"))))
        folders.add(saves_row)
        page.add(folders)

        cache = Adw.PreferencesGroup(
            title=T("Caché de shaders", "Shader cache"),
            description=T("Los shaders se guardan para que los siguientes arranques vayan fluidos. Bórrala solo si "
                          "actualizaste el driver de la GPU o ves fallos gráficos.",
                          "Shaders are kept so later sessions run smoothly. Clear it only after a GPU driver update or "
                          "if you see graphics glitches."))
        clear = button(T("Borrar la caché de shaders", "Clear the shader cache"), lambda: self.confirm(
            T("¿Borrar la caché de shaders?", "Clear the shader cache?"),
            T("El próximo arranque volverá a compilarlos.", "The next start compiles them again."),
            T("Borrar", "Clear"), self.clear_shader_cache))
        clear.set_sensitive(self.process is None)
        cache.add(button_bar(clear))
        page.add(cache)

        steam = Adw.PreferencesGroup(title="Steam")
        exe, _start, _options = steam_shortcuts.launcher_command()
        steam_row = Adw.ActionRow(
            title=T("Añadir a Steam", "Add to Steam"),
            subtitle=T("Un acceso directo \"{}\" que inicia el juego con estos ajustes (modo juego del Steam Deck). "
                       "Cierra Steam antes de añadirlo.",
                       "A \"{}\" shortcut that starts the game with these settings (Steam Deck game mode). Close Steam "
                       "before adding it.").format(steam_shortcuts.NAME))
        steam_row.set_tooltip_text(exe)
        steam_row.add_suffix(button(T("Añadir", "Add"), self.add_to_steam))
        steam.add(steam_row)
        page.add(steam)

        language = Adw.PreferencesGroup(title=T("Idioma del launcher", "Launcher language"))
        current = self.settings.get("ui_language", "")
        language.add(combo_row(T("Idioma", "Language"), None, labelled(UI_LANGUAGES),
                               current if current in ("es", "en") else "", self.set_ui_language))
        page.add(language)

        dev = Adw.PreferencesGroup(title=T("Desarrollador", "Developer"))
        dev.add(switch_row("MangoHud", None, self.settings["mangohud"], lambda on: self.setting("mangohud", on)))
        dev.add(switch_row(T("Estadísticas de fotogramas en el registro", "Frame statistics in the log"), None,
                           self.settings["frame_stats"], lambda on: self.setting("frame_stats", on)))
        dev.add(switch_row(T("Capa de validación de Vulkan (lenta)", "Vulkan validation layer (slow)"), None,
                           self.settings["vk_validation"], lambda on: self.setting("vk_validation", on)))
        env = Adw.EntryRow(title=T("Variables extra (VAR=valor ...)", "Extra environment (VAR=value ...)"),
                           text=self.settings["extra_env"])
        env.connect("changed", lambda r: self.setting("extra_env", r.get_text()))
        dev.add(env)
        page.add(dev)

        about = Adw.PreferencesGroup(title=T("Acerca de", "About"))
        dlss = all((PORT_DIR / ("bin" if PACKAGED else "out") / name).is_file() for name in ("libsacredfire_dlss.so",)) and \
            any((PORT_DIR / ("bin" if PACKAGED else "out")).glob("libnvidia-ngx-dlss.so.*"))
        about.add(note(f"CTR-NitroFueled-PC {port_version()}", "heading"))
        about.add(note(T("Versión de Linux de CTR-NitroFueled-PC (el fork de Windows de GetLostIn de SacredFire, de "
                         "T3CHNOLOG1C), basado en bbport (deadinside28), el port de Windows de Supermedo y shadPS4. "
                         "Parches de illusion y TroyWarez. Sin afiliación con Sony, Activision ni Beenox. Licencia GPL "
                         "v2 o posterior.",
                         "Linux build of CTR-NitroFueled-PC (GetLostIn's Windows fork of SacredFire by T3CHNOLOG1C), "
                         "built on bbport (deadinside28), Supermedo's Windows port and shadPS4. Patches by illusion and "
                         "TroyWarez. Not affiliated with Sony, Activision or Beenox. GPL v2 or later."), "body"))
        about.add(note(T("Escalado temporal: AMD FidelityFX FSR 3.1 (MIT) y NVIDIA DLSS. DLSS usa el SDK de NVIDIA DLSS "
                         "(libnvidia-ngx-dlss.so, redistribuido según su licencia, en licenses/) a través del puente de "
                         "IFreemz (MIT). NVIDIA, DLSS y GeForce RTX son marcas de NVIDIA Corporation.",
                         "Temporal upscaling: AMD FidelityFX FSR 3.1 (MIT) and NVIDIA DLSS. DLSS uses the NVIDIA DLSS SDK "
                         "(libnvidia-ngx-dlss.so, redistributed under its license, in licenses/) through IFreemz's bridge "
                         "(MIT). NVIDIA, DLSS and GeForce RTX are trademarks of NVIDIA Corporation."), "body"))
        about.add(note(T("DLSS: archivos presentes (necesita una tarjeta NVIDIA RTX)", "DLSS: files present (needs an NVIDIA RTX card)")
                       if dlss else T("DLSS: archivos no incluidos (se usa FSR 3)", "DLSS: files not included (FSR 3 is used)")))
        about.add(Gtk.LinkButton(uri=core.REPO_URL, label=core.REPO_URL, halign=Gtk.Align.START, margin_top=6))
        page.add(about)
        return page

    def set_ui_language(self, value):
        self.settings["ui_language"] = value
        save_settings(self.settings)
        core.set_language(value)
        GLib.idle_add(lambda: self.build_pages() and False)

    def clear_shader_cache(self):
        cache = user_dir(self.settings) / "cache" / core.TITLE_ID
        count = sum(len(files) for _d, _s, files in os.walk(cache))
        try:
            shutil.rmtree(cache)
            self.toast(T("Caché borrada ({} archivos).", "Cache cleared ({} files).").format(count))
        except FileNotFoundError:
            self.toast(T("Caché borrada (0 archivos).", "Cache cleared (0 files)."))
        except OSError as error:
            self.toast(T("No se pudo borrar la caché: ", "Could not clear the cache: ") + str(error))

    def add_to_steam(self):
        """A non-Steam shortcut that starts the game directly (--play: no launcher window, so it
        works in the Steam Deck's game mode). Steam rewrites shortcuts.vdf when it exits, so it has
        to be closed while the entry is written."""
        if steam_shortcuts.steam_running():
            self.toast(T("Cierra Steam primero: sobrescribe sus accesos directos al salir",
                         "Close Steam first: it overwrites its shortcuts when it exits"), 6)
            return
        files = steam_shortcuts.shortcut_files()
        if not files:
            self.toast(T("No se encontró la carpeta de usuario de Steam (userdata)", "No Steam user folder found (userdata)"), 6)
            return
        exe, start_dir, options = steam_shortcuts.launcher_command()
        icon = game_icon_path() if install_game_icon(self.settings["game_dir"]) else ""
        entry = steam_shortcuts.make_entry(exe, start_dir, launch_options=options, icon=str(icon))
        added = replaced = 0
        for path in files:
            try:
                if steam_shortcuts.add_shortcut(path, entry):
                    added += 1
                else:
                    replaced += 1
            except (OSError, ValueError) as error:
                self.toast(T("No se pudo escribir {}: {}", "Could not write {}: {}").format(path, error), 8)
                return
        self.toast(T("Añadido a Steam como \"{}\" ({} nuevos, {} actualizados); abre Steam para verlo",
                     "Added to Steam as \"{}\" ({} added, {} updated); start Steam to see it").format(
                         steam_shortcuts.NAME, added, replaced), 8)


class LauncherApp(Adw.Application):
    def __init__(self):
        # NON_UNIQUE: another launcher already running (an older AppImage or checkout, same
        # application id) would otherwise be raised instead of this one opening.
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        window = self.get_active_window() or LauncherWindow(self)
        window.present()


def play():
    """--play: the game with the saved settings, no window (Steam Deck game mode)."""
    settings = load_settings()
    core.set_language(settings.get("ui_language", ""))
    if not (Path(settings["game_dir"]).expanduser() / "eboot.bin").is_file():
        print("CTR Nitro-Fueled PC: choose the game folder in the launcher first", file=sys.stderr)
        return 1
    os.chdir(PORT_DIR)
    os.execvpe("bash", ["bash", str(PORT_DIR / "run.sh")], game_environment(settings))


if __name__ == "__main__":
    if "--play" in sys.argv[1:]:
        sys.exit(play())
    sys.exit(LauncherApp().run([a for a in sys.argv if a != "--play"]))
