"""运行时：装配遥测、事件引擎、仲裁、推送、记忆。"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from ..adapters.llm_client import LLMProvider
from ..adapters.push_sender import CHAT, CRITICAL, IMPORTANT, PushSender
from ..adapters.telemetry_client import TelemetryReader
from ..adapters.telemetry_installer import TelemetryInstaller
from ..catgirl.bridge import CatgirlBridge
from .arbiter import Arbiter
from .companion import Companion
from .config_model import PawpilotConfig
from .event_catalog import BROADCAST_FREQUENCY_MULTIPLIERS
from .event_engine import EventEngine, TruckEvent
from .knowledge import KnowledgeBase
from .ledger import Ledger
from .level_celebrate import LevelCelebrate
from .map_kb import MapKnowledge
from .memory import MemoryStore
from .mood import Persona
from .photo_album import PhotoAlbum
from .pilot import CatPilot
from .places import PlaceBook
from .proactive import Proactive
from .profile import DriverProfile
from .radio import RadioDJ
from .recall import Recall
from .route_planner import RoutePlanner
from .safety_guard import SafetyGuard
from .scene_chat import SceneChat
from .small_talk import SmallTalk
from .templates import EmotionRenderer
from .trip_summary import TripSummary

ACTIVITY_TITLES = {
    "speeding": "超速",
    "hard_brake": "急刹",
    "crash": "车祸",
    "job_start": "接单",
    "job_delivered": "到货",
    "job_cancelled": "取消任务",
    "fine": "罚款",
    "tollgate": "过收费站",
    "refuel": "加油",
    "trailer_attach": "挂上挂车",
    "trailer_detach": "卸下挂车",
    "time_warning": "交付时间警告",
    "time_over": "超时",
    "low_fuel": "低油量",
    "game_start": "进入游戏",
    "game_end": "退出游戏",
    "trip_progress": "行程进度",
    "distance_mark": "距离预告",
    "time_relaxed": "时间充裕",
    "time_tight": "时间紧张",
    "early_arrival": "提前到货",
    "cargo_damage": "货物受损",
}

# 内容类事件：一次性 / 长间隔的真实内容，直接 respond，不参与仲裁限流。
# 实测教训：过仲裁时它们会被"场景门控"或限流单槽窗口吞掉，主人反馈
# "任务分段播报、时间提醒、收费站从没响过"。
CONTENT_EVENTS = frozenset({
    "trip_progress",   # 1/3、1/2、2/3 分段播报
    "distance_mark",   # 剩余距离锚点预告
    "time_warning",    # 交付时间提醒
    "time_over",       # 超时
    "time_tight",      # 时间紧张
    "time_relaxed",    # 时间宽裕
    "early_arrival",   # 提前到达
    "tollgate",        # 过收费站
    "trailer_attach",  # 挂上挂车
    "trailer_detach",  # 卸下挂车
})

# 紧急事件：立即播、并且让闲聊整体安静一会儿（"紧急先走，闲聊排队"）
CRITICAL_EVENTS = frozenset({
    "crash",            # 事故
    "wheels_airborne",  # 翻车/离地
    "engine_stalled",   # 行驶中熄火
})


def _memory_mirror_path(plugin: Any) -> Optional[str]:
    """记忆的本地 JSON 兜底路径（宿主 store 出问题时还能救回来）。"""
    try:
        return str(plugin.data_path("memory_backup.json"))
    except Exception:                       # noqa: BLE001
        return None


class PawpilotRuntime:
    """插件运行时。"""

    def __init__(self, plugin: Any, config: PawpilotConfig) -> None:
        self.plugin = plugin
        self.cfg = config
        host_persona = getattr(plugin, "persona", None)
        self.persona = Persona(host_persona)
        self.catgirl = CatgirlBridge()
        self.llm = LLMProvider()
        self.emotion = EmotionRenderer(self.persona, llm=self.llm)
        self.engine = EventEngine(config)
        self.safety = SafetyGuard(config)
        self.arbiter = Arbiter(config, self.safety)
        self.arbiter.broadcast_categories = dict(config.broadcast_categories)
        self.arbiter.broadcast_frequency = config.broadcast_frequency
        self.push = PushSender(plugin, dry_run=config.dry_run, on_push=self._note_push,
                               gap_chat=float(getattr(config, "companion_gap_s", 90.0) or 90.0))
        self.memory = MemoryStore(plugin.store, mirror_path=_memory_mirror_path(plugin))
        self.recall = Recall(self.memory)
        self.ledger = Ledger(self.memory)
        self.trip_summary = TripSummary({})
        self.knowledge = KnowledgeBase()
        self.map_kb = MapKnowledge()
        self.proactive = Proactive(config, map_kb=self.map_kb)
        # 内容层（2026-10 玩法扩充）：地点簿（自学习地名坐标）+ 猫娘电台
        self.places = PlaceBook(self.memory)
        self.radio = RadioDJ(config)
        self.small_talk = SmallTalk(persona=self.persona)
        # 陪玩维度引擎（2026-10 主人定调"陪玩是方方面面的"）：十几个维度轮转，
        # 每条只投事实行，措辞交给宿主 LLM 按人设演绎
        self.companion = Companion(config, map_kb=self.map_kb)
        self.pilot = CatPilot(persona=self.persona, map_kb=self.map_kb, cfg=config)
        self.profile = DriverProfile(self.memory)
        self.scene_chat = SceneChat()
        self.route_planner = RoutePlanner(self.knowledge)
        self.level_celebrate = LevelCelebrate(self.memory)
        self.photo_album = PhotoAlbum(plugin)
        self.telemetry_installer = TelemetryInstaller(
            plugin_rel=config.telemetry_plugin_rel,
            bundle_rel=config.telemetry_bundle_rel)
        self.telemetry_install_state: Dict[str, str] = {}
        self._game_dir: Optional[str] = None
        self.ocr_regions = self._load_ocr_regions()
        self.hud_ocr = None  # 惰性：OCR 可用时创建
        self._pending_photo_scene: str = ""
        self._pending_photo_until: float = 0.0
        self._ocr_interval = float(self.ocr_regions.get("scan_interval_s", 240))
        self._last_ocr_at = 0.0
        self._tick_task: Optional[asyncio.Task] = None
        self._propose_task: Optional[asyncio.Task] = None
        self._bg_thread: Optional[Any] = None
        self._bg_stop: Optional[Any] = None
        self._bg_loop_ref: Optional[Any] = None
        self._job_crashes = 0
        self._job_speedings = 0
        self._job_hard_brakes = 0
        self._job_refuels = 0
        self._job_fines = 0.0
        self._job_tolls = 0.0
        self._job_fuel_avg_sum = 0.0
        self._job_fuel_avg_n = 0
        self._job_start_ts = 0.0
        self._job_night = 0
        self._trip_start: Optional[TruckEvent] = None
        self._last_snap: Any = None
        self._game_running: bool = False
        self._activity: list = []
        self._bg_tasks: set = set()

    def _note_push(self) -> None:
        """任何一条推送落地时打时间戳：陪聊要给它让路（别把事件内容挤掉）。"""
        self._last_push_at = time.time()

    def _spawn(self, coro) -> None:
        """创建后台任务并跟踪，防泄漏；异常打日志。

        必须在 runtime 自己的后台循环（_bg_loop）里调用；宿主 lifecycle/entry
        的 loop 与后台线程不同，跨 loop create_task 会抛 RuntimeError
        （照 neko_fishpower 线程改造）。
        """
        loop = self._bg_loop_ref
        if loop is None or not loop.is_running():
            self.plugin.logger.warning("_spawn 无后台循环，丢弃任务")
            return
        task = asyncio.run_coroutine_threadsafe(coro, loop)
        self._bg_tasks.add(task)

        def _log_err(t):
            try:
                t.result()
            except (asyncio.CancelledError, Exception) as exc:
                if not isinstance(exc, asyncio.CancelledError):
                    self.plugin.logger.warning("bg task error: %s", exc)
        task.add_done_callback(_log_err)
        task.add_done_callback(self._bg_tasks.discard)

    def _ui_settings_path(self):
        from pathlib import Path
        return Path(__file__).resolve().parent.parent / "data" / "config" / "ui_settings.json"

    def _ui_settings(self) -> dict:
        """读 ui_settings.json，缺失返回空。"""
        import json
        try:
            data = json.loads(self._ui_settings_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    async def settings_save(self) -> None:
        """持久化面板可调设置到 data/config/ui_settings.json（原子写）。"""
        import json
        import os
        data = self._ui_settings()
        data.update({
            "dry_run": self.cfg.dry_run,
            "voice_styles": list(self.persona.voice_styles),
            "broadcast_frequency": self.arbiter.broadcast_frequency,
            "broadcast_categories": dict(self.arbiter.broadcast_categories),
        })
        path = self._ui_settings_path()
        tmp = path.with_suffix(".json.tmp")
        try:
            # data/config 可能整个缺失（安装包未带 data/ 或用户手动清过），
            # 旧版这里不建目录 → 写 .tmp 直接 OSError → 面板所有开关"存了等于没存"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            self.plugin.logger.warning("settings_save failed: %s", exc)

    async def settings_load(self) -> None:
        """启动时从 data/config/ui_settings.json 恢复面板设置。"""
        saved = self._ui_settings()
        if not saved:
            return
        style = saved.get("voice_styles")
        if isinstance(style, list) and style:
            self.persona.set_voice_styles(style)
        else:
            # 兼容旧版单值 voice_style
            old = saved.get("voice_style")
            if old:
                self.persona.set_voice_style(old)
        if "dry_run" in saved:
            self.set_dry_run(bool(saved["dry_run"]))
        if saved.get("broadcast_frequency"):
            self.set_frequency(saved["broadcast_frequency"])
        cats = saved.get("broadcast_categories")
        if isinstance(cats, dict):
            for k in self.arbiter.broadcast_categories:
                if k in cats:
                    self.arbiter.broadcast_categories[k] = bool(cats[k])

    def _load_ocr_regions(self) -> dict:
        import json
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "data" / "config" / "ocr_regions.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def set_dry_run(self, value: bool) -> None:
        self.cfg.dry_run = bool(value)
        self.push.dry_run = bool(value)

    def apply_config(self, config: PawpilotConfig) -> None:
        """配置热更新（config_change 生命周期）。"""
        self.cfg = config
        self.push.dry_run = config.dry_run
        self.arbiter.broadcast_categories = dict(config.broadcast_categories)
        self.arbiter.broadcast_frequency = config.broadcast_frequency
        self.engine.apply_config(config)

    def pause(self) -> None:
        self.safety.pause()

    def resume(self) -> None:
        self.safety.resume()

    def set_frequency(self, frequency: str) -> bool:
        from .event_catalog import BROADCAST_FREQUENCIES
        if frequency not in BROADCAST_FREQUENCIES:
            return False
        self.arbiter.broadcast_frequency = frequency
        return True

    def set_category(self, category: str, enabled: bool) -> bool:
        if category not in self.arbiter.broadcast_categories:
            return False
        self.arbiter.broadcast_categories[category] = bool(enabled)
        return True

    def llm_config(self) -> dict:
        """读 LLM 配置（ui_settings 的 llm 段）。"""
        data = self._ui_settings()
        llm = data.get("llm") if isinstance(data, dict) else {}
        return llm if isinstance(llm, dict) else {}

    async def save_llm_config(self, config: dict) -> bool:
        """保存 LLM 配置并热应用（配置优先，未配置降级模板）。"""
        import json
        import os
        data = self._ui_settings()
        cfg = {k: str(config.get(k, "") or "").strip()
               for k in ("provider", "model", "api_key", "base_url")}
        data["llm"] = cfg
        path = self._ui_settings_path()
        tmp = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            self.plugin.logger.warning("save_llm_config failed: %s", exc)
            return False
        self._wire_llm()
        return True

    async def start(self) -> Dict[str, Any]:
        if not self.cfg.enabled:
            return {"status": "disabled"}
        await self.memory.load()
        self.profile.load()
        self.level_celebrate.load()
        await self.settings_load()
        self._wire_llm()
        templates = self.emotion._short_lines
        self.trip_summary = TripSummary(templates)
        self.engine.on_event(self._on_event)
        # 后台循环必须跑在独立 daemon 线程里：宿主 lifecycle startup 用
        # asyncio.run()（临时 loop），create_task 的后台任务会在返回后被
        # 取消（照 neko_fishpower 线程改造，进度/事件播报依赖 tick_loop）。
        import threading
        self._bg_stop = threading.Event()
        self._bg_thread = threading.Thread(
            target=self._bg_runner, daemon=True, name="pawpilot-bg")
        self._bg_thread.start()
        self._game_dir = self.telemetry_installer.detect_game_dir()
        self.telemetry_install_state = self.telemetry_installer.install(self._game_dir)
        return {"status": "ready", "telemetry": self._probe_telemetry(),
                "dry_run": self.cfg.dry_run, "map": self.map_kb.snapshot(),
                "llm": self.llm.snapshot()}

    def _bg_runner(self) -> None:
        """后台线程入口：自己的事件循环跑 tick/propose。"""
        try:
            asyncio.run(self._bg_loop())
        except Exception as exc:
            self.plugin.logger.exception("后台循环异常退出: %s", exc)

    async def _bg_loop(self) -> None:
        """后台主循环：tick + propose（同线程内任务，照 neko_fishpower）。"""
        self._bg_loop_ref = asyncio.get_running_loop()
        self._tick_task = asyncio.create_task(self._tick_loop())
        self._propose_task = asyncio.create_task(self._propose_loop())
        try:
            while not self._bg_stop.is_set():
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        finally:
            for task in (self._tick_task, getattr(self, "_propose_task", None)):
                if task:
                    task.cancel()
            for task in list(self._bg_tasks):
                task.cancel()
            self._bg_loop_ref = None

    def _wire_llm(self) -> None:
        """LLM 配置：ui_settings 里配了 → 自建 LLM 优先；没配 → 模板降级。

        配置来源：data/config/ui_settings.json 的 llm 段
        （provider/model/api_key/base_url）。
        """
        data = self._ui_settings()
        llm = data.get("llm") if isinstance(data, dict) else None
        if isinstance(llm, dict):
            provider = str(llm.get("provider", "") or "")
            model = str(llm.get("model", "") or "")
            api_key = str(llm.get("api_key", "") or "")
            base_url = str(llm.get("base_url", "") or "")
            self.llm.set_client(provider, model, api_key, base_url)
            if provider and model:
                self.plugin.logger.info("已配置 LLM: %s/%s", provider, model)
            else:
                self.plugin.logger.info("未配置 LLM，情感渲染用模板兜底")
        else:
            self.llm.set_client("", "")
            self.plugin.logger.info("未配置 LLM，情感渲染用模板兜底")
        return {"status": "ready", "telemetry": self._probe_telemetry(),
                "dry_run": self.cfg.dry_run, "map": self.map_kb.snapshot()}

    def install_telemetry(self) -> Dict[str, str]:
        """手动触发遥测文件写入（复用已探测的游戏目录）。"""
        if not self._game_dir:
            self._game_dir = self.telemetry_installer.detect_game_dir()
        self.telemetry_install_state = self.telemetry_installer.install(self._game_dir)
        return self.telemetry_install_state

    def pilot_accept(self) -> str:
        """同意接管：用当前快照对齐注入目标，防首个 tick 误判抢控。"""
        return self.pilot.accept(self._last_snap)

    async def shutdown(self) -> None:
        self.pilot.release(reason="shutdown")
        if self._bg_stop is not None:
            self._bg_stop.set()
        if self._bg_thread is not None and self._bg_thread.is_alive():
            self._bg_thread.join(timeout=5.0)
        self._bg_thread = None
        self._bg_loop_ref = None

    async def dashboard_state(self) -> Dict[str, Any]:
        """主面板状态（@ui.context dashboard）。"""
        s = self._last_snap
        persona = self.persona.snapshot()
        arb = self.arbiter.snapshot()
        base = {
            "connected": s is not None and s.sdk_active,
            "game_running": self._game_running,
            "dry_run": self.cfg.dry_run,
            "telemetry_install": self.telemetry_install_state,
            "game_dir": self._game_dir or "",
            "pilot": self.pilot.snapshot(),            "mood": persona.get("mood", ""),
            "catgirl": {
                "name": self.persona.name,
                "user_call": self.persona.user_call,
                "traits": self.persona.traits,
                "description": self.persona.description,
                "voice_style": self.persona.voice_style,
                "voice_label": self.persona.snapshot().get("voice_label", ""),
                "voice_styles": list(self.persona.voice_styles),
                "voice_labels": self.persona.snapshot().get("voice_labels", []),
            },
            "memory": self.memory.snapshot(),
            "top_cities": self._top_entries("cities"),
            "top_cargos": self._top_entries("cargos"),
            "scenario": arb.get("scenario", "IDLE"),
            "broadcast_frequency": arb.get("broadcast_frequency", "standard"),
            "broadcast_categories": arb.get("broadcast_categories", {}),
            "safety": arb.get("safety", {}),
            "decision_log": self.arbiter.decision_snapshot(),
            "companion": self.companion.snapshot(),
            "ledger": self.ledger.month_summary(),
            "profile": self.profile.snapshot(),
            "level": self.level_celebrate.snapshot().get("level", 0),
            "route_options": self.route_planner.snapshot().get("options", []),
            "ocr": self.hud_ocr.snapshot() if self.hud_ocr else {"available": False},
            "map": self.map_kb.snapshot(),
            "photos": self.photo_album.snapshot(),
            "road_level": "",
            "nearest_service": "",
            # 任务区默认值（未驾驶/主菜单时清空显示）
            "on_job": False,
            "cargo": "",
            "city_src": "",
            "city_dst": "",
            "progress_percent": 0,
            "remaining_km": 0,
            "cargo_damage": 0,
            "speed_kmh": 0,
            "fuel_percent": 0,
            "truck": "",
            "world": {"x": 0, "z": 0},
        }
        if s is None:
            return base
        service = self.map_kb.nearest_facility(s.world_x, s.world_z, "service")
        base.update({
            "road_level": self.map_kb.road_level(s.speed_limit_kmh),
            "nearest_service": (f"{service['name']} {service['distance_km']:.0f} km"
                                if service else "（地图数据未加载）"),
        })
        base.update({
            "speed_kmh": round(s.speed_kmh, 1),
            "speed_limit_kmh": round(s.speed_limit_kmh, 1),
            "is_speeding": s.is_speeding,
            "fuel_percent": round(s.fuel_percent, 1),
            "fuel_range_km": round(s.fuel_range_km, 1),
            "power_type": s.power_type,
            "on_job": s.on_job,
            "cargo": s.cargo,
            "city_src": s.city_src,
            "city_dst": s.city_dst,
            "truck": f"{s.truck_brand} {s.truck_name}",
            "progress_percent": round(s.trip_progress_percent or 0, 1),
            "remaining_km": round(s.route_remaining_km, 1),
            "delivery_remaining_min": s.delivery_remaining_min,
            "damage": round(s.max_damage * 100, 1),
            # 面板/排查用：给出"生效货损"（两路取大）与两路原始值，便于现场
            # 判断 SCS SDK 到底把实时货损写在 job 还是 trailer 通道
            "cargo_damage": round(s.cargo_damage, 3),
            "cargo_damage_job": round(s.job_cargo_damage, 3),
            "cargo_damage_trailer": round(s.trailer_cargo_damage, 3),
            # 碰撞校准用：面板实时显示加速度合成值（g），据此设 crash_accel_g
            "accel_g": round(s.accel_g, 2),
            # 2026-10 新接上的遥测：档位/灯光/机务/地图比例尺
            "gear": s.gear,
            "gear_dashboard": s.gear_dashboard,
            "lights": {
                "low_beam": s.lights_low_beam, "parking": s.lights_parking,
                "brake": s.lights_brake, "hazard": s.lights_hazard,
                "beacon": s.lights_beacon, "reverse": s.lights_reverse,
                "blinker_left": s.blinker_left, "blinker_right": s.blinker_right,
            },
            "mech": {
                "air_pressure": round(s.air_pressure, 1),
                "brake_temp": round(s.brake_temperature, 1),
                "oil_temp": round(s.oil_temperature, 1),
                "water_temp": round(s.water_temperature, 1),
                "battery_v": round(s.battery_voltage, 1),
                "wheels_on_ground": s.wheels_on_ground,
                "warnings": list(s.active_warnings),
            },
            "map_scale": round(s.map_scale, 1),
            "in_city": s.is_city,
            "crash_accel_g": round(self.cfg.crash_accel_g, 2),
            "recent_activity": self._activity[:20],
            "world": {"x": round(s.world_x, 1), "z": round(s.world_z, 1)},
        })
        return base

    def _top_entries(self, kind: str) -> list:
        """记忆 top 条目（带 key 字段给面板展示）。

        注意：MemoryStore 里存的是 {名字: {count, weight, ...}}，`best()` 只返回
        值列表、丢了名字。旧写法 `for k, v in self.memory.best(...)` 是拿字典的
        key 去解包 → 记忆一多就 ValueError: too many values to unpack，
        整个 dashboard/get_panel_state 全崩（面板永远停在「加载中」）。
        """
        try:
            store = self.memory.query(kind) or {}
        except Exception:
            return []
        items = [{"key": k, **v} for k, v in store.items()
                 if isinstance(v, dict) and not v.get("archived")
                 and not str(k).startswith("route_")]
        items.sort(key=lambda e: float(e.get("weight", 0) or 0), reverse=True)
        return items[:5]

    async def memory_state(self) -> Dict[str, Any]:
        """记忆面板状态。"""
        return {
            "counts": self.memory.snapshot(),
            "top_cities": self._top_entries("cities"),
            "top_cargos": self._top_entries("cargos"),
            "recent_events": self.memory.best("events", 8),
        }

    def _probe_telemetry(self) -> str:
        r = TelemetryReader()
        ok = r.open()
        if ok:
            r.close()
        return "connected" if ok else "game-not-running"

    async def _tick_loop(self) -> None:
        r = TelemetryReader()
        while True:
            try:
                if not r.open():
                    self._game_running = False
                    self._last_snap = None
                    await asyncio.sleep(2.0)
                    continue
                self._game_running = True
                try:
                    while True:
                        s = r.snapshot()
                        # 事件检测持续运行：急刹/超速/撞车不依赖任务状态
                        self.arbiter.update_scenario(s)
                        self.proactive.update(s)
                        self.small_talk.update(s)
                        self.engine.feed(s)
                        self.persona.mood.decay_all()
                        # 猫娘智驾：dry_run 时禁止任何输入注入（试运行只跑链路）
                        if not self.cfg.dry_run:
                            pilot_msg = self.pilot.tick(s)
                            if pilot_msg:
                                # 交还/危险退出：走 respond（她要解释"为什么还你"）
                                self._spawn(self.push.push_fact(
                                    self.emotion.custom_fact(pilot_msg, "proactive")))
                                self._last_pilot_handback = time.time()
                        else:
                            self.pilot.tick_dry(s)
                        # 只要 SDK 活跃就保留快照：主菜单/未接管时清空（避免 0 值
                        # 被当成真实读数），但在游戏里停车休息也应该能陪聊
                        # （2026-10：旧条件要求"有任务或车速>5"，服务区歇脚就没人陪了）
                        if s.sdk_active:
                            self._last_snap = s
                        else:
                            self._last_snap = None
                        # 推送车道排空：优先级队列里攒下的话，车道空了就放一条出来
                        await self.push.drain()
                        await asyncio.sleep(self.cfg.poll_interval_s)
                except Exception:
                    r.close()
                    await asyncio.sleep(2.0)  # 退避，防快照异常死循环饿死事件循环
            except asyncio.CancelledError:
                r.close()
                raise
            except Exception:
                await asyncio.sleep(2.0)

    async def _propose_loop(self) -> None:
        """L3 主动提议 + L4 闲聊 + 里程碑（低频）。

        去话痨化原则（2026-10 用户定调）：**每条主动输出都应伴随一个动作或状态
        变化**，否则不发。所以这里只保留：接力驾驶提议（她要接管方向盘）、
        油量/休息等可执行提醒、途经/到达等真实事件内容；纯陪聊频率大幅下调
        （电台 30 分钟一次、定位叙事 15 分钟一次）。
        """
        import time as _time
        while True:
            try:
                await asyncio.sleep(15.0)
                now = _time.time()
                # 记忆衰减（每 10 分钟）：weight 递减，低于下限归档
                if now - getattr(self, "_last_mem_decay", 0) >= 600:
                    self._last_mem_decay = now
                    self.memory.decay()
                    await self.memory.save()
                snap = self._last_snap
                if snap is None or not snap.sdk_active:
                    continue
                # 里程碑（优先，只报一次）
                topic = self.small_talk.milestone_topic()
                if topic:
                    await self.push.push_direct(self.persona.polish(topic))
                    continue
                # 画面畅聊：OCR 采样（每 10 分钟一次）
                if now - self._last_ocr_at >= self._ocr_interval:
                    self._last_ocr_at = now
                    photo_ask, scene_topic = await self._ocr_scan()
                    if photo_ask:
                        self._spawn(self.push.push_fact(
                            self.emotion.custom_fact(photo_ask, "photo"),
                            priority=CHAT))
                        continue
                    if scene_topic:
                        await self.push.push_direct(self.persona.polish(scene_topic),
                                                    priority=CHAT)
                        continue
                # ── 接力驾驶提议：开阔路段主动提出"我来开一段"（有动作，不是话）
                if (getattr(self.cfg, "pilot_relay_enabled", True)
                        and self.pilot.state == "idle" and self.pilot.available
                        and snap.on_job and not snap.paused
                        and snap.is_open_road and snap.speed_kmh > 40
                        and now - getattr(self, "_last_pilot_offer", 0) > 900
                        and now - getattr(self, "_last_pilot_handback", 0) > 600):
                    self._last_pilot_offer = now
                    self.pilot.offer()
                    await self.push.push_fact(
                        "[接力驾驶] 现在是开阔路段（郊外/高速、车速 "
                        f"{snap.speed_kmh:.0f} km/h），猫娘想替你开一段让你歇会儿。"
                        "请以猫娘语气问一句要不要她来开（提示可用面板「猫娘智驾·同意」"
                        "或直接说一声）。不要提系统/工具")
                    continue
                # 途经已知地点 → 顺手介绍这个地方（地点簿自学习，越开越准）
                if getattr(self.cfg, "content_passing_places", True) and not snap.paused:
                    place = self.places.passing(
                        snap.world_x, snap.world_z, now,
                        radius_km=float(getattr(self.cfg, "passing_radius_km", 6.0) or 6.0),
                        cooldown_s=float(getattr(self.cfg, "passing_cooldown_s", 1800.0) or 1800.0),
                    )
                    if place:
                        await self.push.push_fact(self._passing_place_fact(place),
                                                  priority=CHAT)
                        continue
                # 猫娘电台（按时段/天气/驾驶时长挑节目；内容交宿主 LLM 演绎）
                if getattr(self.cfg, "content_radio_dj", True):
                    driving_since = 0.0
                    if self.proactive._drive_start_ts:
                        driving_since = now - self.proactive._drive_start_ts
                    weather = getattr(self, "_last_weather_hint", "")
                    brief = self.radio.pick(snap, now, driving_since=driving_since,
                                            station_hint=getattr(self, "_radio_station_hint", ""),
                                            weather=weather)
                    if brief:
                        await self.push.push_fact(brief, priority=CHAT)
                        continue
                # ── 陪玩维度引擎（主通道）──────────────────────────────
                # 2026-10 主人定调"陪玩是方方面面的"：旧版把主动输出收得只剩安全
                # 提醒，自由驾驶时更几乎全被门控挡掉，玩起来不像有人陪。
                # 现在由十几个维度轮转（路况/进城/天气/夜路/这单货/行程/车况/
                # 关心/巡航/晨昏/生活闲聊/反问/回忆/风景），每维独立冷却，
                # 角度轮换避免车轱辘话；类别挂面板开关，频率乘面板倍率。
                # 陪聊给事件让路：刚有真实内容（分段/收费站/加油站/事故…）出去过，
                # 就先闭嘴，别把主人的注意力抢走（实测每分钟一条闲聊会盖住事件类）
                quiet_s = float(getattr(self.cfg, "companion_event_quiet_s", 45.0) or 45.0)
                if now - getattr(self, "_last_push_at", 0.0) < quiet_s:
                    pass
                else:
                    gap_mul = BROADCAST_FREQUENCY_MULTIPLIERS.get(
                        self.arbiter.broadcast_frequency, 1.0)
                    cctx = self.companion.build_context(
                        snap,
                        drive_min=((now - self.proactive._drive_start_ts) / 60.0
                                   if self.proactive._drive_start_ts else 0.0),
                        place=self.places.nearest(snap.world_x, snap.world_z, max_km=8.0),
                        odometer_km=snap.odometer_km,
                        trips=int((self.profile.snapshot().get("stats") or {}).get("trips", 0) or 0),
                        top_city=self._top_city_name(),
                        rain_confirmed=(getattr(self, "_last_weather_hint", "") == "雨"),
                        remark=self.recall.recent_remark(),
                    )
                    picked = self.companion.next_fact(
                        snap, cctx, now=now,
                        categories=self.arbiter.broadcast_categories,
                        gap_multiplier=gap_mul)
                    if picked:
                        self.plugin.logger.info(
                            "[陪玩] %s | %s", picked["id"],
                            picked["fact"].splitlines()[0][:60])
                        if picked["id"] == "remark":
                            self.recall.mark_remark_spoken()
                        await self.push.push_fact(
                            self.emotion.custom_fact(picked["fact"], "proactive"),
                            priority=CHAT)
                        continue
                # L3 主动提议（服务区/加油/疲劳/夜间…）
                # 放宽门控：只要在任务中且未暂停就允许。旧版限定
                # DRIVING/URBAN/HIGHWAY，而停车等灯时场景被判成 DELIVERY
                # → 整段提议被跳过（这也是"地图信息没反应"的原因之一）。
                if snap.on_job and not snap.paused:
                    propose = self.proactive.propose(now)
                    if propose:
                        await self.push.push_fact(
                            self.emotion.custom_fact(propose, "proactive"))
                        continue
                    sa_advice = self.route_planner.service_area_advice(snap)
                    if sa_advice and now - getattr(self, "_last_sa_advice", 0) > 1200:
                        self._last_sa_advice = now
                        await self.push.push_fact(
                            self.emotion.custom_fact(sa_advice, "proactive"))
                        continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 别静默吞：这个循环一挂，陪玩就整个哑掉，而面板上看不出任何异常
                self.plugin.logger.warning("[陪玩循环] 本轮异常: %s: %s",
                                           type(exc).__name__, exc)
                await asyncio.sleep(15.0)

    async def _ocr_scene_topic(self) -> Optional[str]:
        """OCR 截屏识别 → 普通场景话题（美景不在这里处理，见 _ocr_scan）。"""
        ask, topic = await self._ocr_scan()
        return topic if not ask else None

    async def _ocr_scan(self) -> tuple:
        """一次截屏识别，返回 (美景拍照问句, 普通场景话题)。

        2026-10 主人定调："识别到好风景要询问要不要拍照"——所以美景不再自动拍，
        改成先问一句，主人答应（面板按钮或直接说"拍吧"）才真的拍。
        """
        if self.hud_ocr is None:
            from ..adapters.hud_ocr import HudOcr
            self.hud_ocr = HudOcr(self.plugin)
            if not self.hud_ocr.is_available():
                return "", ""
        text = await asyncio.to_thread(self.hud_ocr.read_text)
        if not text:
            self.plugin.logger.info("[OCR] 没读到文字（可用=%s 错误=%s）",
                                    self.hud_ocr.is_available(),
                                    (self.hud_ocr.snapshot() or {}).get("error", ""))
            return "", ""
        self.plugin.logger.info("[OCR] 读到 %d 字: %s",
                                len(text), " ".join(text.split())[:120])
        # 天气线索（OCR 里出现雨/雪/雾就记一笔）——供猫娘电台与路况叙述引用
        low = text.lower()
        for kw, label in (("rain", "雨"), ("雨", "雨"), ("snow", "雪"),
                          ("雪", "雪"), ("fog", "雾"), ("雾", "雾")):
            if kw in low:
                self._last_weather_hint = label
                break
        # 等级检测：OCR 识别到数字等级 → 庆祝（这条是重要内容）
        level = self._extract_level(text)
        if level:
            celebrate = self.level_celebrate.update(level)
            if celebrate:
                return "", celebrate
        # 美景检测 → 问她要不要拍一张（记住场景，主人答应时才拍）
        scene = self.photo_album.detect(text)
        if scene and self.photo_album.can_shoot():
            self._pending_photo_scene = scene
            self._pending_photo_until = time.time() + 300.0
            return (f"[美景] 屏幕/画面里认出了「{scene}」这样的景色，"
                    f"车主现在车速 {self._snap_speed_label()}。"
                    f"请以猫娘语气问一句要不要拍一张留念"
                    f"（提醒他可以直接说「拍吧」，面板上也有「📸 拍一张」）", "")
        # 天气提醒（雨/雪）——事实行，走内容通道
        weather = self.proactive.weather_propose(text)
        if weather:
            return "", weather
        return "", self.scene_chat.topic_from_ocr(text)

    def _snap_speed_label(self) -> str:
        s = self._last_snap
        return f"{s.speed_kmh:.0f} km/h" if s is not None else "未知"

    async def take_photo(self, scene: str = "", auto: bool = False) -> Dict[str, Any]:
        """拍一张：面板按钮 / 主人说"拍吧" / 她自己按场景拍。

        `auto=False` 表示这是明确要求，跳过相册冷却。
        """
        if self.hud_ocr is None:
            from ..adapters.hud_ocr import HudOcr
            self.hud_ocr = HudOcr(self.plugin)
        if not self.hud_ocr.is_available():
            return {"ok": False, "summary": "现在抓不到画面喵（截屏组件不可用）"}
        if not scene:
            scene = self._pending_photo_scene or "scenery"
        record = await self.photo_album.shoot(scene, self.hud_ocr,
                                              force=not auto)
        if not record:
            return {"ok": False, "summary": "刚拍过一张喵，过十分钟再拍~"}
        self._pending_photo_scene = ""
        self.recall.on_relationship("last_photo", {"value": scene}, importance=0.3)
        await self.push.push_fact(
            self.emotion.custom_fact(
                f"[拍照] 已经把刚才的画面存进相册（场景：{scene}）。"
                "请以猫娘语气说一句「拍好了」，提一句这个画面值得留着", "photo"),
            priority=IMPORTANT)
        return {"ok": True,
                "summary": f"📸 拍好了（{scene}）——面板「地图 → 美景相册」里能看到"}

    def _top_city_name(self) -> str:
        """跑得最多的城市（陪玩回忆维度用）。"""
        try:
            top = self._top_entries("cities")
            if top:
                return str(top[0].get("key", "") or "")
        except Exception:
            pass
        return ""

    # ── 内容层：地点文化 / 开场故事 / 途经播报（事实行交宿主 LLM 演绎）──

    def _opening_story_fact(self, snap, hints: Optional[list] = None) -> str:
        """每单开场：把这一单的客观信息 + 起点地域特色要求交给宿主 LLM。

        2026-10 主人补需求："开始任务的里程剩余里程什么的播报"——所以这里除了
        起点/终点/货物，还要带上全程里程、出发时剩余里程、预计到达时间，
        以及"这箱油够不够跑完全程"（和加油判断同一套判据）。
        """
        km = snap.planned_distance_km
        eta = km / 65.0 if km else 0.0
        hour = (snap.time_abs_min // 60) % 24 if snap.time_abs_min else -1
        when = ""
        if 0 <= hour < 5 or hour >= 23:
            when = "深夜出发"
        elif 5 <= hour < 9:
            when = "清晨出发"
        elif 17 <= hour < 22:
            when = "傍晚出发"
        bits = [f"起点 {snap.city_src or '未知'}", f"终点 {snap.city_dst or '未知'}",
                f"货物 {snap.cargo or '普通货'}", f"全程约 {km} km"]
        if eta:
            bits.append(f"预计 {eta:.1f} 小时")
        remain = float(getattr(snap, "route_remaining_km", 0) or 0)
        if remain:
            bits.append(f"出发时导航剩余 {remain:.0f} km")
        if snap.time_abs_min and eta:
            arr = int(snap.time_abs_min + eta * 60) % 1440
            bits.append(f"按当前时段推算约 {arr // 60:02d}:{arr % 60:02d} 到达")
        # 一路的油够不够：和 fuel_advice 同一判据（续航 vs 全程）
        rng = float(getattr(snap, "fuel_range_km", 0) or 0)
        if rng and km:
            bits.append("这箱油够跑完全程，不用专门找加油站"
                        if rng >= km * 1.1 else
                        f"续航只有 {rng:.0f} km，跑不完全程，中途必须加油")
        if when:
            bits.append(when)
        if snap.is_city:
            bits.append("当前在市区发车")
        mem = ""
        if hints:
            mem = "、".join(str(h) for h in hints[:2])
        fact = ("[行程开场] " + "；".join(bits) + "。"
                "请以猫娘语气开个场：一句介绍起点的地域特色/文化（可以讲当地风物、"
                "路况习惯），一句说这趟的打算（顺带把里程/预计时间说清楚）。"
                "不要念参数，像副驾随口聊。")
        if mem:
            fact += f"（可顺带提起：{mem}）"
        return fact

    def _arrival_culture_fact(self, snap) -> str:
        """到达目的地：让猫娘讲讲这个地方的特色与文化。"""
        city = snap.city_dst or ""
        bits = [f"目的地 {city or '（未知城市）'}"]
        if snap.job_delivered_revenue:
            bits.append(f"本单收入 {snap.job_delivered_revenue} €")
        if snap.job_cargo_damage is not None:
            bits.append(f"货物完好率 {(1 - snap.cargo_damage) * 100:.0f}%")
        return ("[到达] " + "；".join(bits) + "。"
                "请以猫娘语气收个尾：介绍一下这个地方的风土/文化/值得一看的东西"
                "（如果知道当地特色就点一句），并祝贺主人跑完这单。")

    def _passing_place_fact(self, place: dict) -> str:
        """途经已知地点：让她随口讲一句这个地方的特点。"""
        name = place.get("name", "")
        dist = place.get("distance_km", 0.0)
        visits = int(place.get("visits", 0) or 0)
        tail = "、我们以前来过这里" if visits > 1 else ""
        return (f"[途经] 正经过 {name} 附近（约 {dist:.1f} km）{tail}。"
                f"请以猫娘语气随口说一句这个地方的特点或风物（一句话，别念参数）")

    @staticmethod
    def _extract_level(text: str) -> Optional[int]:
        """从 OCR 文本提取等级数字（'level 15'/'Lv.15'/'15级'）。"""
        import re
        for pat in (r"lv\.?\s*(\d+)", r"level\s*(\d+)", r"(\d+)\s*级"):
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return int(m.group(1))
        return None

    async def _say_line(self, event_name: str, **kw: Any) -> None:
        """blind 短句推送：LLM 优先（配置了），失败/未配置用模板兜底。"""
        text = await self.emotion.short_line_llm(event_name, **kw)
        if not text:
            text = self.emotion.short_line(event_name, **kw)
        await self.push.push_direct(self.persona.polish(text))

    def _on_event(self, ev: TruckEvent) -> None:
        self._record_activity(ev)
        self._track_stats(ev)
        if ev.name == "job_start" and ev.snapshot:
            self._trip_start = ev
            self._reset_trip_stats()
            # 记忆写入不依赖推送仲裁：接单即记城市/货物/路线
            hints = self.recall.on_job_start(ev.snapshot.city_src, ev.snapshot.city_dst,
                                             ev.snapshot.cargo)
            self._spawn(self.memory.save())
            # 地点簿学习：接单时的坐标就是起点城市的位置
            snap0 = ev.snapshot
            if snap0.city_src:
                self.places.learn(snap0.city_src, snap0.world_x, snap0.world_z, "city")
            if snap0.city_dst:
                # 终点坐标未知，等到达时再学（这里只登记名字，坐标留待 job_delivered）
                pass
            lines = []
            preview = self.route_planner.preview(ev.snapshot)
            if preview:
                lines.append(self.persona.polish(preview))
            if lines:
                self._spawn(self.push.push_direct("\n".join(lines)))
            # ── 每单开场故事 + 起点地域文化（事实行交宿主 LLM 演绎，不硬编码台词）
            if getattr(self.cfg, "content_opening_story", True):
                self._spawn(self.push.push_fact(self._opening_story_fact(ev.snapshot, hints)))
            return
        if ev.name == "crash":
            # 计数已由 _track_stats 记一次（此处再 +1 会把单次事故计成两次）
            crash_hint = self.recall.on_crash()
            snap = ev.snapshot
            parts = ", ".join(f"{k} {v * 100:.0f}%" for k, v in
                              snap.damage_parts.items() if v > 0.01)
            # 事故必走 respond（事实行交宿主 LLM 按人设说话）：旧版只 push_direct
            # 直出硬编码短句，猫娘"没有 llm 对话"，主人听不到她的声音。
            self._spawn(self.push.push_fact(self.emotion.fact_prompt(
                "crash",
                parts=parts or "无明显损伤",
                speed=float(ev.data.get("speed_kmh", snap.speed_kmh)),
            ), priority=CRITICAL))
            if crash_hint:
                self._spawn(self.push.push_fact(
                    self.emotion.custom_fact(crash_hint, "crash"),
                    priority=CRITICAL))
            self._spawn(self._crash_chain(ev))
            return
        if ev.name == "job_delivered" and ev.snapshot:
            self._spawn(self._finish_trip(ev))  # 内部含归档，避免统计竞态
            # 到货只发一条：结算数据行（交宿主 LLM 收尾）。
            # 旧版还额外盲推一句写死的"到站检查"话术，和总结叠在一起像复读。
            snap_d = ev.snapshot
            if snap_d.city_dst:
                self.places.learn(snap_d.city_dst, snap_d.world_x, snap_d.world_z, "city")
                self._spawn(self.memory.save())
            # ── 到达地文化介绍（交宿主 LLM 讲这个地方的风土，不硬编码）
            if getattr(self.cfg, "content_arrival_culture", True):
                self._spawn(self.push.push_fact(self._arrival_culture_fact(snap_d)))
            # 到货升级提议（赚了钱 → 车行升级，L3 低频）
            revenue = ev.data.get("revenue", ev.snapshot.job_delivered_revenue)
            if revenue and revenue > 8000 and getattr(self, "_last_upgrade_tip", 0) == 0:
                self._last_upgrade_tip = 1
                self._spawn(self.push.push_direct(
                    self.persona.polish(f"赚了 {revenue} € 喵，要不要去车行升级下引擎？")))
            return
        if ev.name == "refuel":
            line = self.catgirl.existence_line("refuel")
            if line:
                self._spawn(self.push.push_direct(
                    self.persona.polish(line)))
            return
        if ev.name == "game_end":
            line = self.catgirl.existence_line("game_end")
            if line:
                self._spawn(self.push.push_direct(
                    self.persona.polish(line)))
            return
        if ev.name in ("ferry", "train"):
            # 过境：站点名是遥测里唯一可靠的地名 → 学习 + 介绍两端风土
            src = str(ev.data.get("source", "") or "")
            dst = str(ev.data.get("target", "") or "")
            snap_p = ev.snapshot
            if snap_p is not None:
                if src:
                    self.places.learn(src, snap_p.world_x, snap_p.world_z, "ferry_station")
                if dst:
                    self.places.learn(dst, snap_p.world_x, snap_p.world_z, "ferry_station")
                self._spawn(self.memory.save())
            what = "渡轮" if ev.name == "ferry" else "火车"
            self._spawn(self.push.push_fact(
                f"[过境] 主人把车开上了{what}：{src or '起点'} → {dst or '终点'}。"
                f"请以猫娘语气说一句（提一下这两地的地理位置/海峡或沿途风景，1-2 句）"))
            return
        if ev.name == "mountain_pass":
            climb = ev.data.get("climb_m", 0)
            alt = ev.data.get("altitude", 0)
            up = ev.data.get("direction") == "up"
            self._spawn(self.push.push_fact(
                f"[地标] 正在{'爬坡上山' if up else '下坡'}：这段海拔变化 {climb:.0f} 米"
                f"（当前海拔 {alt:.0f} 米）。请以猫娘语气提醒一句山路驾驶"
                f"（连续弯道/长下坡用发动机制动等），1 句即可"))
            return
        # ── 内容类事件：不走仲裁，直接 respond ──────────────────────
        # 这些都是一次性/长间隔的真实内容（行程分段、距离锚点、交付时间、
        # 收费站、挂车），旧版和普通事件一样过仲裁 → 常被"场景门控"或
        # "限流单槽窗口"吞掉。实测主人反馈："任务分段播报、时间提醒从没响过"。
        # 它们本身频率极低（一次任务 3~4 条），直接放行不会刷屏。
        if ev.name in CONTENT_EVENTS:
            prompt = self._render(ev)
            if prompt:
                self._spawn(self.push.push_fact(prompt))
            return
        allowed, reason = self.arbiter.decide(ev.name, ev.snapshot)
        if not allowed:
            # 留痕：面板决策日志现在也是滚动历史，这里再记一条便于对账
            self.plugin.logger.info("[事件·抑制] %s ← %s", ev.name, reason)
            return
        prompt = self._render(ev)
        if prompt:
            prio = CRITICAL if ev.name in CRITICAL_EVENTS else IMPORTANT
            if ev.name == "air_pressure_low" and ev.data.get("emergency"):
                prio = CRITICAL          # 紧急气压报警按事故级处理
            self._spawn(self.push.push_fact(prompt, priority=prio))

    def _reset_trip_stats(self) -> None:
        self._job_crashes = 0
        self._job_speedings = 0
        self._job_hard_brakes = 0
        self._job_refuels = 0
        self._job_fines = 0.0
        self._job_tolls = 0.0
        self._job_fuel_avg_sum = 0.0
        self._job_fuel_avg_n = 0
        self._job_night = 0
        self._job_start_ts = __import__("time").time()

    async def _crash_chain(self, ev: TruckEvent) -> None:
        """事故叙事链：伤情检查 → 维修评估/救援 → 事后安慰（L1 时间轴）。

        三个阶段全部走 respond（事实行 → 宿主 LLM 按人设演绎）。旧版是三段
        push_direct 硬编码台词，主人听不到猫娘自己的话（"车祸没有 llm 对话"）。
        """
        snap = ev.snapshot
        damage = snap.max_damage
        parts_detail = ", ".join(
            f"{k} {v * 100:.0f}%" for k, v in snap.damage_parts.items() if v > 0.01)
        # 伤情检查（5s 后）
        await asyncio.sleep(5)
        if damage >= 0.5:
            await self.push.push_fact(self.emotion.custom_fact(
                f"事故伤情检查：车辆损伤 {damage * 100:.0f}%（{parts_detail}），"
                f"损伤过半，继续开会持续恶化，该靠边停车叫救援拖车", "crash"))
        else:
            repair = round(damage * 1000)
            await self.push.push_fact(self.emotion.custom_fact(
                f"事故伤情检查：车辆损伤 {damage * 100:.0f}%（{parts_detail}），"
                f"修车预估 {repair} €，顺口提醒主人过弯慢一点", "crash"))
        # 事后安慰（30s 后）
        await asyncio.sleep(25)
        await self.push.push_fact(self.emotion.custom_fact(
            "事故已过 30 秒，主人仍在正常行驶、情绪平稳：安抚一句，"
            "提示慢慢开不着急", "crash"))

    def _track_stats(self, ev: TruckEvent) -> None:
        """累计行程统计。"""
        s = ev.snapshot
        if ev.name == "speeding":
            self._job_speedings += 1
        elif ev.name == "hard_brake":
            self._job_hard_brakes += 1
        elif ev.name == "crash":
            self._job_crashes += 1
        elif ev.name == "refuel":
            self._job_refuels += 1
        elif ev.name == "fine" and s:
            self._job_fines += float(s.fine_amount or 0)
        elif ev.name == "tollgate" and s:
            self._job_tolls += float(s.tollgate_amount or 0)
        elif ev.name == "trip_progress" and s and s.fuel_avg_consumption > 0:
            self._job_fuel_avg_sum += s.fuel_avg_consumption
            self._job_fuel_avg_n += 1
            if s.time_abs_min is not None and Proactive._is_night(s.time_abs_min):
                self._job_night = 1

    def _record_activity(self, ev: TruckEvent) -> None:
        """记录最近活动（面板时间线）。"""
        import datetime
        title = ACTIVITY_TITLES.get(ev.name, ev.name)
        detail = ""
        s = ev.snapshot
        if ev.name == "speeding" and s:
            detail = f"{s.speed_kmh:.0f}/{s.speed_limit_kmh:.0f} km/h"
        elif ev.name == "job_delivered" and s:
            detail = f"+{ev.data.get('revenue', s.job_delivered_revenue)} €"
        elif ev.name == "crash" and s:
            detail = f"损伤 {s.max_damage * 100:.0f}%"
        elif ev.name == "fine" and s:
            detail = f"-{s.fine_amount} €"
        elif ev.name == "job_start" and s:
            detail = f"{s.city_src} → {s.city_dst}"
        self._activity.insert(0, {
            "title": title,
            "detail": detail,
            "time": datetime.datetime.now().strftime("%H:%M"),
        })
        self._activity = self._activity[:20]

    async def _archive_trip(self, ev: TruckEvent) -> None:
        s = ev.snapshot
        start = self._trip_start.snapshot if self._trip_start else None
        fuel_avg = (self._job_fuel_avg_sum / self._job_fuel_avg_n
                    if self._job_fuel_avg_n else 0.0)
        duration_min = int((__import__("time").time() - self._job_start_ts) / 60) \
            if self._job_start_ts else 0
        trip = {
            "ts": int(ev.ts),
            "src": s.city_src or (start.city_src if start else ""),
            "dst": s.city_dst or (start.city_dst if start else ""),
            "cargo": s.cargo or "",
            "revenue": ev.data.get("revenue", s.job_delivered_revenue),
            "distance_km": s.planned_distance_km,
            "crashes": self._job_crashes,
            "speedings": self._job_speedings,
            "hard_brakes": self._job_hard_brakes,
            "refuels": self._job_refuels,
            "fines": self._job_fines,
            "tolls": self._job_tolls,
            "fuel_avg": round(fuel_avg, 1),
            "fuel_cost": round(fuel_avg * s.planned_distance_km / 100.0 * 1.5, 1),
            "repair": round(s.job_cargo_damage * 1000, 1),
            "duration_min": duration_min,
            "damage": round(s.job_cargo_damage, 3),
            "odometer_km": round(s.odometer_km, 1),
            "night": self._job_night,
        }
        self.recall.on_trip_end(trip)
        self.ledger.record(trip)
        await self.memory.save()
        self._trip_start = None
        self._reset_trip_stats()

    async def _finish_trip(self, ev: TruckEvent) -> None:
        """到货叙事：先取统计，再归档（清零），最后结算+总结。"""
        # 统计快照必须先于归档（归档会重置计数器）
        fuel_avg = (self._job_fuel_avg_sum / self._job_fuel_avg_n
                    if self._job_fuel_avg_n else 99.0)
        snap_stats = {
            "speedings": self._job_speedings,
            "hard_brakes": self._job_hard_brakes,
            "crashes": self._job_crashes,
            "refuels": self._job_refuels,
        }
        await self._archive_trip(ev)
        s = ev.snapshot
        start = self._trip_start.snapshot if self._trip_start else None
        lines = []
        trip = {
            "dst": s.city_dst or (start.city_dst if start else ""),
            "revenue": ev.data.get("revenue", s.job_delivered_revenue),
            "distance_km": s.planned_distance_km,
            "cargo": s.cargo or "",
            "duration_min": int((__import__("time").time() - self._job_start_ts) / 60)
            if self._job_start_ts else 0,
            "speedings": snap_stats["speedings"],
            "hard_brakes": snap_stats["hard_brakes"],
            "crashes": snap_stats["crashes"],
            "refuels": snap_stats["refuels"],
            "fuel_avg": round(fuel_avg, 1),
        }
        summary = self.trip_summary.build_fact(trip)
        lines.append(summary)
        # 新纪录/连续无事故庆祝（同样只给事实，不写死台词）
        profile_result = self.profile.record(trip)
        celebration = self.profile.record_celebration(profile_result)
        if celebration:
            lines.append(celebration)
        await self.push.push_fact(
            self.emotion.custom_fact("\n".join(lines), "job_delivered"))

    def _render(self, ev: TruckEvent) -> Optional[str]:
        s = ev.snapshot
        if s is None:
            return None
        if ev.name == "speeding":
            if self.persona.strict_mode:
                return ("主人超速了！{speed:.0f}/{limit:.0f} km/h！"
                        "立刻减速，安全不是儿戏！".format(speed=s.speed_kmh,
                                                       limit=s.speed_limit_kmh))
            return self.emotion.fact_prompt(ev.name, speed=s.speed_kmh, limit=s.speed_limit_kmh)
        if ev.name == "hard_brake":
            if self.persona.strict_mode:
                return (f"急刹！{s.speed_kmh:.0f} km/h 突然刹停！"
                        "跟车距离留够了吗？这样开迟早出事！")
            return self.emotion.fact_prompt(ev.name, speed=s.speed_kmh)
        if ev.name == "crash":
            parts = ", ".join(f"{k} {v * 100:.0f}%" for k, v in s.damage_parts.items() if v > 0.01)
            return self.emotion.fact_prompt(ev.name, parts=parts)
        if ev.name == "job_start":
            return self.emotion.fact_prompt(ev.name, cargo=s.cargo, src=s.city_src,
                                            dst=s.city_dst, km=s.planned_distance_km)
        if ev.name == "job_delivered":
            return self.emotion.fact_prompt(ev.name, revenue=ev.data.get("revenue", s.job_delivered_revenue))
        if ev.name == "job_cancelled":
            return self.emotion.fact_prompt(ev.name, penalty=ev.data.get("penalty", 0))
        if ev.name == "fine":
            return self.emotion.fact_prompt(ev.name, amount=s.fine_amount, offence=s.fine_offence or "违规")
        if ev.name == "tollgate":
            return self.emotion.fact_prompt(ev.name, amount=ev.data.get("amount", s.tollgate_amount))
        if ev.name == "refuel":
            return self.emotion.fact_prompt(ev.name, liters=s.fuel, percent=s.fuel_percent)
        if ev.name == "trailer_attach":
            return self.emotion.fact_prompt(ev.name, plate=s.trailer_license)
        if ev.name == "trailer_detach":
            return self.emotion.fact_prompt(ev.name)
        if ev.name == "time_warning":
            return self.emotion.fact_prompt(ev.name, min=ev.data.get("min", 0))
        if ev.name == "time_over":
            return self.emotion.fact_prompt(ev.name)
        if ev.name == "low_fuel":
            return self.emotion.fact_prompt(ev.name, percent=ev.data.get("percent", 0))
        if ev.name == "game_start":
            return self.emotion.fact_prompt(ev.name) + self._greeting()
        if ev.name == "game_end":
            return self.emotion.fact_prompt(ev.name) + self._farewell()
        if ev.name == "trip_progress":
            d = ev.data
            mark = d.get("mark", 0)
            ratio = {33: "三分之一", 50: "一半", 67: "三分之二", 100: "全程"}.get(mark, f"{mark}%")
            return self.emotion.fact_prompt(ev.name, mark_ratio=ratio,
                                            km=d.get("km", 0), min=d.get("min", 0))
        if ev.name == "distance_mark":
            d = ev.data
            return self.emotion.fact_prompt(ev.name,
                                            km=round(d.get("remaining_km", 0)),
                                            mark=d.get("mark", 0))
        if ev.name == "time_relaxed":
            return self.emotion.fact_prompt(ev.name, min=ev.data.get("min", 0))
        if ev.name == "time_tight":
            return self.emotion.fact_prompt(ev.name, min=ev.data.get("min", 0))
        if ev.name == "early_arrival":
            return self.emotion.fact_prompt(ev.name, min=ev.data.get("min", 0))
        if ev.name == "cargo_damage":
            return self.emotion.fact_prompt(ev.name, pct=ev.data.get("pct", 0))
        if ev.name == "vehicle_damage":
            return self.emotion.fact_prompt(
                ev.name,
                percent=ev.data.get("percent", 0.0),
                parts=ev.data.get("parts", ""),
            )
        if ev.name == "brake_temp_high":
            return self.emotion.fact_prompt(
                ev.name, temp=ev.data.get("temp", 0.0),
                retarder_hint=("（缓速器开着能帮上忙）" if ev.data.get("retarder")
                               else "（长下坡记得用缓速器/发动机制动）"))
        if ev.name == "air_pressure_low":
            return self.emotion.fact_prompt(
                ev.name, bar=ev.data.get("bar", 0.0),
                emergency_hint=("是紧急气压报警，必须马上靠边停车"
                                if ev.data.get("emergency") else "刹车会变软"))
        # 机务/操作类：参数直接来自遥测
        if ev.name in ("adblue_low", "battery_low",
                       "lights_off_night", "park_brake_moving", "wheels_airborne",
                       "engine_stalled", "high_beam_on", "blinker_forgotten",
                       "engine_warning", "overheat"):
            kw = dict(ev.data)
            kw.setdefault("speed", s.speed_kmh)
            return self.emotion.fact_prompt(ev.name, **kw)
        return None

    def _greeting(self) -> str:
        """按游戏内时段问候。"""
        import datetime
        hour = datetime.datetime.now().hour
        streak = self._bump_streak()
        base = ""
        if 5 <= hour < 12:
            base = " 早呀！今天想拉什么货？"
        elif 12 <= hour < 18:
            base = " 下午好喵！今天跑哪条线？"
        elif 18 <= hour < 23:
            base = " 晚上好喵！今晚跑哪条线？"
        else:
            base = " 深夜了喵… 我陪你跑夜路！"
        if streak >= 2:
            base += f" 又是你喵，连续 {streak} 天跑车真勤快！"
        return base

    def _bump_streak(self) -> int:
        """连续天数记录（关系记忆）。"""
        import datetime
        today = datetime.date.today().isoformat()
        entry = self.memory.query("relationship", "streak") or {}
        last = entry.get("last_day", "")
        count = int(entry.get("count", 0))
        if last == today:
            return count
        # 昨天 → 连续+1；否则重置
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        count = count + 1 if last == yesterday else 1
        self.memory.remember("relationship", "streak",
                             {"count": count, "last_day": today}, importance=0.7)
        return count

    def _farewell(self) -> str:
        """退出总结：今日里程/单数。"""
        p = self.profile.snapshot()
        stats = p.get("stats", {})
        km = int(stats.get("total_km", 0))
        trips = int(stats.get("trips", 0))
        if trips:
            return f" 今天累计跑了 {km} km、{trips} 单喵，辛苦啦，晚安~ 💤"
        return " 今天辛苦了喵，晚安~ 💤"

    async def handle_status_query(self) -> Dict[str, Any]:
        r = TelemetryReader()
        if not r.open():
            return {"ok": False, "summary": "游戏没在运行喵，先打开欧卡2吧"}
        s = r.snapshot()
        r.close()
        if not s.on_job:
            state = "休息中" if s.paused else "自由驾驶"
            return {"ok": True,
                    "summary": f"现在没有任务喵，在{state}。车辆：{s.truck_brand} {s.truck_name}"}
        pct = s.trip_progress_percent or 0
        return {"ok": True, "summary": (
            f"任务：{s.city_src} → {s.city_dst}，{s.cargo}，"
            f"进度 {pct:.0f}%，剩 {s.route_remaining_km:.0f} km "
            f"~{s.route_remaining_time_min:.0f} 分钟喵")}

    async def handle_player_talk(self, text: str) -> Dict[str, Any]:
        self.arbiter.on_player_speak()
        # 陪玩关键：主人说过的话要记住，之后她会自己主动提起
        try:
            self.recall.on_player_remark(text)
            self._spawn(self.memory.save())
        except Exception as exc:            # noqa: BLE001
            self.plugin.logger.warning("记住主人这句话失败: %s", exc)
        # "拍吧" —— 她问了要不要拍照，主人答应了（口语/面板按钮两条路都通）
        if any(k in text for k in ("拍照", "拍一张", "拍吧", "拍下来", "照一张",
                                   "来一张", "截图", "留个纪念")):
            return await self.take_photo()
        r = TelemetryReader()
        if not r.open():
            return {"ok": True,
                    "summary": "游戏没在运行喵，先打开欧卡2再聊驾驶吧"}
        s = r.snapshot()
        r.close()
        if "超速" in text or ("快" in text and "慢" not in text):
            if s.is_speeding:
                return {"ok": True, "summary": (
                    f"正在超速：{s.speed_kmh:.0f} / 限速 {s.speed_limit_kmh:.0f} km/h")}
            return {"ok": True, "summary": f"没有超速，当前 {s.speed_kmh:.0f} km/h"}
        if "油" in text:
            return {"ok": True, "summary": (
                f"油量 {s.fuel_percent:.0f}%（{s.fuel:.0f}L），还能跑 {s.fuel_range_km:.0f} km")}
        # 知识库问答
        if "省油" in text or "怎么开省" in text:
            return {"ok": True, "summary": self.knowledge.fuel_tip()}
        if "卡车" in text or "这车" in text or "这辆" in text:
            info = self.knowledge.truck_info(s.truck_brand)
            if info:
                return {"ok": True, "summary": info}
        if "载重" in text or "拉多少" in text or "能拉" in text:
            return {"ok": True, "summary": (
                f"当前挂车货物 {s.cargo_mass:.0f}t，这台车拉这个量很轻松喵")}
        if "哪个城市" in text and ("赚" in text or "钱" in text):
            return {"ok": True, "summary": self.knowledge.best_city_tip(s.city_dst)}
        if "第二台车" in text or "车队" in text or "买台" in text or "买辆车" in text:
            return {"ok": True, "summary": self.knowledge.fleet_tip(self.ledger.month_summary())}
        if "多远" in text or "距离" in text:
            tip = self.knowledge.route_tip(s.city_src, s.city_dst)
            if tip:
                return {"ok": True, "summary": tip}
        if "货物" in text and s.cargo:
            return {"ok": True, "summary": self.knowledge.cargo_tip(s.cargo)}
        if "路" in text and ("哪条" in text or "怎么走" in text or "选" in text):
            choice = self.route_planner.route_choice(s)
            if choice:
                return {"ok": True, "summary": choice}
        if "服务区" in text or "加油" in text:
            advice = self.route_planner.service_area_advice(s)
            if advice:
                return {"ok": True, "summary": advice}
        if "几级" in text or "等级" in text:
            return {"ok": True, "summary": (
                f"你现在 {self.level_celebrate.snapshot().get('level', 0)} 级喵")}
        if "赔" in text or "修车" in text or "撞车" in text:
            repair = round(s.max_damage * 1000)
            return {"ok": True, "summary": (
                f"当前损伤 {s.max_damage * 100:.0f}%，修车估计要 {repair} € 喵，小心点开呀")}
        if "累" in text or "困" in text or "疲劳" in text:
            rest = s.rest_stop_min
            if rest is not None and rest > 0:
                return {"ok": True, "summary": (
                    f"离强制休息还有 {rest:.0f} 分钟喵，累了就找休息区睡一觉")}
            return {"ok": True, "summary": "目前还不累喵，继续开没问题~"}
        if "超时" in text or "来得及" in text or "时间够" in text:
            rem = s.delivery_remaining_min
            if rem is not None:
                state = "很宽裕" if rem > 120 else "有点紧" if rem > 60 else "很紧张"
                return {"ok": True, "summary": (
                    f"交付还剩 {rem} 分钟，时间{state}喵，别超速赶路")}
            return {"ok": True, "summary": "现在没有任务喵"}
        if "接" in text and ("单" in text or "任务" in text) and "该" in text:
            dist = s.planned_distance_km
            revenue = s.job_income
            if dist and revenue:
                per_km = revenue / dist
                verdict = "划算" if per_km > 15 else "一般" if per_km > 10 else "不划算"
                return {"ok": True, "summary": (
                    f"这单 {dist} km 赚 {revenue} €，每公里 {per_km:.0f} €，{verdict}喵")}
        # 游戏机制/风景/建筑/标识知识问答（任意语句先匹配，命中即答）
        tip = self.knowledge.game_tip(text)
        if tip:
            return {"ok": True, "summary": tip}
        # 配置了自建 LLM → 深度回答（失败/未配置自动落兜底）
        if self.llm.configured:
            prompt = (f"你是{self.persona.name}（{self.persona.user_call}的副驾驶猫娘）。"
                      f"当前车辆 {s.truck_brand} {s.truck_name}，车速 {s.speed_kmh:.0f} km/h，"
                      f"限速 {s.speed_limit_kmh:.0f} km/h。玩家说：{text}。"
                      f"用一句话自然回应（25 字内，带猫娘语气）。")
            answer = await self.llm.call(prompt)
            if answer:
                return {"ok": True, "summary": answer}
        # 兜底：知识性回应（车况 + 随机技巧），给 LLM 素材避免反问
        import random as _rnd
        extra = _rnd.choice(self.knowledge.drive_tips + self.knowledge.fuel_tips)
        return {"ok": True, "summary": (
            f"当前车辆 {s.truck_brand} {s.truck_name}，限速 {s.speed_limit_kmh:.0f} km/h。"
            f"顺带一提：{extra}")}

