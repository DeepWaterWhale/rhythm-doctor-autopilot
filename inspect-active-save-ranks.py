"""只读定位当前活动存档对象和路径缓存。"""
import importlib.util
import json
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parent
def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

api = load("save_api", "rhythm-doctor-live-autoplay.py")
nav = load("save_nav", "rhythm-doctor-navigation.py")
parser = load("save_parser", "managed-type-name-offsets.py")
layout = json.loads((ROOT / "app-runtime-layout.json").read_text(encoding="utf-8"))
scene = nav.SceneReader(api, layout["identity"][0], layout["classes"])
r = scene.reader
def read_dictionary(obj, string_keys=False, string_values=False):
    version = scene.get(obj, "_version")
    array = scene.get(obj, "_entries")
    count = scene.get(obj, "_count")
    if not 0 <= count <= 10000:
        raise ValueError("Dictionary count")
    # mono_class_get_element_class 静态反汇编确认读取 MonoClass + 0。
    entry_cls = r.u64(scene.cls(array))
    meta = scene.meta(entry_cls)
    stride = meta["instance_size"] - 16
    offsets = {f["name"]: f["offset"] - 16 for f in meta["fields"]}
    if stride != 24 or set(offsets) != {"hashCode", "next", "key", "value"}:
        raise ValueError("Dictionary entry layout")
    values = {}
    for i in range(count):
        entry = array + 32 + stride*i
        if r.i32(entry + offsets["hashCode"]) < 0:
            continue
        key_at = entry + offsets["key"]
        key = r.managed_string(r.u64(key_at), limit=65536) if string_keys else r.i32(key_at)
        value = r.u64(entry + offsets["value"])
        if string_values:
            value = r.managed_string(value, limit=65536)
        values[key] = value
    if version != scene.get(obj, "_version") or array != scene.get(obj, "_entries"):
        raise ValueError("Dictionary changed during read")
    return values, {"stride":stride,"fields":offsets,"entry_class":meta["name"]}

try:
    cache_path = ROOT / "active-save-classes.json"
    classes = {}
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached["identity"] == layout["identity"]:
            classes = cached["classes"]
    if not classes:
        names = ["Persistence", "PlayerPrefsJson"]
        asm = Path(r.path).with_name("Rhythm Doctor_Data") / "Managed" / "Assembly-CSharp.dll"
        offsets = parser.read_type_name_offsets(asm, names)["types"]
        anchor = layout["classes"]["scnGame"]
        image = r.u64(anchor + 0x40)
        anchor_name = r.u64(anchor + 0x48)
        wanted = {anchor_name + offsets[name]["delta_from_anchor"]: name for name in names}
        refs = r.scan([struct.pack("<Q", address) for address in wanted], alignment=8)
        if refs.get("truncated"):
            raise RuntimeError("类地址扫描截断")
        for ref in refs["hits"]:
            at = ref["address"] - 0x48
            try:
                name = wanted.get(r.u64(at + 0x48))
                if name and r.u64(at + 0x40) == image and int(scene.meta(at)["token"], 16) == int(offsets[name]["token"], 16):
                    if name in classes and classes[name] != at:
                        raise RuntimeError("多个类型候选:" + name)
                    classes[name] = at
            except (OSError,ValueError,IndexError):
                continue
        if set(classes) != set(names):
            raise RuntimeError("未找到类型:" + str(classes))
        cache_path.write_text(json.dumps({"identity":layout["identity"],"classes":classes},indent=2),encoding="utf-8")
    slot = scene.get(None, "currentSlotIndex", classes["Persistence"])
    result = {"identity":layout["identity"],"classes":classes,"slot":slot,"objects":{}}
    for field in ("files", "filesPath"):
        obj = scene.get(None, field, classes["PlayerPrefsJson"])
        result["objects"][field] = api.objmod.inspect(r, scene.cls(obj), obj)
    files, files_layout = read_dictionary(scene.get(None,"files",classes["PlayerPrefsJson"]))
    paths, paths_layout = read_dictionary(scene.get(None,"filesPath",classes["PlayerPrefsJson"]),string_values=True)
    prefs = files[slot+1]
    prefs_type = scene.get(prefs,"fileType")
    if prefs_type != slot+1 or scene.get(None,"currentSlotIndex",classes["Persistence"]) != slot:
        raise ValueError("Active slot changed")
    saved, saved_layout = read_dictionary(scene.get(prefs,"dict"),string_keys=True)
    ranks = {k:r.managed_string(p,limit=256) for k,p in saved.items() if k.startswith('Level_') and k.endswith('_rank')}
    result.update(active_path=paths[slot+1],active_prefs=hex(prefs),ranks=ranks,
                  dictionary_layouts={"files":files_layout,"paths":paths_layout,"save":saved_layout})
    metadata, _ = read_dictionary(scene.get(None,"levelMetadata",layout["classes"]["GC"]))
    result["levels"] = [{"level":nav.LEVEL_NAMES[k],"type":scene.get(v,"type"),"id":scene.get(v,"id")} for k,v in metadata.items()]
    (ROOT / "active-save-inspection.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k!="objects"},ensure_ascii=False))
finally:
    scene.close()
