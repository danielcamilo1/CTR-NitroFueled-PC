"""launcher/ctr_core.py: the Linux launcher's game folders, preparing the game, saves, DLC,
keyboard bindings, patches and settings file, as the Windows launcher does them."""
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

from paths import ROOT

sys.path.insert(0, str(ROOT / "launcher"))
import ctr_core as core  # noqa: E402


def param_sfo(entries):
    keys = data = b""
    index = []
    for key, value in entries.items():
        raw = value.encode() + b"\0"
        index.append((len(keys), 0x204, len(raw), len(raw), len(data)))
        keys += key.encode() + b"\0"
        data += raw
    start = 20 + 16 * len(entries)
    return (struct.pack("<4sIIII", b"\0PSF", 0x101, start, start + len(keys), len(entries))
            + b"".join(struct.pack("<HHIII", *e) for e in index) + keys + data)


def pkg(entries):
    param = param_sfo(entries)
    header = bytearray(0x200)
    header[:4] = b"\x7fCNT"
    struct.pack_into(">I", header, 0x10, 1)
    struct.pack_into(">I", header, 0x18, 0x100)
    struct.pack_into(">IIIIII", header, 0x100, 0x1000, 0, 0, 0, 0x200, len(param))
    return bytes(header) + param


def dump(folder, category, version, files=(), split=False):
    """A fake dump: eboot.bin, sce_sys/param.sfo and files (relative path -> bytes)."""
    folder = Path(folder)
    files_root = folder / "Image0" if split else folder
    sys_root = folder / "Sc0" if split else folder / "sce_sys"
    sys_root.mkdir(parents=True, exist_ok=True)
    files_root.mkdir(parents=True, exist_ok=True)
    (sys_root / "param.sfo").write_bytes(param_sfo({"APP_VER": version, "CATEGORY": category, "TITLE_ID": "CUSA13795"}))
    (files_root / "eboot.bin").write_bytes(b"eboot " + version.encode())
    for name, data in dict(files).items():
        (files_root / name).parent.mkdir(parents=True, exist_ok=True)
        (files_root / name).write_bytes(data)
    return folder


class LauncherCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        core.set_language("en")

    def tearDown(self):
        self.tmp.cleanup()

    def test_language(self):
        core.set_language("es")
        self.assertEqual(core.T("Jugar", "Play"), "Jugar")
        core.set_language("en")
        self.assertEqual(core.T("Jugar", "Play"), "Play")

    def test_ini_keeps_other_lines_and_erases(self):
        path = self.root / "bbport.ini"
        path.write_text("# comment\nshow_fps=1\nkey.cross=Space\n")
        ini = core.Ini(path)
        self.assertTrue(ini.flag("show_fps"))
        ini.set("show_fps", "0")
        ini.set("render_res", "1440")
        ini.erase("key.cross")
        ini.save()
        self.assertEqual(path.read_text(), "# comment\nshow_fps=0\nrender_res=1440\n")
        default = self.root / "default.ini"
        default.write_text("fps_limit=60\n")
        self.assertEqual(core.Ini(self.root / "missing.ini", default).get("fps_limit"), "60")

    def test_analyze_and_merge(self):
        base = dump(self.root / "CUSA13795", "gd", "01.00", {"data/a.bin": b"base a", "data/b.bin": b"base b"})
        update = dump(self.root / "CUSA13795-patch", "gp", "01.21", {"data/b.bin": b"update b"})
        self.assertEqual(core.analyze(base)["kind"], core.BASE)
        self.assertEqual(core.analyze(update)["kind"], core.UPDATE)  # small: the update alone
        self.assertEqual(core.analyze(self.root / "nothing")["kind"], core.MISSING)
        self.assertEqual(core.analyze("")["kind"], core.EMPTY)
        (self.root / "empty").mkdir()
        self.assertEqual(core.analyze(self.root / "empty")["kind"], core.NOT_GAME)
        target = core.merge_target(base, [base, update])
        self.assertEqual(target, self.root / "CUSA13795-1.21")
        progress = []
        files, copied = core.merge([base, update], target, lambda done, total: progress.append((done, total)))
        self.assertEqual((target / "data/a.bin").read_bytes(), b"base a")
        self.assertEqual((target / "data/b.bin").read_bytes(), b"update b")
        self.assertEqual((target / "eboot.bin").read_bytes(), b"eboot 01.21")
        self.assertEqual(files, 4)
        self.assertEqual(progress[-1], (4, 4))
        self.assertEqual(copied, 0)  # hard links on one drive
        self.assertEqual((target / "data/a.bin").stat().st_ino, (base / "data/a.bin").stat().st_ino)
        self.assertEqual(core.analyze(target)["kind"], core.UPDATE)  # under 10 GB: a fixture

    def test_split_dump_is_flattened(self):
        merged = dump(self.root / "pkgtool", "gp", "01.21", {"data/x": b"x"}, split=True)
        info = core.analyze(merged)
        self.assertTrue(info["split"])
        self.assertEqual(core.game_root(merged / "Image0"), merged)
        self.assertEqual(core.game_root(self.root), self.root / "pkgtool")
        target = self.root / "flat"
        core.merge([merged], target)
        self.assertTrue((target / "sce_sys/param.sfo").is_file())
        self.assertTrue((target / "data/x").is_file())
        self.assertFalse(core.analyze(target)["split"])

    def test_saves_backup_restore_import(self):
        user = self.root / "user"
        root = core.save_root(user)
        self.assertEqual(root, user / "savedata/1/CUSA13795")
        (root / "SAVE00").mkdir(parents=True)
        (root / "SAVE00/data.bin").write_bytes(b"progress")
        (root / "SAVE00.sce_sys").mkdir()
        (root / "SAVE00.sce_sys/param.bin").write_bytes(core.SAVE_PARAM.pack(b"Crash", b"Adventure", b"50%", 0, 0, 0, b""))
        saves = core.list_saves(root)
        self.assertEqual([(s["dir"], s["title"], s["subtitle"], s["detail"]) for s in saves],
                         [("SAVE00", "Crash", "Adventure", "50%")])
        backups = self.root / "backups"
        made = core.backup_saves(root, backups)
        self.assertTrue((made / "SAVE00/data.bin").is_file())
        self.assertEqual([b["name"] for b in core.list_backups(backups)], [made.name])
        (root / "SAVE00/data.bin").write_bytes(b"lost")
        core.restore_backup(made, root, backups)
        self.assertEqual((root / "SAVE00/data.bin").read_bytes(), b"progress")
        # A decrypted PS4 save: its files and sce_sys/param.sfo, into the runtime's layout.
        ps4 = self.root / "apollo"
        (ps4 / "sce_sys").mkdir(parents=True)
        (ps4 / "sce_sys/param.sfo").write_bytes(param_sfo({"MAINTITLE": "CTR", "SAVEDATA_DIRECTORY": "SAVE01"}))
        (ps4 / "game.dat").write_bytes(b"ps4")
        core.import_saves(ps4, root, backups)
        self.assertEqual((root / "SAVE01/game.dat").read_bytes(), b"ps4")
        self.assertEqual(core.list_saves(root)[1]["title"], "CTR")
        (self.root / "not-a-save").mkdir()
        with self.assertRaises(ValueError):
            core.import_saves(self.root / "not-a-save", root, backups)
        exported = core.export_saves(root, self.root)
        self.assertTrue((exported / "SAVE01/game.dat").is_file())

    def test_dlc(self):
        folder = self.root / "dlc"
        good = self.root / "a.pkg"
        good.write_bytes(pkg({"CATEGORY": "ac", "CONTENT_ID": "UP0002-CUSA13795_00-CTRDELUXEPACK001",
                              "TITLE": "Deluxe", "TITLE_ID": "CUSA13795"}))
        other = self.root / "other.pkg"
        other.write_bytes(pkg({"CATEGORY": "ac", "CONTENT_ID": "UP0000-CUSA00001_00-SOMEOTHERGAME001",
                               "TITLE_ID": "CUSA00001"}))
        added, problems = core.add_dlc([good, other], folder)
        self.assertEqual(added, 1)
        self.assertEqual(len(problems), 1)
        (folder / "broken.pkg").write_bytes(b"nope")
        found = core.scan_dlc(folder)
        self.assertEqual([(d["file"], d["ok"]) for d in found], [("a.pkg", True), ("broken.pkg", False)])
        self.assertEqual(found[0]["label"], "CTRDELUXEPACK001")
        self.assertEqual(core.split_list(" a, ,b "), ["a", "b"])

    def test_keyboard(self):
        self.assertEqual(core.key_name(0x39 + 8), "Space")       # evdev KEY_SPACE
        self.assertEqual(core.key_name(103 + 8), "Up")           # evdev KEY_UP
        self.assertEqual(core.key_name(42 + 8), "Left Shift")
        self.assertIsNone(core.key_name(110 + 8))                # Insert: the in-game menu
        self.assertEqual(core.key_label("Up"), "Up arrow")
        self.assertEqual(core.key_label("Keypad 5"), "Num 5")
        ini = core.Ini(self.root / "bbport.ini")
        core.bind_key(ini, "cross", "Return", replace=True)
        self.assertEqual(ini.get("key.cross"), "Return")
        for name in ("A", "B", "C", "D"):
            core.bind_key(ini, "cross", name, replace=False)
        self.assertEqual(ini.get("key.cross"), "A,B,C,D")  # up to 4, the oldest goes
        # Every default key and every name the table writes is one the runtime knows (SDL names).
        names = set(core.EVDEV_KEYS.values())
        for _input, _es, _en, fallback in core.KEY_INPUTS:
            self.assertTrue(set(core.split_keys(fallback)) <= names, fallback)

    def test_patches(self):
        folder = ROOT / "patches"
        patches = core.list_patches([folder, self.root / "none"])
        keys = [p["key"] for p in patches]
        self.assertIn("CrashTeamRacing.xml/60 FPS Unlock", keys)
        self.assertNotIn("CrashTeamRacing.xml/Resolution Patch (1080p for PS4 Pro)", keys)
        fps = next(p for p in patches if p["key"] == "CrashTeamRacing.xml/60 FPS Unlock")
        self.assertEqual(fps["ini_key"], "patch_60fps")
        ini = core.Ini(self.root / "bbport.ini")
        config = core.load_patch_config(self.root / "patches.json")
        core.set_patch(fps, True, ini, config)
        self.assertTrue(core.patch_on(fps, ini, config))
        self.assertEqual(ini.get("patch_60fps"), "1")
        extra = dict(key="mine.xml/X", default_on=True, ini_key=None)
        core.set_patch(extra, False, ini, config)
        self.assertEqual(config, {"enabled": [], "disabled": ["mine.xml/X"]})
        self.assertFalse(core.patch_on(extra, ini, config))
        core.save_patch_config(self.root / "patches.json", config)
        self.assertEqual(json.loads((self.root / "patches.json").read_text())["disabled"], ["mine.xml/X"])


if __name__ == "__main__":
    unittest.main()
