#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""SacredFire launcher (GTK4 / libadwaita): runs Crash Team Racing: Nitro-Fueled natively on Linux.

Picks the game folder (the merged 1.21 dump), the controller and the start-up options passed to
run.sh as environment variables, edits the port's settings in bbport.ini (game patches, FPS
counter), starts and stops the game and shows its output. Launcher settings live in
~/.config/sacredfire-launcher/settings.json. `--play` starts the game with the saved settings
without a window (Steam Deck game mode).

Derived from bbport's launcher (removed with the Bloodborne profile on 2026-10-09); this one
knows only the CTR profile.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from bbport_i18n import language, set_language, tr

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

PORT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PORT_DIR / "scripts"))
import content_profile  # noqa: E402
import game_check  # noqa: E402
import patches as game_patches  # noqa: E402
import steam_shortcuts  # noqa: E402

PACKAGED = bool(os.environ.get("BB_PREBUILT"))


def port_version():
    """The build's source revision: VERSION in the package, out/VERSION in a checkout (build.sh
    writes it: the nearest v* tag or the commit, "-dirty" with uncommitted changes)."""
    for name in ("VERSION", "out/VERSION"):
        try:
            return (PORT_DIR / name).read_text().strip()
        except OSError:
            continue
    return ""


DATA_DIR = Path(os.environ.get("BB_DATA_DIR", PORT_DIR))
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sacredfire-launcher"
CONFIG_FILE = CONFIG_DIR / "settings.json"
MAX_LOG_LINES = 5000
TITLE = "SacredFire"
GAME = "Crash Team Racing: Nitro-Fueled"

# Choices: (label, value); the first is the default.
UI_LANGUAGES = [("System", ""), ("English", "en"), ("Русский", "ru"), ("Português (Brasil)", "pt_BR")]
# The languages CUSA13795 (the Americas build) has, as PS4 system language codes.
LANGUAGES = [("English", "1"), ("Spanish (Latin America)", "20"), ("French", "2"),
             ("Portuguese (Brazil)", "17")]
PRESENT_MODES = [("Mailbox", "Mailbox"), ("FIFO (VSync)", "Fifo"),
                 ("FIFO Relaxed", "FifoRelaxed"), ("Immediate", "Immediate")]
# Game patches: every patch for CUSA13795 1.21 in the patches folder's shadPS4-format XML files
# (patches/CrashTeamRacing.xml: community patches by illusion and TroyWarez from
# https://github.com/illusionyy/PS-Game-Patch), as the Windows launcher lists them. The ones the
# in-game menu also switches keep their bbport.ini key (scripts/patches.py GAME_PATCH_KEYS); the
# others go to patches.json. The 1080p Pro patch is not listed: Internal resolution decides it.
PATCH_INI_KEYS = {patch: key for key, patch in game_patches.GAME_PATCH_KEYS.items()}
HIDDEN_PATCHES = {game_patches.GAME_PATCH_KEYS["patch_pro_1080p"]}
PATCHES_DIR = Path(os.environ.get("BB_PATCHES_DIR", PORT_DIR / "patches"))
# Display and performance (bbport.ini keys the in-game menu and the Windows launcher edit too).
# render_res: the scene size; above and below 1080p the game's PS4 Pro path, which run.sh turns
# on by itself (scripts/patches.py render_plan, docs/sacredfire/13-resolution.md).
RENDER_RESOLUTIONS = [("480p (854 x 480, very low-end PCs)", "480"), ("720p (1280 x 720, low-end PCs)", "720"),
                      ("1080p (1920 x 1080)", "1080"), ("1440p (2560 x 1440, PS4 Pro)", "1440"),
                      ("1620p (2880 x 1620)", "1620"), ("4K (3840 x 2160)", "2160")]
UPSCALERS = [("Off", "off"), ("AMD FSR 1", "fsr1"), ("AMD FSR 3 (experimental)", "fsr3"),
             ("NVIDIA DLSS (experimental)", "dlss")]
DLSS_QUALITIES = [("Auto (internal resolution)", "auto"), ("DLAA (native)", "dlaa"), ("Quality", "quality"),
                  ("Balanced", "balanced"), ("Performance", "performance"),
                  ("Ultra Performance", "ultra_performance")]
DLSS_OUTPUTS = [("1920 x 1080", "1080"), ("3840 x 2160 (4K)", "2160")]
DLSS_MODELS = [("Transformer (recommended)", "transformer"), ("CNN", "cnn")]
FPS_LIMITS = [("No limit", "0")] + [(f"{v} FPS", str(v)) for v in (30, 60, 72, 90, 120, 144, 240)]

DEFAULTS = {
    "ui_language": "",
    "game_dir": "" if PACKAGED else str(PORT_DIR / "orig" / "CUSA13795"),
    "user_dir": "",
    "language": "1",
    "fullscreen": False,
    "present_mode": "Mailbox",
    "display": "",
    "dlc": True,
    "dlc_disabled": "",
    "gamepad": "",
    "gamepad_name": "",
    "clear_shader_cache": False,
    "frame_stats": False,
    "save_log": False,
    "mangohud": False,
    "vk_validation": False,
    "extra_env": "",
}
INI_DEFAULTS = {"show_fps": "0", **{key: "0" for key in game_patches.GAME_PATCH_KEYS}}
# The first bbport.ini, shared with run.sh and the Windows package (60 FPS limit, FSR 1, ...).
DEFAULT_INI = PORT_DIR / "packaging" / "bbport.default.ini"


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


def load_ini():
    values = dict(INI_DEFAULTS)
    lines = []
    try:
        lines = ini_path().read_text().splitlines()
    except OSError:
        try:
            lines = DEFAULT_INI.read_text().splitlines()
        except OSError:
            pass
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values, lines


def save_ini(values, lines):
    """Rewrites the edited keys in place, appends missing ones, keeps every other line."""
    written = set()
    out = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in values:
                out.append(f"{key}={values[key]}")
                written.add(key)
                continue
        out.append(line)
    if not lines:
        out.append("# SacredFire / bbport settings (in-game menu: Insert / L3+R3)")
    for key, value in values.items():
        if key not in written:
            out.append(f"{key}={value}")
    ini_path().write_text("\n".join(out) + "\n")


def game_environment(s):
    """Environment for run.sh from the launcher settings."""
    env = dict(os.environ)
    env["BB_GAME_DIR"] = str(Path(s["game_dir"]).expanduser())
    if s["user_dir"]:
        env["BB_USER_DIR"] = s["user_dir"]
    env["BB_LANGUAGE"] = s["language"]
    env["BB_MENU_LANGUAGE"] = language()
    env["BB_PRESENT_MODE"] = s["present_mode"]
    if s.get("display"):
        env["BB_DISPLAY"] = s["display"]
    env["BB_DLC"] = "1" if s.get("dlc", True) else "0"
    if s.get("dlc_disabled"):
        env["BB_DLC_DISABLED"] = s["dlc_disabled"]
    if s.get("gamepad"):
        env["BB_GAMEPAD"] = s["gamepad"]
    if s.get("clear_shader_cache"):
        env["BB_CLEAR_SHADER_CACHE"] = "1"
    if s["frame_stats"]:
        env["BB_FRAME_STATS"] = "1"
    if s.get("save_log"):
        env["BB_SAVE_LOG"] = "1"
    if s["mangohud"]:
        env["MANGOHUD"] = "1"
    if s["vk_validation"]:
        env["BB_VK_VALIDATION"] = "1"
    for item in s["extra_env"].split():
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
    """(GUID, name) of the connected gamepads (bb-gpu-capabilities --gamepads), [] if unknown."""
    return [(fields[0], "\t".join(fields[1:])) for fields in capabilities("--gamepads")]


def connected_displays():
    """(BB_DISPLAY value, label) of the monitors in SDL's order, numbered from 1 as BB_DISPLAY
    takes them (bb-gpu-capabilities --displays: name, WxH, primary)."""
    displays = []
    for number, fields in enumerate(capabilities("--displays"), 1):
        label = f"{number}: {fields[0]}" + (f" ({fields[1]})" if len(fields) > 1 else "")
        if len(fields) > 2 and fields[2] == "1":
            label += tr(" - primary")
        displays.append((str(number), label))
    return displays


def patches_config_path():
    return Path(os.environ.get("BB_PATCHES_CONFIG", DATA_DIR / "patches.json"))


def load_patches_config():
    """patches.json: {"enabled": [...], "disabled": [...]} (run.sh passes it to scripts/patches.py)."""
    try:
        config = json.loads(patches_config_path().read_text())
    except (OSError, ValueError):
        config = {}
    return {"enabled": list(config.get("enabled", [])), "disabled": list(config.get("disabled", []))}


def dlc_dir():
    return Path(os.environ.get("BB_DLC_DIR", DATA_DIR / "dlc"))


def flat_button(icon, tooltip, handler):
    button = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, tooltip_text=tooltip)
    button.add_css_class("flat")
    button.connect("clicked", handler)
    return button


def open_folder(window, path):
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    Gtk.FileLauncher.new(Gio.File.new_for_path(str(path))).launch(window, None, None)


def combo_row(title, subtitle, choices, current):
    model = Gtk.StringList.new([tr(choice[0]) for choice in choices])
    row = Adw.ComboRow(title=title, subtitle=subtitle or "", model=model)
    values = [choice[1] for choice in choices]
    row.set_selected(values.index(current) if current in values else 0)
    row.values = values
    return row


def combo_value(row):
    return row.values[row.get_selected()]


APP_ID = "io.github.sacredfire.Launcher"
# The game's log names this when the known clobbered-dword bug shows itself (gpu/shim/
# bbport_clobber.cpp AskForReport); the log page then asks for the log to be attached to the issue.
ISSUE2_MARKER = "SacredFire issue #2"
ISSUE2_URL = "https://github.com/T3CHNOLOG1C/SacredFire/issues/2"


def game_icon_path():
    share = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return share / "icons/hicolor/512x512/apps" / f"{APP_ID}.png"


def install_game_icon(game_dir):
    """The game's own icon (sce_sys/icon0.png) as this app's icon: copied into the user's icon
    theme under the application id, where GTK and the desktop find it (launcher/install-desktop.sh
    does the same for the menu entry). Game files never enter the repository."""
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


class LauncherWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=f"{TITLE} {port_version()}".rstrip())
        if install_game_icon(self.settings["game_dir"] if hasattr(self, "settings") else load_settings()["game_dir"]):
            self.set_icon_name(APP_ID)
        self.set_default_size(720, 760)
        self.settings = load_settings()
        set_language(self.settings.get("ui_language", ""))
        self.ini, self.ini_lines = load_ini()
        self.process = None
        self.stream = None
        self.build()
        self.connect("close-request", self.on_close)

    def build(self):
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        self.launch_button = Gtk.Button()
        self.launch_button.connect("clicked", self.on_launch)
        header.pack_end(self.launch_button)
        toolbar.add_top_bar(header)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.stack)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)
        self.stack.add_titled_with_icon(self.build_settings_page(), "settings", tr("Settings"),
                                        "preferences-system-symbolic")
        self.stack.add_titled_with_icon(self.build_log_page(), "log", tr("Log"),
                                        "utilities-terminal-symbolic")
        self.update_launch_button()
        self.update_game_status()

    def update_launch_button(self):
        running = self.process is not None
        self.launch_button.set_label(tr("Stop") if running else tr("Play"))
        self.launch_button.remove_css_class("destructive-action" if not running else "suggested-action")
        self.launch_button.add_css_class("destructive-action" if running else "suggested-action")

    # --- settings page -------------------------------------------------------------------

    def build_settings_page(self):
        page = Adw.PreferencesPage()

        game = Adw.PreferencesGroup(title=tr("Game"),
                                    description=tr("The dumped game with its 1.21 update merged over the base (eboot.bin, sce_module, ...)"))
        self.game_row = Adw.ActionRow(title=tr("Game folder (CUSA13795)"))
        self.game_status = Gtk.Image(valign=Gtk.Align.CENTER)
        self.game_row.add_suffix(self.game_status)
        self.game_row.add_suffix(flat_button("folder-open-symbolic", tr("Choose the folder with eboot.bin"), self.on_choose_game))
        game.add(self.game_row)
        self.user_row = Adw.ActionRow(title=tr("Saves folder"))
        self.user_row.add_suffix(flat_button("folder-open-symbolic", tr("Choose the saves folder"), self.on_choose_user))
        self.user_row.add_suffix(flat_button("edit-undo-symbolic", tr("Back to the default folder"), self.on_reset_user))
        self.user_row.add_suffix(flat_button("folder-symbolic", tr("Open in the file manager"),
                                             lambda _b: open_folder(self, self.user_dir())))
        game.add(self.user_row)
        self.language_row = combo_row(tr("Game language"),
                                      tr("The languages this build of the game has (CUSA13795, the Americas). "
                                         "Spain's Spanish is not in it"),
                                      LANGUAGES, self.settings["language"])
        game.add(self.language_row)
        page.add(game)

        controls = Adw.PreferencesGroup(title=tr("Controller"),
                                        description=tr("The keyboard always works too. "
                                                       "Split-screen ports are assigned in the in-game menu (Insert / L3+R3)."))
        self.gamepad_row = combo_row(tr("Controller"), None, self.gamepad_choices(), self.settings["gamepad"])
        self.gamepad_row.add_suffix(flat_button("view-refresh-symbolic", tr("Look for controllers again"),
                                                self.on_refresh_gamepads))
        controls.add(self.gamepad_row)
        page.add(controls)

        self.build_patches(page)
        self.build_dlc(page)

        performance = Adw.PreferencesGroup(title=tr("Performance"))
        self.fps_limit_row = combo_row(tr("FPS limit"),
                                       tr("With the 60 FPS patch the game runs as fast as the PC allows. On a 144 Hz "
                                          "screen without G-Sync/FreeSync, 72 divides the refresh evenly"),
                                       FPS_LIMITS, self.ini.get("fps_limit", "60"))
        performance.add(self.fps_limit_row)
        self.async_row = Adw.SwitchRow(title=tr("Compile shaders in the background (experimental)"),
                                       subtitle=tr("Fewer hitches when an effect is first seen; an effect may show up a frame late"),
                                       active=self.ini.get("async_pipelines") == "1")
        performance.add(self.async_row)
        page.add(performance)

        screen = Adw.PreferencesGroup(title=tr("Display"))
        self.render_res_row = combo_row(tr("Internal resolution"),
                                        tr("The size the game draws its 3D scene at. Above 1080p it uses the game's PS4 Pro "
                                           "path (turned on automatically) and the picture is output at 4K: on a 1080p screen "
                                           "that removes jagged edges. 4K needs a strong GPU; 720p and 480p help low-end PCs "
                                           "(the picture is output at 1080p)"),
                                        RENDER_RESOLUTIONS, self.ini.get("render_res", "1080"))
        screen.add(self.render_res_row)
        temporal = self.ini.get("temporal")
        if temporal not in [v for _, v in UPSCALERS]:  # v0.3 test builds: fsr3=1, fsr1=0
            if self.ini.get("fsr3") == "1":
                temporal = "fsr3"
            elif self.ini.get("fsr1") == "0":
                temporal = "off"
            else:
                temporal = "fsr1"
        self.upscaler_row = combo_row(tr("Upscaling"),
                                      tr("FSR 1 enlarges a smaller scene with sharper edges. FSR 3 and DLSS rebuild detail over "
                                         "several frames (DLSS needs an NVIDIA RTX card; FSR 3 runs otherwise). Experimental: "
                                         "switch to FSR 1 if you see trails"),
                                      UPSCALERS, temporal)
        screen.add(self.upscaler_row)
        self.dlss_quality_row = combo_row(tr("DLSS quality"),
                                          tr("Auto uses the internal resolution. The others draw the scene at NVIDIA's share "
                                             "of the DLSS output: Quality 67%, Balanced 58%, Performance 50%, "
                                             "Ultra Performance 33%, DLAA 100%"),
                                          DLSS_QUALITIES, self.ini.get("dlss_quality", "auto"))
        screen.add(self.dlss_quality_row)
        self.dlss_output_row = combo_row(tr("DLSS output"), None, DLSS_OUTPUTS, self.ini.get("dlss_output", "1080"))
        screen.add(self.dlss_output_row)
        self.dlss_model_row = combo_row(tr("DLSS model"),
                                        tr("Transformer: NVIDIA's current model (DLSS 4), sharper and steadier, a little "
                                           "heavier. CNN: the older model, lighter"),
                                        DLSS_MODELS, self.ini.get("dlss_model", "transformer"))
        screen.add(self.dlss_model_row)
        try:
            sharpness = float(self.ini.get("fsr3_sharpness", "0.30"))
        except ValueError:
            sharpness = 0.30
        self.fsr3_sharpness_row = Adw.SpinRow.new_with_range(0.0, 1.0, 0.05)
        self.fsr3_sharpness_row.set_title(tr("FSR 3 sharpness"))
        self.fsr3_sharpness_row.set_digits(2)
        self.fsr3_sharpness_row.set_value(sharpness)
        screen.add(self.fsr3_sharpness_row)
        for row in (self.upscaler_row, self.dlss_quality_row):
            row.connect("notify::selected", lambda *_: self.update_display_rows())
        self.update_display_rows()
        # bbport.ini, like the in-game menu's switch (BB_FULLSCREEN would override that one).
        self.fullscreen_row = Adw.SwitchRow(title=tr("Fullscreen"),
                                            active=self.ini.get("fullscreen", "1" if self.settings["fullscreen"] else "0") == "1")
        screen.add(self.fullscreen_row)
        self.present_row = combo_row(tr("Presentation"), None, PRESENT_MODES, self.settings["present_mode"])
        screen.add(self.present_row)
        self.fps_row = Adw.SwitchRow(title=tr("FPS counter in the corner"), active=self.ini.get("show_fps") == "1")
        screen.add(self.fps_row)
        displays = [(tr("Primary"), "")] + [(label, value) for value, label in connected_displays()]
        if self.settings["display"] and self.settings["display"] not in [v for _, v in displays]:
            displays.append((tr("{} (not connected)").format(self.settings["display"]), self.settings["display"]))
        self.display_row = combo_row(tr("Monitor"), tr("The screen the game window and fullscreen go to"),
                                     displays, self.settings["display"])
        screen.add(self.display_row)
        page.add(screen)

        dev = Adw.PreferencesGroup(title=tr("Developer"))
        self.mangohud_row = Adw.SwitchRow(title="MangoHud", active=self.settings["mangohud"])
        dev.add(self.mangohud_row)
        self.stats_row = Adw.SwitchRow(title=tr("Frame statistics in the log"), active=self.settings["frame_stats"])
        dev.add(self.stats_row)
        self.save_log_row = Adw.SwitchRow(title=tr("Save the log to a file (logs/)"), active=self.settings["save_log"])
        dev.add(self.save_log_row)
        self.cache_row = Adw.SwitchRow(title=tr("Clear the shader cache at start"),
                                       subtitle=tr("The cache is kept by default so known areas do not compile shaders mid-race; clear it after GPU driver updates or rendering glitches"),
                                       active=self.settings["clear_shader_cache"])
        dev.add(self.cache_row)
        self.validation_row = Adw.SwitchRow(title=tr("Vulkan validation layer (slow)"), active=self.settings["vk_validation"])
        dev.add(self.validation_row)
        self.env_row = Adw.EntryRow(title=tr("Extra environment (VAR=value ...)"), text=self.settings["extra_env"])
        dev.add(self.env_row)
        self.ui_language_row = combo_row(tr("Launcher language"), tr("Applies after restarting the launcher"),
                                         UI_LANGUAGES, self.settings["ui_language"])
        dev.add(self.ui_language_row)
        page.add(dev)

        steam = Adw.PreferencesGroup(title=tr("Steam"))
        exe, _start, _options = steam_shortcuts.launcher_command()
        steam_row = Adw.ActionRow(title=tr("Add to Steam"),
                                  subtitle=tr("A non-Steam shortcut that starts the game with these settings (Steam Deck game mode). Close Steam before adding."))
        steam_row.set_tooltip_text(exe)
        add_button = Gtk.Button(label=tr("Add"), valign=Gtk.Align.CENTER)
        add_button.connect("clicked", self.on_add_to_steam)
        steam_row.add_suffix(add_button)
        steam.add(steam_row)
        page.add(steam)
        self.update_user_status()
        return page

    def update_display_rows(self):
        """DLSS rows with DLSS, the sharpness with FSR 3; a DLSS quality sets the resolution."""
        upscaler = combo_value(self.upscaler_row)
        dlss = upscaler == "dlss"
        dlss_sized = dlss and combo_value(self.dlss_quality_row) != "auto"
        for row in (self.dlss_quality_row, self.dlss_model_row):
            row.set_visible(dlss)
        self.dlss_output_row.set_visible(dlss_sized)
        self.fsr3_sharpness_row.set_visible(upscaler == "fsr3")
        self.render_res_row.set_sensitive(not dlss_sized)

    def gamepad_choices(self, pads=None):
        pads = connected_gamepads() if pads is None else pads
        choices = [(tr("First connected"), "")] + [(name, guid) for guid, name in pads]
        if self.settings["gamepad"] and self.settings["gamepad"] not in [g for g, _ in pads]:
            choices.append((tr("{} (not connected)").format(self.settings["gamepad_name"] or self.settings["gamepad"]), self.settings["gamepad"]))
        return choices

    def on_refresh_gamepads(self, _button):
        self.settings["gamepad"] = combo_value(self.gamepad_row)
        self.settings["gamepad_name"] = self.gamepad_row.get_selected_item().get_string() if self.settings["gamepad"] else ""
        pads = connected_gamepads()
        choices = self.gamepad_choices(pads)
        self.gamepad_row.set_model(Gtk.StringList.new([tr(label) for label, _ in choices]))
        self.gamepad_row.values = [value for _, value in choices]
        current = self.settings["gamepad"]
        self.gamepad_row.set_selected(self.gamepad_row.values.index(current) if current in self.gamepad_row.values else 0)
        self.toasts.add_toast(Adw.Toast(title=tr("Controllers found: {}").format(len(pads))))

    def build_patches(self, page):
        """Every patch for this game version in the patches folder, as the Windows launcher's
        Patches tab: menu patches on their bbport.ini key, the others in patches.json."""
        group = Adw.PreferencesGroup(title=tr("Game patches"),
                                     description=tr("Applied when the game starts (or with the in-game menu's \"Apply and restart\"). "
                                                    "shadPS4-format .xml files in the patches folder for CUSA13795 01.21 are listed"))
        self.patch_rows = {}
        self.patches_config = load_patches_config()
        found = game_patches.external_patches(PATCHES_DIR)
        if not found:
            group.add(Adw.ActionRow(title=tr("No patches in the patches folder")))
        for key, _path, meta in found:
            if key in HIDDEN_PATCHES:
                continue
            ini_key = PATCH_INI_KEYS.get(key)
            if ini_key:
                on = self.ini.get(ini_key) == "1"
            else:
                on = key in self.patches_config["enabled"] or (
                    key not in self.patches_config["disabled"] and meta.get("isEnabled", "false").lower() == "true")
            byline = " ".join(part for part in (tr("by {}").format(meta.get("Author")) if meta.get("Author") else "",
                                                f"v{meta.get('PatchVer')}" if meta.get("PatchVer") else "") if part)
            subtitle = " · ".join(part for part in (byline, meta.get("Note", "")) if part)
            row = Adw.SwitchRow(title=meta.get("Name", key), subtitle=subtitle, active=on)
            row.set_tooltip_text(key)
            group.add(row)
            self.patch_rows[key] = (row, ini_key, meta.get("isEnabled", "false").lower() == "true")
        page.add(group)

    def store_patches(self):
        enabled, disabled = self.patches_config["enabled"], self.patches_config["disabled"]
        changed = False
        for key, (row, ini_key, default_on) in self.patch_rows.items():
            on = row.get_active()
            if ini_key:
                self.ini[ini_key] = "1" if on else "0"
                continue
            was = (key in enabled, key in disabled)
            for listing in (enabled, disabled):
                while key in listing:
                    listing.remove(key)
            if on and not default_on:
                enabled.append(key)
            elif not on and default_on:
                disabled.append(key)
            changed |= was != (key in enabled, key in disabled)
        if changed:
            try:
                patches_config_path().parent.mkdir(parents=True, exist_ok=True)
                patches_config_path().write_text(json.dumps(self.patches_config, indent=2) + "\n")
            except OSError as error:
                self.toasts.add_toast(Adw.Toast(title=tr("Could not write {}: {}").format(patches_config_path(), error)))

    def build_dlc(self, page):
        """The player's DLC packages (.pkg) in the dlc folder, as the Windows launcher's DLC tab:
        all of them on or off (BB_DLC) and each one (BB_DLC_DISABLED, by label)."""
        group = Adw.PreferencesGroup(title=tr("DLC"),
                                     description=tr("Your own DLC packages (.pkg) for CUSA13795, copied into the dlc folder; "
                                                    "applied when you press Play"))
        group.set_header_suffix(flat_button("folder-symbolic", tr("Open the dlc folder"),
                                            lambda _b: open_folder(self, dlc_dir())))
        self.dlc_row = Adw.SwitchRow(title=tr("Load DLC"), active=self.settings["dlc"])
        group.add(self.dlc_row)
        disabled = {x.strip() for x in self.settings["dlc_disabled"].split(",") if x.strip()}
        self.dlc_rows = {}
        try:
            addons = content_profile.find_addons(dlc_dir(), "CUSA13795")
        except (OSError, ValueError):
            addons = []
        for label, title, source in addons:
            row = Adw.SwitchRow(title=title, subtitle=f"{label} · {source}", active=label not in disabled)
            self.dlc_row.bind_property("active", row, "sensitive", GObject.BindingFlags.SYNC_CREATE)
            group.add(row)
            self.dlc_rows[label] = row
        if not addons:
            self.dlc_row.set_subtitle(tr("No DLC in {}").format(dlc_dir()))
        self.dlc_known_disabled = disabled - set(self.dlc_rows)
        page.add(group)

    def game_dir(self):
        return Path(self.settings["game_dir"]).expanduser()

    def user_dir(self):
        return Path(self.settings["user_dir"]).expanduser() if self.settings["user_dir"] else DATA_DIR / "user"

    def update_user_status(self):
        self.user_row.set_subtitle(str(self.user_dir()) + ("" if self.settings["user_dir"] else tr(" (default)")))

    def update_game_status(self):
        path = self.game_dir()
        ok = bool(self.settings["game_dir"]) and (path / "eboot.bin").is_file()
        self.game_row.set_subtitle(str(path) if self.settings["game_dir"] else tr("not chosen"))
        problem = game_check.problem(path) if ok else None
        profile = game_check.profile(path) if ok and not problem else None
        if not ok:
            tooltip = tr("No eboot.bin in the folder")
        elif problem:
            tooltip = game_check.explain(*problem)
            self.game_row.set_subtitle(f"{path}\n{tooltip}")
        elif profile and profile["name"] != "ctr":
            tooltip = tr("This is {}; this launcher runs Crash Team Racing: Nitro-Fueled").format(profile["game"])
            problem = ("other_title", profile["title"], profile["version"])
            self.game_row.set_subtitle(f"{path}\n{tooltip}")
        else:
            tooltip = f"{GAME} CUSA13795, 1.21"
        self.game_status.set_from_icon_name("object-select-symbolic" if ok and not problem else "dialog-warning-symbolic")
        self.game_status.set_tooltip_text(tooltip)
        self.launch_button.set_sensitive((ok and not problem) or self.process is not None)

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
                chosen(folder.get_path())
        dialog.select_folder(self, None, done)

    def on_choose_game(self, _button):
        def chosen(path):
            self.settings["game_dir"] = path
            self.update_game_status()
            self.store()
        self.choose_folder(tr("Game folder (with eboot.bin)"), self.game_dir(), chosen)

    def on_choose_user(self, _button):
        def chosen(path):
            self.settings["user_dir"] = path
            self.update_user_status()
            self.store()
        self.choose_folder(tr("Saves folder"), self.user_dir(), chosen)

    def on_reset_user(self, _button):
        self.settings["user_dir"] = ""
        self.update_user_status()
        self.store()

    def store(self):
        s = self.settings
        s["language"] = combo_value(self.language_row)
        s["gamepad"] = combo_value(self.gamepad_row)
        s["gamepad_name"] = self.gamepad_row.get_selected_item().get_string() if s["gamepad"] else ""
        s["fullscreen"] = self.fullscreen_row.get_active()
        s["present_mode"] = combo_value(self.present_row)
        s["display"] = combo_value(self.display_row)
        s["dlc"] = self.dlc_row.get_active()
        s["dlc_disabled"] = ",".join(sorted(self.dlc_known_disabled | {
            label for label, row in self.dlc_rows.items() if not row.get_active()}))
        s["mangohud"] = self.mangohud_row.get_active()
        s["frame_stats"] = self.stats_row.get_active()
        s["save_log"] = self.save_log_row.get_active()
        s["clear_shader_cache"] = self.cache_row.get_active()
        s["vk_validation"] = self.validation_row.get_active()
        s["extra_env"] = self.env_row.get_text()
        s["ui_language"] = combo_value(self.ui_language_row)
        save_settings(s)
        self.store_patches()
        self.ini["show_fps"] = "1" if self.fps_row.get_active() else "0"
        self.ini["fullscreen"] = "1" if self.fullscreen_row.get_active() else "0"
        self.ini["fps_limit"] = combo_value(self.fps_limit_row)
        self.ini["async_pipelines"] = "1" if self.async_row.get_active() else "0"
        self.ini["render_res"] = combo_value(self.render_res_row)
        self.ini["temporal"] = combo_value(self.upscaler_row)
        self.ini["dlss_quality"] = combo_value(self.dlss_quality_row)
        self.ini["dlss_output"] = combo_value(self.dlss_output_row)
        self.ini["dlss_model"] = combo_value(self.dlss_model_row)
        self.ini["fsr3_sharpness"] = f"{self.fsr3_sharpness_row.get_value():.2f}"
        # The 1080p Pro patch's switch is gone (render_res decides, as in the Windows launcher): an
        # older bbport.ini that has it on is turned off. temporal= wins over the old fsr1/fsr3 keys.
        if "patch_pro_1080p" in self.ini:
            self.ini["patch_pro_1080p"] = "0"
        save_ini(self.ini, self.ini_lines)
        self.ini, self.ini_lines = load_ini()

    def environment(self):
        return game_environment(self.settings)

    # --- log page and process ------------------------------------------------------------

    def build_log_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Shown when the game's log reports the known clobbered-dword bug (issue #2): the log
        # holds the diagnostics that can find its writer, so ask for it.
        self.issue_notice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                                    margin_top=8, margin_bottom=2, margin_start=8, margin_end=8,
                                    visible=False)
        self.issue_notice.add_css_class("card")
        issue_label = Gtk.Label(
            label=tr("The game hit the known \"clobbered command buffer\" bug (issue #2). "
                     "Please export this log and attach it to the issue so the cause can be fixed."),
            wrap=True, xalign=0, hexpand=True, margin_top=6, margin_bottom=6, margin_start=6)
        self.issue_notice.append(issue_label)
        issue_button = Gtk.Button(label=tr("Open issue #2"), valign=Gtk.Align.CENTER, margin_end=6)
        issue_button.connect("clicked", self.on_open_issue)
        self.issue_notice.append(issue_button)
        box.append(self.issue_notice)
        self.log_view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_top_margin(8)
        self.log_view.set_left_margin(8)
        self.log_view.set_right_margin(8)
        self.log_scroller = Gtk.ScrolledWindow(vexpand=True, child=self.log_view)
        box.append(self.log_scroller)
        # bbport 0.5 (PR 78): the log to the clipboard or to a file, for bug reports.
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, margin_top=6,
                      margin_bottom=6, margin_start=8, margin_end=8)
        self.export_log_button = Gtk.Button(label=tr("Export log…"))
        self.export_log_button.connect("clicked", self.on_export_log)
        bar.append(self.export_log_button)
        self.copy_log_button = Gtk.Button(label=tr("Copy"))
        self.copy_log_button.connect("clicked", self.on_copy_log)
        bar.append(self.copy_log_button)
        box.append(bar)
        buffer = self.log_view.get_buffer()
        buffer.connect("changed", self.on_log_changed)
        self.on_log_changed(buffer)
        return box

    def on_log_changed(self, buffer):
        has_text = buffer.get_char_count() > 0
        self.copy_log_button.set_sensitive(has_text)
        self.export_log_button.set_sensitive(has_text)

    def log_text(self):
        buffer = self.log_view.get_buffer()
        return buffer.get_text(*buffer.get_bounds(), False)

    def on_open_issue(self, _button):
        Gio.AppInfo.launch_default_for_uri(ISSUE2_URL, None)

    def note_issue_line(self, line):
        """A line of the game's log: the notice appears at the first mention of the bug."""
        if ISSUE2_MARKER in line and not self.issue_notice.get_visible():
            self.issue_notice.set_visible(True)
            self.toasts.add_toast(Adw.Toast(
                title=tr("Known bug (issue #2) hit: please export the log and attach it to the issue"),
                timeout=10))

    def on_copy_log(self, _button):
        self.get_clipboard().set(self.log_text())
        self.toasts.add_toast(Adw.Toast(title=tr("Log copied to the clipboard")))

    def on_export_log(self, _button):
        text = self.log_text()
        dialog = Gtk.FileDialog(title=tr("Export log"))
        dialog.set_initial_name(f"sacredfire-{time.strftime('%Y%m%d_%H%M%S')}.log")

        def finish(dialog, result):
            try:
                chosen = dialog.save_finish(result)
            except GLib.Error:
                return  # cancelled
            try:
                Path(chosen.get_path()).write_text(text, encoding="utf-8")
                self.toasts.add_toast(Adw.Toast(title=tr("Log saved to {}").format(chosen.get_path())))
            except OSError as error:
                self.toasts.add_toast(Adw.Toast(title=tr("Could not save the log: {}").format(error)))
        dialog.save(self, None, finish)

    # --- Steam ---------------------------------------------------------------------------

    def on_add_to_steam(self, _button):
        """A non-Steam shortcut that starts the game directly (--play: no launcher window, so
        it works in the Steam Deck's game mode). Steam rewrites shortcuts.vdf when it exits, so
        it has to be closed while the entry is written."""
        if steam_shortcuts.steam_running():
            self.toasts.add_toast(Adw.Toast(title=tr("Close Steam first: it overwrites its shortcuts when it exits"),
                                            timeout=6))
            return
        files = steam_shortcuts.shortcut_files()
        if not files:
            self.toasts.add_toast(Adw.Toast(title=tr("No Steam user folder found (userdata)"), timeout=6))
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
                self.toasts.add_toast(Adw.Toast(title=tr("Could not write {}: {}").format(path, error), timeout=8))
                return
        self.toasts.add_toast(Adw.Toast(
            title=tr("Added to Steam as \"{}\" ({} added, {} updated); start Steam to see it").format(
                steam_shortcuts.NAME, added, replaced), timeout=8))

    def append_log(self, text):
        buffer = self.log_view.get_buffer()
        buffer.insert(buffer.get_end_iter(), text)
        extra = buffer.get_line_count() - MAX_LOG_LINES
        if extra > 0:
            buffer.delete(buffer.get_start_iter(), buffer.get_iter_at_line(extra)[1])
        adj = self.log_scroller.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper()) and False)

    def on_launch(self, _button):
        if self.process:
            self.stop_game()
            return
        self.store()
        self.log_view.get_buffer().set_text("")
        self.issue_notice.set_visible(False)
        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        launcher.set_environ([f"{k}={v}" for k, v in self.environment().items()])
        launcher.set_cwd(str(PORT_DIR))
        try:
            self.process = launcher.spawnv(["setsid", "bash", str(PORT_DIR / "run.sh")])
        except GLib.Error as error:
            self.toasts.add_toast(Adw.Toast(title=tr("Could not start: {}").format(error.message)))
            return
        self.stream = Gio.DataInputStream.new(self.process.get_stdout_pipe())
        self.read_line()
        self.process.wait_async(None, self.on_exit)
        self.update_launch_button()
        self.stack.set_visible_child_name("log")

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
        self.note_issue_line(line)
        self.read_line()

    def stop_game(self):
        if not self.process:
            return
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
        self.process = None
        self.update_launch_button()
        self.update_game_status()
        self.append_log(tr("\n— the game exited (code {}) —\n").format(status))

    def on_close(self, _window):
        self.store()
        if self.process:
            self.stop_game()
        return False


class LauncherApp(Adw.Application):
    def __init__(self):
        # NON_UNIQUE: another SacredFire launcher already running (an older AppImage or checkout,
        # same application id) would otherwise be raised instead of this one opening.
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        window = self.get_active_window() or LauncherWindow(self)
        window.present()


def play():
    """--play: the game with the saved settings, no window (Steam Deck game mode)."""
    settings = load_settings()
    if not (Path(settings["game_dir"]).expanduser() / "eboot.bin").is_file():
        print("SacredFire: choose the game folder in the launcher first", file=sys.stderr)
        return 1
    os.chdir(PORT_DIR)
    os.execvpe("bash", ["bash", str(PORT_DIR / "run.sh")], game_environment(settings))


if __name__ == "__main__":
    if "--play" in sys.argv[1:]:
        sys.exit(play())
    sys.exit(LauncherApp().run([a for a in sys.argv if a != "--play"]))
