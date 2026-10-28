"""Pure interpretation of current, signed HoYoPlay native observations.

Check/repair uses the 2026-10-25 captured sequence. The literal 更新 command
comes from the official ZZZ PC update notice; its actual role, uniqueness,
pattern and operation subtree still have to match at runtime. Progress phases
are reported as observed labels, never guessed as download or installation.
"""
from dataclasses import dataclass
import math

from .game_launcher import GameLaunchError
from .hoyoplay_owner_family import valid_node_owner, node_owner_key
from .hoyoplay_native_observation import FIXED_GAME_EXECUTABLE, NativeHoYoPlaySampler


def fail(reason):
    raise GameLaunchError("HoYoPlay 当前原生证据不满足动作合同。", reason_code="zzz_launcher_" + reason)


@dataclass(frozen=True)
class UpdateScope:
    pid: int
    birth: int
    hwnd: int
    document_id: tuple
    container_id: tuple
    config_sha: str
    document_owner: tuple
    container_owner: tuple


def range_progress(bar):
    value = bar["uia"].get("rangeValue")
    if (bar["uia"].get("rangeValuePatternAvailable") is not True or not isinstance(value, dict)
            or any(type(value.get(k)) not in (float, int) or not math.isfinite(value[k]) for k in ("value", "minimum", "maximum"))
            or not value["minimum"] <= value["value"] <= value["maximum"] or value["minimum"] >= value["maximum"]):
        fail("progress_value_missing")
    return int(10000 * (value["value"] - value["minimum"]) / (value["maximum"] - value["minimum"]))


@dataclass(frozen=True)
class View:
    packet: dict
    document: dict
    nodes: tuple
    state: str
    progress: tuple | None

    def update_target(self):
        # The notice supplies this literal command, not UIA selector IDs. The
        # recorded main-control cluster is checked anew; a changed layout blocks.
        candidate = self.unique(self.find("Button", "更新") + self.find("Button", "下载"))
        target = self.target(candidate["name"], "Button")
        if self.packet.get("gameExecutablePresent") is not True:
            fail("game_binding_temporarily_unavailable")
        parent = self.nodes[target["parent"]]
        dx = self.find("CheckBox", "使用DX12启动")
        menu = [n for n in self.find("MenuItem", "") if n["parent"] == parent["index"]]
        if (parent["controlType"] not in {"ControlType.Group", "ControlType.Pane", "ControlType.Custom"}
                or len(dx) != 1 or dx[0]["parent"] != parent["index"] or len(menu) != 1
                or self.find("Button", "开始游戏") or self.find("ProgressBar", within=parent)):
            fail("update_control_scope_unqualified")
        p = self.packet["process"]
        scope = UpdateScope(p["pid"], p["createdAt"], self.packet["hwnd"],
            tuple(self.document["uia"]["runtimeId"]), tuple(parent["uia"]["runtimeId"]),
            self.packet["installationAssociation"]["configSha256"],
            node_owner_key(self.document["uia"]), node_owner_key(parent["uia"]))
        return target, scope

    def update_container(self, scope):
        if not isinstance(scope, UpdateScope):
            fail("update_scope_missing")
        p = self.packet["process"]
        if ((p["pid"], p["createdAt"], self.packet["hwnd"], tuple(self.document["uia"]["runtimeId"]),
             self.packet["installationAssociation"]["configSha256"])
                != (scope.pid, scope.birth, scope.hwnd, scope.document_id, scope.config_sha)):
            fail("update_identity_changed_no_replay")
        node = self.unique([n for n in self.nodes if tuple(n["uia"]["runtimeId"]) == scope.container_id
                           and self.below(n, self.document) and n.get("offscreen") is False])
        if (node_owner_key(self.document["uia"]) != scope.document_owner
                or node_owner_key(node["uia"]) != scope.container_owner):
            fail("update_identity_changed_no_replay")
        return node

    def update_progress(self, scope):
        container = self.update_container(scope)
        candidates = []
        # A phase name is an observed Text child of the operation's progress
        # Button. Do not read percentage/ETA from the changing Button name and
        # do not select the pause button's or supplementary region's bar.
        for bar in self.find("ProgressBar", within=container):
            parent = self.nodes[bar["parent"]]
            if parent["controlType"] != "ControlType.Button" or parent["parent"] != container["index"]:
                continue
            labels = [n["name"].strip() for n in self.find("Text", within=parent)
                      if n["parent"] == parent["index"] and n["name"].strip()
                      and not any(c.isdigit() for c in n["name"])]
            if labels == ["暂停"]:
                continue  # Root's captured secondary pause control.
            if len(labels) != 1 or len(labels[0]) > 64:
                fail("update_phase_label_unqualified")
            label = labels[0]
            if any(word in label.casefold() for word in ("失败", "错误", "暂停", "取消", "重试", "继续", "failed", "error", "paused", "cancel", "retry", "resume")):
                fail("update_interrupted_no_replay")
            candidates.append((bar, label))
        if len(candidates) != 1:
            fail("update_progress_scope_unqualified")
        bar, label = candidates[0]
        return ("official-phase:" + label, range_progress(bar))

    def below(self, node, ancestor):
        by_id = {n["index"]: n for n in self.nodes}
        for _ in range(13):
            if node["parent"] == ancestor["index"]:
                return True
            node = by_id.get(node["parent"])
            if node is None:
                return False
        fail("tree_invalid")

    def find(self, role, name=None, within=None):
        return [n for n in self.nodes if n["controlType"] == "ControlType." + role
                and (name is None or n["name"] == name) and n.get("offscreen") is False
                and self.below(n, within or self.document)]

    def unique(self, nodes):
        if len(nodes) != 1:
            fail("action_ambiguous_or_missing")
        return nodes[0]

    def target(self, name, role, within=None, pattern="invoke"):
        node = self.unique(self.find(role, name, within))
        field = "invokePatternAvailable" if pattern == "invoke" else "expandCollapsePatternAvailable"
        if node.get("enabled") is not True or node["uia"].get(field) is not True:
            fail("action_pattern_missing")
        return node

    def menu(self):
        # A named command must have the actual Menu as its ancestor, never a
        # similarly named news item or another document's menu.
        menus = self.find("Menu")
        menus = [m for m in menus if len(self.find("MenuItem", "检查更新", m)) == 1
                 and len(self.find("MenuItem", "一键修复", m)) == 1]
        return self.unique(menus)

    def opener(self):
        starts = self.find("Button", "开始游戏")
        if len(starts) != 2 or starts[0]["parent"] != starts[1]["parent"]:
            fail("ready_structure_missing")
        parent = starts[0]["parent"]
        dx = self.find("CheckBox", "使用DX12启动")
        if len(dx) != 1 or dx[0]["parent"] != parent:
            fail("ready_structure_missing")
        node = self.unique([n for n in self.find("MenuItem", "") if n["parent"] == parent])
        # The unnamed icon is authorized only through its explicit dropdown
        # pattern. Never Invoke an unidentified icon or use coordinates/IDs.
        if (node.get("enabled") is not True or node["uia"].get("expandCollapsePatternAvailable") is not True
                or node["uia"].get("expandCollapseState") != 0):
            fail("menu_expand_pattern_missing")
        return node


def interpret(packet, *, update_scope=None):
    if (not isinstance(packet, dict) or packet.get("truncated") is not False
            or packet.get("selectedDocumentCount") != 1 or not isinstance(packet.get("nonce"), str)):
        fail("incomplete_live_observation")
    process = packet.get("process")
    NativeHoYoPlaySampler._validate_processes([process])
    binding = packet.get("installationAssociation", {})
    if type(packet.get("gameExecutablePresent")) is not bool:
        fail("game_binding_presence_unknown")
    if packet["gameExecutablePresent"] is not True and update_scope is None:
        fail("game_binding_temporarily_unavailable")
    if (binding.get("primaryGame") != "nap_cn" or binding.get("gameExecutable") != str(FIXED_GAME_EXECUTABLE)
            or not isinstance(binding.get("configSha256"), str) or len(binding["configSha256"]) != 64):
        fail("game_binding_changed")
    nodes = packet.get("nodes")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 256:
        fail("tree_invalid")
    for i, node in enumerate(nodes):
        raw = node.get("uia", {})
        if (node.get("index") != i or type(node.get("parent")) is not int or not -1 <= node["parent"] < i
                or not valid_node_owner(raw, process, packet.get("hwnd"), root_node=node.get("parent") == -1)
                or not isinstance(raw.get("runtimeId"), list) or not 1 <= len(raw["runtimeId"]) <= 32
                or any(type(v) is not int for v in raw["runtimeId"])):
            fail("tree_identity_invalid")
    docs = [n for n in nodes if n.get("controlType") == "ControlType.Document" and n.get("offscreen") is False]
    if len(docs) != 1:
        fail("document_ambiguous")
    doc = docs[0]
    location = doc.get("documentLocation", {})
    if (location.get("origin") != "https://hyp-api.mihoyo.com" or location.get("path") != "/hypfe/index.html"
            or location.get("route") != "/nap_cn" or location.get("status") != "unqualified"
            or location.get("queryOmitted") is not True or doc.get("documentText") == "unknown"):
        fail("selected_game_unqualified")
    view = View(packet, doc, tuple(nodes), "unknown", None)
    container = view.update_container(update_scope) if update_scope is not None else None
    for node in nodes:
        if node.get("offscreen") is False and view.below(node, doc):
            if node.get("privateInput") is True or node.get("controlType") == "ControlType.Edit":
                fail("authentication_or_unknown_input")
            if ((node.get("dialog") is True or node.get("controlType") == "ControlType.Window")
                    and node["name"] not in {"提示", "一键修复提醒"}):
                fail("unrecognized_dialog")
    # Route + signed process + fixed file/config association + coherent current
    # control subtree form the selection evidence; the URL alone grants nothing.
    dialogs = view.find("Window") + view.find("Pane") + view.find("Group")
    named = [n for n in dialogs if n["name"] in {"提示", "一键修复提醒"}]
    # UIA may expose ARIA dialog as Window, Pane, Group or Custom. Accept only a
    # named scope with the exact recorded contents, never a generic button.
    named += [n for n in view.find("Custom") if n["name"] in {"提示", "一键修复提醒"}]
    if named:
        dialog = view.unique(named)
        if dialog["name"] == "提示" and len(view.find("Text", "当前已是最新版本，无需更新", dialog)) == 1:
            state = "check-complete"
        elif dialog["name"] == "一键修复提醒" and len(view.find("Button", "取消", dialog)) == 1:
            state = "repair-confirm"
        else:
            fail("unrecognized_dialog")
        # A disabled button may remain visible while an owned dialog settles.
        # Invocation availability is checked only by target(), before input.
        view.unique(view.find("Button", "知道了" if state == "check-complete" else "开始修复", dialog))
        return View(packet, doc, tuple(nodes), state, None)
    text = doc.get("documentText", "")
    if "正在解析下载内容，请稍候" in text:
        return View(packet, doc, tuple(nodes), "checking", None)
    labels = view.find("Text", "校验并修复")
    if labels:
        label = view.unique(labels)
        parent = nodes[label["parent"]]
        if parent["controlType"] != "ControlType.Button":
            fail("progress_scope_missing")
        if container is not None and not view.below(parent, container):
            fail("update_progress_scope_unqualified")
        bar = view.unique(view.find("ProgressBar", within=parent))
        progress = range_progress(bar)
        return View(packet, doc, tuple(nodes), "verifying", ("repair-basis-points", progress))
    if any(n["controlType"] == "ControlType.Button" and n.get("enabled") is False
           and n["name"] == "0 等待中" for n in nodes):
        return View(packet, doc, tuple(nodes), "waiting", None)
    starts = view.find("Button", "开始游戏")
    dx = view.find("CheckBox", "使用DX12启动")
    if (len(starts) == 2 and all(n.get("enabled") is True for n in starts)
            and len(dx) == 1 and starts[0]["parent"] == starts[1]["parent"] == dx[0]["parent"]):
        if packet["gameExecutablePresent"] is not True:
            fail("game_binding_temporarily_unavailable")
        return View(packet, doc, tuple(nodes), "ready", None)
    if view.find("Button", "更新") or view.find("Button", "下载"):
        view.update_target()
        return View(packet, doc, tuple(nodes), "update-available", None)
    if update_scope is not None:
        return View(packet, doc, tuple(nodes), "updating", view.update_progress(update_scope))
    fail("unrecognized_live_state")
