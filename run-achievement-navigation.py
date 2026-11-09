"""获准实机验证入口；仅发送普通按键，选到目标后停止。"""
import argparse
import json
import importlib.util
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent
def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
app = load("app", "rhythm-doctor-autoplay-app.py")
achievements = load("achievements", "rhythm-doctor-achievements.py")
parser = argparse.ArgumentParser()
parser.add_argument("--target")
parser.add_argument("--action")
parser.add_argument("--activate", action="store_true")
parser.add_argument("--activate-only", action="store_true")
parser.add_argument("--seconds", type=int, default=60)
args = parser.parse_args()
pid = app.runtime.game_pid()
binding = app.runtime.resolve_layout(app.api, pid, print)
scene = app.navigation.SceneReader(app.api, pid, binding["classes"])
keyboard = app.api.keyboard_mod.Keyboard(pid)
try:
    if args.activate or args.activate_only:
        keyboard.activate()
    if args.activate_only:
        print(json.dumps({"foreground": keyboard.is_foreground(), "scene_type": scene.snapshot()["scene_type"]}))
    elif args.action:
        snapshot = scene.snapshot()
        if args.action.startswith("achievement_"):
            assert (snapshot["scene_type"] == "scnLevelSelect" and not snapshot["paused"]
                    and not snapshot["dialogue"]["active"] and not snapshot["monitor"]["visible"]
                    and not snapshot["controlsAreLocked"] and snapshot["currentWardIndex"] != 11)
        achievements.execute(keyboard, args.action)
        time.sleep(2)
        print(json.dumps({"action": args.action, "snapshot": scene.snapshot(),
                          "achievements": achievements.read_status(scene, binding["identity"])}, ensure_ascii=False))
    else:
        assert args.target in app.navigation.LEVEL_ENTITIES
        nav = app.platinum.PlatinumNavigator()
        nav.target = args.target
        for _ in range(args.seconds):
            snapshot = scene.snapshot()
            if not keyboard.is_foreground():
                raise RuntimeError("游戏不在前台")
            if snapshot["scene_type"] == "scnGame" and snapshot["level"] != args.target:
                action, reason = nav.exit_current(snapshot, time.monotonic(), "实测切换目标关卡")
                print(json.dumps({"action": action, "reason": reason}, ensure_ascii=False), flush=True)
                if action:
                    keyboard.tap_key(action)
                    nav.action_sent(snapshot, action)
                time.sleep(1)
                continue
            if snapshot["scene_type"] != "scnLevelSelect":
                print(json.dumps(snapshot, ensure_ascii=False))
                break
            if snapshot["dialogue"]["active"]:
                action = "space" if snapshot["dialogue"]["choices"] <= 1 else None
                reason = "推进单选剧情"
            elif snapshot["monitor"]["visible"] and snapshot["monitor"]["levelToGo"] != args.target:
                action, reason = "escape", "关闭当前面板"
            else:
                action, reason = nav.menu(snapshot)
            print(json.dumps({"action": action, "reason": reason,
                              "selected": snapshot["entities"][snapshot["selectedEntityIndex"]]["id"]}, ensure_ascii=False), flush=True)
            if action:
                if not nav.permit(snapshot, action):
                    raise RuntimeError("导航无进展")
                keyboard.tap_key(action)
            time.sleep(1)
finally:
    keyboard.close()
    scene.close()
