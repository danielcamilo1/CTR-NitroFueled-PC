"""The Linux launcher's window driven through its handlers on a display (CI: xvfb-run with
launcher/shell.nix): every tab in both languages, preparing a game from fake dumps, a key binding,
the upscaler rows, saves, DLC, patches and the shader cache. Any Python error in a GTK callback
fails it. Not a unittest module (it needs GTK and a display):

    xvfb-run -a nix-shell launcher/shell.nix --run 'python3 tests/launcher_gui_smoke.py'
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
temp = Path(tempfile.mkdtemp())
for key, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "share"), ("BB_DATA_DIR", "data")):
    os.environ[key] = str(temp / sub)
    (temp / sub).mkdir()
os.environ.setdefault("GSK_RENDERER", "cairo")
sys.path[:0] = [str(ROOT / "launcher"), str(ROOT / "tests")]

errors = []
sys.excepthook = lambda kind, value, tb: (errors.append(value), sys.__excepthook__(kind, value, tb))

import sacredfire_launcher as L  # noqa: E402
from gi.repository import GLib  # noqa: E402
from test_launcher_core import dump, pkg  # noqa: E402


def pump(seconds=0.2):
    end = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        while context.iteration(False):
            pass
        time.sleep(0.01)


def check(condition, what):
    if not condition:
        errors.append(AssertionError(what))
        print("FAILED:", what)


app = L.LauncherApp()
app.register(None)
win = L.LauncherWindow(app)
win.present()
pump()

for language in ("es", "en"):
    win.set_ui_language(language)
    pump()
    for name in win.PAGES:
        check(win.stack.get_child_by_name(name) is not None, f"{language}: tab {name}")
        win.stack.set_visible_child_name(name)
        pump(0.05)

# Play: the base game and the update prepared into a new folder, then checked.
base = dump(temp / "games/CUSA13795", "gd", "01.00", {"data/a": b"a"})
update = dump(temp / "games/CUSA13795-patch", "gp", "01.21", {"data/b": b"b"})
win.settings.update(base_dir=str(base), update_dir=str(update), game_dir="")
win.analyzed.clear()
win.update_play()
win.start_merge([base, update])
for _ in range(100):
    pump(0.05)
    if win.merging is None:
        break
check(win.settings["game_dir"] == str(temp / "games/CUSA13795-1.21"), "prepared game folder")
check((temp / "games/CUSA13795-1.21/data/b").is_file(), "merged files")

# Keyboard: Change, then Return (evdev 28) for Cross; Default removes it.
cross = [i for i, k in enumerate(L.core.KEY_INPUTS) if k[0] == "cross"][0]
win.stack.set_visible_child_name("keyboard")
win.begin_capture(cross)
check(win.on_key(None, 0, 28 + 8, 0) is True, "key consumed")
check(win.ini.get("key.cross") == "Return", "key.cross=Return")
win.reset_key("cross")
check(not win.ini.has("key.cross"), "key.cross reset")
win.begin_capture(cross)
win.stack.set_visible_child_name("play")  # leaving the tab cancels
check(win.capturing == -1, "capture cancelled")

# Settings: DLSS with a quality shows its rows; FSR 3 its sharpness.
for key, value in (("temporal", "dlss"), ("dlss_quality", "quality"), ("temporal", "fsr3")):
    win.ini_set(key, value, rebuild=("settings",))
    pump(0.1)
check(L.core.Ini(L.ini_path()).get("temporal") == "fsr3", "bbport.ini written")
win.refresh_devices()

# Saves: a slot, a backup, the tab rebuilt.
root = L.core.save_root(L.user_dir(win.settings))
(root / "SAVE00").mkdir(parents=True)
(root / "SAVE00/x").write_bytes(b"x")
win.backup_now(root)
pump()
check(len(L.core.list_backups(L.backup_dir())) == 1, "backup made")

# DLC: one package, switched off.
L.dlc_dir().mkdir(parents=True, exist_ok=True)
(L.dlc_dir() / "a.pkg").write_bytes(pkg({"CATEGORY": "ac", "CONTENT_ID": "UP0002-CUSA13795_00-CTRDELUXEPACK001",
                                          "TITLE": "Deluxe", "TITLE_ID": "CUSA13795"}))
win.build_pages(("dlc",))
win.switch_dlc("CTRDELUXEPACK001", False)
check(win.settings["dlc_disabled"] == "CTRDELUXEPACK001", "DLC switched off")

# Patches and the shader cache.
patch = L.core.list_patches([ROOT / "patches"])[0]
win.switch_patch(patch, True)
win.reload_patches()
(L.user_dir(win.settings) / "cache/CUSA13795").mkdir(parents=True)
win.clear_shader_cache()
check(not (L.user_dir(win.settings) / "cache/CUSA13795").exists(), "shader cache cleared")
pump()

win.destroy()
pump()
if errors:
    print(f"{len(errors)} error(s):", *errors, sep="\n  ")
    sys.exit(1)
print("launcher GUI smoke test: OK")
