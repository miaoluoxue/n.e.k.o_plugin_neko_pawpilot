"""冒烟测试：manifest 校验 + 核心逻辑（直连根目录导入）。"""

# 包链 + SDK stub 由 conftest.py 建立；本文件只做纯 import（避免 E402）。
from __future__ import annotations

import pathlib
import shutil
import time
import tomllib

from plugin.plugins.neko_pawpilot.adapters.telemetry_client import (
    TruckSnapshot,
)
from plugin.plugins.neko_pawpilot.core.arbiter import Arbiter
from plugin.plugins.neko_pawpilot.core.challenge import Challenge
from plugin.plugins.neko_pawpilot.core.config_model import PawpilotConfig
from plugin.plugins.neko_pawpilot.core.event_catalog import (
    EVENT_CATALOG,
    spec,
)
from plugin.plugins.neko_pawpilot.core.event_engine import EventEngine
from plugin.plugins.neko_pawpilot.core.knowledge import KnowledgeBase
from plugin.plugins.neko_pawpilot.core.ledger import Ledger
from plugin.plugins.neko_pawpilot.core.memory import MemoryStore
from plugin.plugins.neko_pawpilot.core.mood import Persona
from plugin.plugins.neko_pawpilot.core.proactive import Proactive
from plugin.plugins.neko_pawpilot.core.profile import DriverProfile
from plugin.plugins.neko_pawpilot.core.recall import Recall
from plugin.plugins.neko_pawpilot.core.safety_guard import SafetyGuard
from plugin.plugins.neko_pawpilot.core.scenario import (
    HIGHWAY,
    IDLE,
    URBAN,
    ScenarioMachine,
)
from plugin.plugins.neko_pawpilot.core.small_talk import SmallTalk
from plugin.plugins.neko_pawpilot.core.templates import EmotionRenderer
from plugin.plugins.neko_pawpilot.core.trip_summary import TripSummary

_ROOT = pathlib.Path(__file__).resolve().parent.parent

TMP = pathlib.Path(__file__).parent / ".tmp_pawpilot_test"


class FakeStore:
    """宿主 store 的模拟：KV 内存存储。"""

    def __init__(self):
        self._data = {}

    async def get(self, key, default=None):
        return self._data.get(key, default)

    async def set(self, key, value):
        self._data[key] = value


class FakePlugin:
    def __init__(self, tmp: pathlib.Path):
        self.config_dir = tmp
        self.store = FakeStore()
        self.logger = None


def _manifest() -> dict:
    return tomllib.loads((_ROOT / "plugin.toml").read_text(encoding="utf-8"))


def _cfg() -> PawpilotConfig:
    return PawpilotConfig()


def test_manifest():
    m = _manifest()
    assert m["plugin"]["id"] == "neko_pawpilot"
    assert m["plugin"]["entry"] == "plugin.plugins.neko_pawpilot:NekoPawpilotPlugin"
    assert m["plugin_runtime"]["enabled"] is True
    assert m["plugin"]["store"]["enabled"] is True
    # 面板指向 static HTML（方便管理）
    assert m["plugin"]["ui"]["panel"][0]["entry"] == "static/index.html"
    # 配置段存在（宿主配置系统读取）
    assert m["neko_pawpilot"]["dry_run"] is True
    assert m["neko_pawpilot"]["global_rate_limit_s"] == 12.0


def test_config_section():
    m = _manifest()
    cfg = PawpilotConfig(m["neko_pawpilot"])
    assert cfg.poll_interval_s == 1.0
    assert cfg.dry_run is True
    assert cfg.push_visibility == ["chat"]
    assert cfg.global_rate_limit_s == 12.0
    assert cfg.safety_failure_limit == 5


def test_event_engine_synthetic():
    eng = EventEngine(_cfg())
    fired = []
    eng.on_event(lambda e: fired.append(e.name))

    def mk(**kw):
        s = TruckSnapshot()
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    eng.feed(mk(sdk_active=True))
    eng.feed(mk(sdk_active=True, on_job=True, cargo="steel", city_src="Paris",
                   city_dst="Berlin", planned_distance_km=760))
    eng.feed(mk(sdk_active=True, on_job=True, speed_mps=25, speed_limit_mps=20,
                   wear_cabin=0.12))
    eng.feed(mk(sdk_active=True, on_job=True, fuel=10, fuel_capacity=500))
    eng.feed(mk(sdk_active=True, on_job=True, ev_fined=True, fine_amount=400,
                   fine_offence="speeding"))
    eng.feed(mk(sdk_active=True, on_job=False, ev_job_delivered=True,
                   job_delivered_revenue=12000))
    expected = {"game_start", "job_start", "speeding", "crash", "low_fuel",
                "fine", "job_delivered"}
    missing = expected - set(fired)
    assert not missing, f"missing: {missing}"


def test_emotion_layer():
    persona = Persona()
    renderer = EmotionRenderer(persona)
    fact = renderer.fact_prompt("speeding", speed=90, limit=80)
    assert "90" in fact and "限速" in fact
    # 人设提示由宿主导入（名字/称呼）
    assert "你是" in fact
    assert persona.name in fact
    line = renderer.short_line("speeding", speed=90, limit=80)
    assert "喵" in line or "！" in line
    assert "worry" in persona.mood.snapshot()
    persona.mood.trigger("excitement", 0.9)
    assert persona.mood.style().get("energy") == "high"


def test_memory_system():
    store = MemoryStore(FakeStore())
    recall = Recall(store)
    recall.on_relationship("nickname", {"value": "司机先生"}, importance=0.8)
    assert store.query("relationship", "nickname")["value"] == "司机先生"
    recall.on_trip_end({"src": "Paris", "dst": "Berlin", "cargo": "steel",
                        "revenue": 12000, "crashes": 1, "ts": int(time.time())})
    assert store.query("cities", "Berlin")["count"] == 1
    assert store.query("cargos", "steel")["best_income"] == 12000
    recall.on_trip_end({"src": "Paris", "dst": "Berlin", "cargo": "steel",
                        "revenue": 13000, "crashes": 0, "ts": int(time.time())})
    route = store.query("cities", "route_Paris_Berlin")
    assert route["count"] == 2
    hints = recall.on_job_start("Paris", "Berlin", "steel")
    assert any("2 次" in h for h in hints)
    # weight 衰减到下限归档（不删除）
    store.remember("cities", "trivial", {"count": 1}, importance=0.14)
    store.decay()
    assert store.query("cities", "trivial").get("archived") is True


def test_event_catalog():
    assert "crash" in EVENT_CATALOG
    assert spec("crash").preempt is True
    assert spec("crash").priority == 9
    assert spec("speeding").cooldown_seconds == 30
    # 未知事件给保守默认
    assert spec("unknown_xyz").priority == 1


def test_scenario_machine():
    sm = ScenarioMachine()

    def mk(speed, limit, on_job=True, sdk=True, paused=False):
        s = TruckSnapshot()
        s.speed_mps = speed / 3.6
        s.speed_limit_mps = limit / 3.6
        s.on_job = on_job
        s.sdk_active = sdk
        s.paused = paused
        return s

    assert sm.update(mk(0, 50, on_job=False)) == IDLE
    assert sm.update(mk(0, 50)) != IDLE  # 有任务
    assert sm.update(mk(90, 90)) == HIGHWAY
    assert sm.update(mk(40, 50)) == URBAN
    assert sm.allow("safety") is True
    assert sm.allow("task") is True


def test_safety_guard():
    cfg = _cfg()
    sg = SafetyGuard(cfg)
    assert sg.status() == "running"
    sg.pause()
    assert sg.stopped is True
    sg.resume()
    assert sg.status() == "running"
    # 自动急停：窗口内 5 次失败
    for _ in range(5):
        sg.record_failure()
    assert sg.auto_paused is True
    assert sg.status() == "tripped"


def test_arbiter():
    cfg = _cfg()
    sg = SafetyGuard(cfg)
    arb = Arbiter(cfg, sg)
    arb.broadcast_categories = dict(cfg.broadcast_categories)
    snap = TruckSnapshot()
    snap.sdk_active = True
    snap.on_job = True
    snap.speed_mps = 90 / 3.6
    snap.speed_limit_mps = 90 / 3.6
    arb.update_scenario(snap)
    # 玩家静默窗
    arb.on_player_speak(silence_s=60)
    allowed, reason = arb.decide("speeding", snap)
    assert allowed is False and reason == "player_quiet_window"
    arb._player_silence_until = 0
    # 正常放行
    allowed, reason = arb.decide("speeding", snap)
    assert allowed is True
    # 冷却内拒绝
    allowed, reason = arb.decide("speeding", snap)
    assert allowed is False and reason == "cooldown"
    # 场景门控：IDLE 下 task 类被挡
    snap.on_job = False
    arb.update_scenario(snap)
    allowed, reason = arb.decide("job_start", snap)
    assert allowed is False and reason.startswith("scenario_gated")
    # 类别开关
    arb.broadcast_categories["trip"] = False
    snap.on_job = True
    arb.update_scenario(snap)
    allowed, reason = arb.decide("refuel", snap)
    assert allowed is False and reason == "category_disabled"


def test_ledger():
    store = MemoryStore(FakeStore())
    ledger = Ledger(store)
    entry = ledger.record({"ts": 1, "src": "Paris", "dst": "Berlin",
                           "revenue": 12000, "tolls": 45, "fines": 100,
                           "fuel_cost": 1800, "repair": 800})
    assert entry["net"] == 12000 - 45 - 100 - 1800 - 800
    m = ledger.month_summary()
    assert m["trips"] == 1
    assert "净赚" in ledger.render_summary()


def test_challenge():
    store = MemoryStore(FakeStore())
    ch = Challenge(store)
    line = ch.start(kind="fuel")
    assert line  # 挑战话术非空
    settle = ch.settle({"fuel_avg": 20.0, "speedings": 0, "hard_brakes": 0})
    assert "赢" in settle or "♪" in settle  # 低油耗必赢
    wins = store.query("relationship", "challenge_wins") or {}
    assert wins.get("count", 0) >= 1


def test_trip_summary():
    ts = TripSummary({"summary_head": "「{dst}这趟：{distance:.0f} km",
                      "summary_tail": "开得不错喵！"})
    text = ts.build({"dst": "Berlin", "distance_km": 760, "revenue": 12000,
                     "cargo": "steel", "duration_min": 552, "speedings": 2,
                     "hard_brakes": 1, "crashes": 0, "refuels": 1,
                     "fuel_avg": 32.0})
    assert "Berlin" in text and "760" in text and "12000" in text
    assert "超速 2 次" in text and "32.0" in text


def test_knowledge():
    kb = KnowledgeBase()
    assert "PACCAR" in kb.truck_info("DAF XD")
    assert kb.fuel_tip()
    assert "玻璃" in kb.cargo_tip("玻璃")  # 易碎类
    assert kb.city_distance("Berlin", "Hamburg") == 280
    assert kb.route_tip("Berlin", "Hamburg")


def test_proactive():
    cfg = PawpilotConfig()
    p = Proactive(cfg)
    snap = TruckSnapshot()
    snap.on_job = True
    snap.speed_mps = 80 / 3.6
    snap.fuel = 50
    snap.fuel_capacity = 500
    snap.rest_stop_min = 60
    snap.time_abs_min = 60 * 23  # 深夜
    p.update(snap)
    line = p.propose(now=time.time() + 1)
    assert line is not None  # 低油量必触发


def test_profile():
    store = MemoryStore(FakeStore())
    prof = DriverProfile(store)
    prof.record({"distance_km": 760, "revenue": 12000, "speedings": 2,
                 "hard_brakes": 1, "crashes": 0, "night": 1})
    prof.record({"distance_km": 500, "revenue": 8000, "speedings": 0,
                 "hard_brakes": 0, "crashes": 0, "night": 0})
    snap = prof.snapshot()
    assert snap["label"]
    assert "首 100km" in snap["milestones"] or "首 1000km" in snap["milestones"]


def test_small_talk():
    st = SmallTalk()
    assert st.random_topic(now=time.time() + 1)
    # 预触发 100/500 里程碑，验证 1000km
    st._fired_milestones.update({100, 500})
    st._last_km = 1200
    topic = st.milestone_topic()
    assert topic and "1000km" in topic


def test_telemetry_installer():
    from plugin.plugins.neko_pawpilot.adapters.telemetry_installer import (
        TelemetryInstaller,
    )
    ti = TelemetryInstaller()
    assert ti.bundled_available(), "插件必须捆绑 scs-telemetry.dll"
    assert len(ti.bundled_hash()) == 64
    fake = pathlib.Path(__file__).parent / ".tmp_tl_test"
    fake.mkdir(parents=True, exist_ok=True)
    try:
        # 缺失 → installed
        r1 = ti.install(str(fake))
        assert r1["status"] == "installed"
        # 一致 → ok
        r2 = ti.install(str(fake))
        assert r2["status"] == "ok"
        # 版本不符 → updated
        (ti.target_path(str(fake))).write_bytes(b"stale")
        r3 = ti.install(str(fake))
        assert r3["status"] == "updated"
        assert ti.installed_ok(str(fake))
        # 无捆绑 → no_bundle
        ti2 = TelemetryInstaller()
        ti2._bundled = pathlib.Path(__file__).parent / "no_such_dll.dll"
        assert ti2.install(str(fake))["status"] == "no_bundle"
    finally:
        shutil.rmtree(fake, ignore_errors=True)


def test_runtime_start_smoke():
    """runtime.start() 全链路：防 AttributeError 类启动回归。"""
    import asyncio
    import logging

    from plugin.plugins.neko_pawpilot.core.runtime import PawpilotRuntime

    class _FakePlugin:
        def __init__(self):
            self.store = FakeStore()
            self.logger = logging.getLogger("test_pawpilot")
            self.persona = None
            self.config_dir = _ROOT

    async def _run():
        rt = PawpilotRuntime(_FakePlugin(), PawpilotConfig())
        status = await rt.start()
        await rt.shutdown()
        return status

    status = asyncio.run(_run())
    assert status["status"] == "ready"


def test_push_sender_sync_contract():
    """push_message 是 SDK 同步方法（返回 dict），push_sender 不得 await 它。"""
    import asyncio

    from plugin.plugins.neko_pawpilot.adapters.push_sender import PushSender

    class _FakePlugin:
        def __init__(self):
            self.calls = []
            self.logger = None

        def push_message(self, **kw):
            self.calls.append(kw)
            return {"submitted": True}

    fp = _FakePlugin()
    ps = PushSender(fp, dry_run=False)
    assert asyncio.run(ps.push_fact("测试")) is True
    assert fp.calls and fp.calls[0]["ai_behavior"] == "respond"
    assert asyncio.run(ps.push_direct("直出")) is True
    assert fp.calls[1]["visibility"] == ["chat"]

    # 拒绝路径：返回 submitted=False 应判失败
    class _RejectPlugin(_FakePlugin):
        def push_message(self, **kw):
            return {"ok": False, "submitted": False, "reason": "rate_limited"}

    rp = _RejectPlugin()
    ps2 = PushSender(rp, dry_run=False)
    assert asyncio.run(ps2.push_fact("x")) is False


def test_road_matching_and_facility_queries():
    """道路匹配（74k 段网格索引）：最近道路/路网密度/前方弯道可用。

    红绿灯提议已删除：实测地图数据里全图仅 194 个红绿灯，站在城市里
    3km 内 0 个（最近 10.3km），该功能拿现有数据无法成立。
    """
    from plugin.plugins.neko_pawpilot.core.map_kb import MapKnowledge

    kb = MapKnowledge()
    snap = kb.snapshot()
    assert snap["loaded"] is True
    assert snap["roads"] > 1000 and snap["facilities"] > 100

    # 取一个已知道路端点坐标 → 最近道路距离应≈0
    road = kb._roads[0]
    near = kb.nearest_road(road["x"], road["z"], max_km=1.0)
    assert near is not None, "道路索引应按坐标命中"
    assert near["distance_km"] < 0.05, f"端点应几乎重合，实际 {near['distance_km']} km"
    assert near["road_type"], "应带回道路类型"

    # 路网密度：城区/枢纽应远大于 0
    assert kb.road_density(road["x"], road["z"], radius_km=1.0) > 0

    # 前方弯道：不抛异常，返回 None(直) 或弧度值
    curve = kb.curve_ahead(road["x"], road["z"])
    assert curve is None or isinstance(curve, float)

    # 远离地图（超出 bbox）→ 找不到道路/设施，不应误报
    assert kb.nearest_road(500000, 500000, max_km=1.0) is None
    assert kb.nearest_facility(500000, 500000, kind="fuel", max_km=3.0) is None


def test_station_proposal():
    """加油站/服务区接近提议：低油量→加油，长途→休息，冷却+重置。"""
    import time as _time

    from plugin.plugins.neko_pawpilot.core.map_kb import MapKnowledge
    from plugin.plugins.neko_pawpilot.core.proactive import Proactive

    kb = MapKnowledge()
    fuel = kb.nearest_facility(1798, 1783, kind="fuel", max_km=3)
    assert fuel and fuel["kind"] == "fuel"
    svc = kb.nearest_facility(2477, 6713, kind="service", max_km=4)
    assert svc and svc["kind"] == "service"

    class Snap:
        def __init__(self, x, z, fuel_pct=80, rem=400):
            self.on_job = True
            self.world_x, self.world_z = x, z
            self.fuel_percent = fuel_pct
            self.route_remaining_km = rem
            self.rest_stop_min = 500
            self.speed_kmh = 60
            self.time_abs_min = 720
            self.delivery_remaining_min = 300

    p = Proactive(PawpilotConfig(), map_kb=kb)
    now = _time.time()
    # 低油量接近加油站
    msg = p.station_propose(Snap(1798, 1783, fuel_pct=30), now)
    assert msg and "油" in msg
    # 冷却：同一站不再报
    assert p.station_propose(Snap(1798, 1783, fuel_pct=30), now + 1) is None
    # 高油量接近服务区（长途）
    msg2 = p.station_propose(Snap(2477, 6713, fuel_pct=80), now + 2)
    assert msg2 and "服务区" in msg2
    # 开到地图外（半径内无任何设施）→ 重置，允许下次重新提醒
    # 注意：半径已从 1.5/2.0km 放宽到 3/4km，所以这里必须用真正远离全图
    # 设施的坐标（地图 bbox 约 ±68k），否则近处仍有油站、重置不会发生
    p.station_propose(Snap(500000, 500000, fuel_pct=30), now + 3)
    assert p._last_station_id is None


def test_game_knowledge():
    """游戏机制知识问答：罚款/疲劳/升级/档位/天气/技巧兜底。"""
    from plugin.plugins.neko_pawpilot.core.knowledge import KnowledgeBase
    kb = KnowledgeBase()
    assert "罚款" in kb.game_tip("罚款怎么算")
    assert "休息" in kb.game_tip("疲劳驾驶会怎么样")
    assert "升级" in kb.game_tip("怎么升级卡车")
    assert "档" in kb.game_tip("手动挡怎么开")
    assert "雨" in kb.game_tip("下雨天要注意什么")
    # 技巧兜底
    assert kb.game_tip("有什么驾驶技巧")
    assert kb.drive_tip()
    # 完全不匹配返回 None
    assert kb.game_tip("帮我看看这个货") is None


def test_game_knowledge_expanded():
    """扩充知识库：国家/左行/货物/司机/车库/油价/特殊货物 + 组合词防截胡。"""
    from plugin.plugins.neko_pawpilot.core.knowledge import KnowledgeBase
    kb = KnowledgeBase()
    # 新主题命中
    assert "英国" in kb.game_tip("英国怎么开")
    assert "危险品" in kb.game_tip("有什么特殊货物")
    assert "司机" in kb.game_tip("雇司机怎么管")
    assert "车库" in kb.game_tip("车库怎么升级")  # 组合词优先于升级主题
    assert "东欧" in kb.game_tip("哪加油便宜")    # 油价优先于燃料
    assert "渡轮" in kb.game_tip("渡轮怎么坐")
    # 货物明细
    assert "几十种" in kb.game_tip("什么货物好拉")
    # 城市距离扩充
    assert kb.city_distance("Berlin", "Prague") == 350
    assert kb.city_distance("Madrid", "Barcelona") == 620
    # 危险品货物
    assert "危险品" in kb.cargo_tip("化学品")


def test_road_knowledge():
    """道路知识：道路类型/弯道/坡道/隧道/环岛/施工/山路/限速。"""
    from plugin.plugins.neko_pawpilot.core.knowledge import KnowledgeBase
    kb = KnowledgeBase()
    assert "高速" in kb.game_tip("高速和国道有什么区别")
    assert "弯道" in kb.game_tip("弯道怎么开安全")
    assert "坡" in kb.game_tip("下长坡注意什么")
    assert "灯" in kb.game_tip("过隧道要注意什么")
    assert "环岛" in kb.game_tip("环岛怎么走")
    assert "施工" in kb.game_tip("遇到施工区怎么办")
    assert "山路" in kb.game_tip("山路怎么开")
    assert "限速" in kb.game_tip("限速牌怎么看")
    assert "超车" in kb.game_tip("高速上怎么变道")


def test_scenery_knowledge():
    """风景/建筑/标识/NPC知识：看见什么聊什么，避免空洞反问。"""
    from plugin.plugins.neko_pawpilot.core.knowledge import KnowledgeBase
    kb = KnowledgeBase()
    assert "风车" in kb.game_tip("路边风车好多")
    assert "建筑" in kb.game_tip("前面是什么建筑")
    assert "极光" in kb.game_tip("北欧有极光吗")      # 天气优先于 DLC
    assert "鹿" in kb.game_tip("路上遇到鹿怎么办")
    assert "风景" in kb.game_tip("这风景真漂亮")
    assert "AI 车" in kb.game_tip("那些AI车怎么开的")  # 大小写不敏感
    assert "彩虹" in kb.game_tip("路边有彩虹")
    assert "地标" in kb.game_tip("埃菲尔铁塔在哪")
    assert "动物" in kb.game_tip("路上有动物")
    # 任意语句不返回 None（兜底有知识素材）
    assert kb.game_tip("帮我看看油箱") is None  # 无关键词时 game_tip 本身返回 None


def test_events_fire_without_job():
    """自由驾驶（无任务）时急刹/超速/撞车也必须触发事件。"""
    from plugin.plugins.neko_pawpilot.adapters.telemetry_client import TruckSnapshot
    from plugin.plugins.neko_pawpilot.core.event_engine import EventEngine

    eng = EventEngine(PawpilotConfig())
    events = []
    eng.on_event(lambda ev: events.append(ev.name))

    def mk(**kw):
        s = TruckSnapshot()
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    # 自由驾驶（on_job=False）急刹
    eng.feed(mk(sdk_active=True, on_job=False, speed_mps=30,
                user_brake=1.0, wear_engine=0.1))
    assert "hard_brake" in events
    events.clear()
    # 自由驾驶超速
    eng.feed(mk(sdk_active=True, on_job=False, speed_mps=40,
                speed_limit_mps=25))
    assert "speeding" in events
    events.clear()
    # 自由驾驶撞车（损伤突增）
    eng.feed(mk(sdk_active=True, on_job=False, speed_mps=20, wear_engine=0.1))
    eng.feed(mk(sdk_active=True, on_job=False, speed_mps=0, wear_engine=0.4))
    assert "crash" in events


def test_distance_marks():
    """距离分级锚点：按任务里程自动生成，长/短途各有合理分级。"""
    from plugin.plugins.neko_pawpilot.adapters.telemetry_client import TruckSnapshot
    from plugin.plugins.neko_pawpilot.core.event_engine import (
        EventEngine,
        _gen_distance_anchors,
    )

    # 锚点自动生成验证
    assert _gen_distance_anchors(500) == (250, 125, 50, 25, 12, 5)
    assert _gen_distance_anchors(20) == (10, 5, 2, 1)
    assert _gen_distance_anchors(8) == (4, 2, 1)

    eng = EventEngine(PawpilotConfig())
    marks = []
    eng.on_event(lambda ev: marks.append(ev.data.get("mark"))
                 if ev.name == "distance_mark" else None)

    def mk(rem, on_job=True):
        s = TruckSnapshot()
        s.sdk_active = True
        s.on_job = on_job
        s.route_distance_km = rem * 1000.0
        return s

    # 长途单 500km：触发 250/125/50/25/12/5
    for rem in (500, 260, 240, 130, 120, 55, 45, 26, 20, 13, 10, 4, 0.5):
        eng.feed(mk(rem))
    assert marks == [250, 125, 50, 25, 12, 5], f"500km 应触发比例锚点，实际 {marks}"
    # 任务结束重置
    eng.feed(mk(0, on_job=False))
    # 短途单 8km：触发 4/2/1（也有分级，不写死）
    marks.clear()
    eng2 = EventEngine(PawpilotConfig())
    eng2.on_event(lambda ev: marks.append(ev.data.get("mark"))
                  if ev.name == "distance_mark" else None)
    for rem in (8, 5, 3, 1.5, 0.5):
        eng2.feed(mk(rem))
    assert marks == [4, 2, 1], f"8km 短途也应触发 4/2/1，实际 {marks}"


def test_settings_persistence():
    """面板设置（dry_run/频率/类别）保存后可恢复。"""
    import asyncio

    from plugin.plugins.neko_pawpilot.core.runtime import PawpilotRuntime

    async def _run():
        rt = PawpilotRuntime(_FakePluginForRt(), PawpilotConfig())
        rt.set_dry_run(False)
        rt.set_frequency("active")
        rt.set_category("chatter", True)
        await rt.settings_save()
        rt2 = PawpilotRuntime(_FakePluginForRt(), PawpilotConfig())
        rt2.memory._kv = rt.memory._kv
        await rt2.settings_load()
        return rt2

    rt2 = asyncio.run(_run())
    assert rt2.cfg.dry_run is False
    assert rt2.arbiter.broadcast_frequency == "active"
    assert rt2.arbiter.broadcast_categories["chatter"] is True


class _FakePluginForRt:
    """runtime 测试用假插件（复用顶部 FakeStore）。"""
    def __init__(self):
        import logging
        self.store = FakeStore()
        self.logger = logging.getLogger("test_pawpilot_rt")
        self.persona = None
        self.config_dir = _ROOT


def test_road_level_invalid_limit():
    """限速为 0（未驾驶/主菜单）时 road_level 应为空，不显示乡道。"""
    from plugin.plugins.neko_pawpilot.core.map_kb import MapKnowledge
    kb = MapKnowledge()
    assert kb.road_level(0) == ""
    # 有效限速按档位推断（比较档位代号，避免编码问题）
    assert kb.road_level(25) != ""
    assert kb.road_level(70) != ""
    assert kb.road_level(90) != ""
    assert kb.road_level(25) != kb.road_level(90)


def test_voice_style_effects():
    """口吻切换真实生效：prompt 注入 + 闲聊间隔 + strict 模式。"""
    from plugin.plugins.neko_pawpilot.core.mood import Persona
    p = Persona()
    # 默认自然
    assert p.voice_style == "default"
    assert p.talk_interval == 900.0
    assert not p.strict_mode
    # 傲娇：prompt 注入
    assert p.set_voice_style("tsundere")
    assert "傲娇" in p.persona_hint()
    # 话痨：闲聊间隔缩短
    assert p.set_voice_style("chatty")
    assert p.talk_interval < 900.0
    assert "话痨" in p.persona_hint()
    # 严厉：strict_mode 开启
    assert p.set_voice_style("strict")
    assert p.strict_mode
    # 无效值拒绝且保留原状
    assert not p.set_voice_style("bogus")
    assert p.voice_style == "strict"


def test_voice_style_multi():
    """多口吻融合：列表生效、prompt 合并、闲聊间隔取最小、无效过滤。"""
    from plugin.plugins.neko_pawpilot.core.mood import Persona
    p = Persona()
    # 新语气可用
    assert p.set_voice_style("yandere")
    assert p.strict_mode  # 病娇严格模式
    assert p.set_voice_style("loli")
    assert p.set_voice_style("onee")
    assert p.set_voice_style("genki")
    # 多语气融合
    assert p.set_voice_styles(["yandere", "chatty"])
    assert p.voice_styles == ["yandere", "chatty"]
    hint = p.persona_hint()
    assert "病娇" in hint and "话痨" in hint  # prompt 合并
    assert p.talk_interval == 420.0  # 取最活跃（话痨 420 < 病娇 600）
    # 无效过滤
    assert p.set_voice_styles(["loli", "bogus"])
    assert p.voice_styles == ["loli"]
    # 全无效拒绝
    assert not p.set_voice_styles(["bogus1", "bogus2"])
    # snapshot 输出列表
    snap = p.snapshot()
    assert snap["voice_styles"] == ["loli"]
    assert snap["voice_labels"] == ["萝莉"]


def test_llm_fallback_chain():
    """LLM 三级链路：未配置→None，配置→尝试调用，失败→自动降级。"""
    import asyncio

    from plugin.plugins.neko_pawpilot.adapters.llm_client import LLMProvider

    async def _run():
        p = LLMProvider()
        # 未配置
        assert not p.configured
        assert await p.call("hi") is None
        # 缺 model 不算配置
        p.set_client("openai", "")
        assert not p.configured
        # 配置有效
        p.set_client("openai_compatible", "test-model", "k", "http://127.0.0.1:9/v1")
        assert p.configured
        # 调用失败 → None（降级），记录错误
        r = await p.call("你好")
        assert r is None
        assert p.snapshot()["last_error"]
        return True

    assert asyncio.run(_run())


def test_cat_pilot_snatch_limit():
    """猫娘智驾：同会话 3 次抢夺后自动交还，重接不清零。"""
    from plugin.plugins.neko_pawpilot.core.pilot import CatPilot

    class Snap:
        speed_kmh = 60
        speed_limit_kmh = 80
        fuel_percent = 60
        user_steer = 0.0
        user_throttle = 0.0
        user_brake = 0.0

    p = CatPilot()
    p._pdi = object()  # 测试桩：标记输入组件"可用"（真实 pydirectinput 仅 Windows，
                       # Linux CI 导入失败会令 offer() 误判不可用直接返回）
    p.offer()
    assert p.state == "offer"
    p.accept()
    assert p.state == "engaged"
    s = Snap()

    def tick():
        p._last_tick = 0.0
        return p.tick(s)

    def settle():
        """消耗接管沉降期（5 拍），不触发真实输入注入。"""
        p._pdi = None  # 测试环境禁用注入
        for _ in range(6):
            p._last_tick = 0.0
            p.tick(s)

    # 会话内 3 次抢夺
    for i in range(1, 4):
        settle()  # 沉降期结束
        s.user_steer = 0.8
        msg = tick()
        assert p.state == "idle"  # 每次都被让渡
        if i < 3:
            assert p._snatches == i  # 计数累计
            assert "让给你" in msg
        else:
            assert p._snatches == 0  # 第 3 次交还后清零
            assert "方向盘还你" in msg
        p.accept()
        p._last_tick = 0
        s.user_steer = 0.0
        settle()
    # 新会话清零
    p.reset_session()
    assert p._snatches == 0
    # 危险退出
    p._pdi = None
    p.accept()
    p._last_tick = 0
    s.user_steer = 0.0
    s.speed_kmh = 95
    s.speed_limit_kmh = 80
    msg = tick()
    assert p.state == "idle"
    assert "超速" in msg


def test_memory_written_on_job_start():
    """接单即写入城市/货物记忆（不依赖推送仲裁）。"""
    import asyncio

    from plugin.plugins.neko_pawpilot.adapters.telemetry_client import TruckSnapshot
    from plugin.plugins.neko_pawpilot.core.event_engine import EventEngine, TruckEvent
    from plugin.plugins.neko_pawpilot.core.runtime import PawpilotRuntime

    async def _run():
        import asyncio as _asyncio
        rt = PawpilotRuntime(_FakePluginForRt(), PawpilotConfig())
        # 测试环境无后台线程：把 _spawn 的提交目标指向当前 loop
        rt._bg_loop_ref = _asyncio.get_running_loop()
        rt.engine = EventEngine(PawpilotConfig())
        rt.engine.on_event(rt._on_event)
        s = TruckSnapshot()
        s.sdk_active = True
        s.on_job = True
        s.city_src = "Berlin"
        s.city_dst = "Hamburg"
        s.cargo = "玻璃制品"
        s.planned_distance_km = 300
        ev = TruckEvent(name="job_start", snapshot=s)
        rt._on_event(ev)
        await asyncio.sleep(0.05)
        # 接单只唤起不计数：城市/货物尚未入库
        cities0 = rt.memory.query("cities") or {}
        assert "Hamburg" not in cities0, "接单不应计数入库"
        # 到货正式入库
        ev2 = TruckEvent(name="job_delivered", snapshot=s,
                         data={"revenue": 12000})
        rt._on_event(ev2)
        await asyncio.sleep(0.05)
        await rt.memory.save()
        cities = rt.memory.query("cities") or {}
        cargos = rt.memory.query("cargos") or {}
        return cities, cargos

    cities, cargos = asyncio.run(_run())
    assert "Hamburg" in cities, "到货应记录目的城市"
    assert "玻璃制品" in cargos, "到货应记录货物"


def test_cargo_damage_threshold_regression():
    """货损感知：≥5% 才报（旧条件写反成"<5%"→ 真货损永不播报），恶化再报。"""
    from plugin.plugins.neko_pawpilot.adapters.telemetry_client import TruckSnapshot
    from plugin.plugins.neko_pawpilot.core.event_engine import EventEngine

    eng = EventEngine(PawpilotConfig())
    fired = []
    eng.on_event(lambda ev: fired.append((ev.name, ev.data)))

    def mk(**kw):
        s = TruckSnapshot()
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def cargo_hits():
        return [d for n, d in fired if n == "cargo_damage"]

    eng.feed(mk(sdk_active=True, on_job=True))
    # 轻微擦碰（3% < 5%）不报
    eng.feed(mk(sdk_active=True, on_job=True, job_cargo_damage=0.03))
    assert not cargo_hits(), "货损未达 5% 不该播报"
    # 真实货损 8%（旧代码正是在这里静默）→ 必须播报，且完好率 <95%
    eng.feed(mk(sdk_active=True, on_job=True, job_cargo_damage=0.08))
    hits = cargo_hits()
    assert hits, "货损 ≥5% 必须播报（旧版条件写反导致永不触发）"
    assert hits[-1]["pct"] < 95, f"完好率应低于 95%，实际 {hits[-1]['pct']}"
    # 恶化到 25% → 再报一次（旧版一次性 boolean 后永久静默）
    eng.feed(mk(sdk_active=True, on_job=True, job_cargo_damage=0.25))
    assert len(cargo_hits()) == 2, "货损继续恶化应再播报一次"


def test_cargo_damage_reads_trailer_channel():
    """货损感知：挂在挂车上的实时货损（trailer 通道）也必须能触发。"""
    from plugin.plugins.neko_pawpilot.adapters.telemetry_client import TruckSnapshot
    from plugin.plugins.neko_pawpilot.core.event_engine import EventEngine

    # 取值层：两路取大
    s = TruckSnapshot()
    s.job_cargo_damage = 0.0
    s.trailer_cargo_damage = 0.42
    assert abs(s.cargo_damage - 0.42) < 1e-9, "应取挂车通道的货损"
    assert s.to_dict()["cargo_damage"] == s.cargo_damage

    # 事件层：只有挂车通道有值时也要报
    eng = EventEngine(PawpilotConfig())
    names = []
    eng.on_event(lambda ev: names.append(ev.name))

    def mk(**kw):
        t = TruckSnapshot()
        for k, v in kw.items():
            setattr(t, k, v)
        return t

    eng.feed(mk(sdk_active=True, on_job=True))
    eng.feed(mk(sdk_active=True, on_job=True, trailer_cargo_damage=0.4))
    assert "cargo_damage" in names, "只看 job 通道会漏掉挂在挂车上的货损"


def test_vehicle_damage_band_event():
    """车损：累计损伤跨档要说话（旧版只有碰撞突增），修车后重新武装。"""
    from plugin.plugins.neko_pawpilot.adapters.telemetry_client import TruckSnapshot
    from plugin.plugins.neko_pawpilot.core.event_catalog import spec
    from plugin.plugins.neko_pawpilot.core.event_engine import EventEngine

    assert "vehicle_damage" in EVENT_CATALOG, "车损事件需在规格表登记"
    assert spec("vehicle_damage").category == "safety"

    eng = EventEngine(PawpilotConfig())
    hits = []
    eng.on_event(lambda ev: hits.append(ev.data) if ev.name == "vehicle_damage" else None)

    def mk(**kw):
        s = TruckSnapshot()
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    eng.feed(mk(sdk_active=True, on_job=True, wear_cabin=0.10))
    assert not hits, "10% 未到 25% 档不该报"
    eng.feed(mk(sdk_active=True, on_job=True, wear_cabin=0.30))
    assert hits and hits[-1]["percent"] >= 25, "30% 应触发车损播报"
    eng.feed(mk(sdk_active=True, on_job=True, wear_cabin=0.55))
    assert len(hits) == 2, "恶化到 50% 档应再报"
    # 修车（损伤回落）→ 重新武装，再次跨档还要报
    eng.feed(mk(sdk_active=True, on_job=True, wear_cabin=0.05))
    eng.feed(mk(sdk_active=True, on_job=True, wear_cabin=0.30))
    assert len(hits) == 3, "修车后重新跨档应再次播报"


def test_crash_reports_once_via_llm():
    """事故：只计一次（旧版重复计数）+ 必走 respond（交宿主 LLM 说话）。"""
    import asyncio

    from plugin.plugins.neko_pawpilot.adapters.telemetry_client import TruckSnapshot
    from plugin.plugins.neko_pawpilot.core.event_engine import TruckEvent
    from plugin.plugins.neko_pawpilot.core.runtime import PawpilotRuntime

    class _PushPlugin(_FakePluginForRt):
        def __init__(self):
            super().__init__()
            self.calls = []

        def push_message(self, **kw):
            self.calls.append(kw)
            return {"submitted": True}

    async def _run():
        import asyncio as _asyncio
        fp = _PushPlugin()
        rt = PawpilotRuntime(fp, PawpilotConfig())
        rt.set_dry_run(False)  # 关试运行，让推送真正落到 push_message
        rt._bg_loop_ref = _asyncio.get_running_loop()
        s = TruckSnapshot()
        s.sdk_active = True
        s.on_job = True
        s.speed_mps = 20.0
        s.wear_cabin = 0.42
        rt._on_event(TruckEvent(name="crash", snapshot=s,
                                data={"delta": 0.3, "speed_kmh": 72.0}))
        await asyncio.sleep(0.1)
        crashed = rt._job_crashes
        calls = list(fp.calls)
        # 事故链会 sleep 30s：断言后取消，别拖住测试
        for t in asyncio.all_tasks():
            if t is not _asyncio.current_task():
                t.cancel()
        return crashed, calls

    crashed, calls = asyncio.run(_run())
    assert crashed == 1, f"单次事故只能计一次，实际 {crashed}"
    respond = [c for c in calls if c.get("ai_behavior") == "respond"]
    assert respond, "事故必须走 respond 交宿主 LLM 生成台词（旧版只直出硬编码短句）"
    text = respond[0]["parts"][0]["text"]
    assert "车祸" in text and "42" in text, f"事实行应含事故与损伤，实际：{text}"


def test_crash_accel_and_window_detection():
    """碰撞双信号：① 加速度尖峰（默认 1.5g）② 时间窗累计损伤（治刮蹭漏报）。"""
    from plugin.plugins.neko_pawpilot.adapters.telemetry_client import TruckSnapshot
    from plugin.plugins.neko_pawpilot.core.event_engine import EventEngine

    def mk(**kw):
        s = TruckSnapshot()
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    # ① 加速度信号：默认 1.5g 开启；30 m/s² ≈ 3.06g → 判碰撞
    eng = EventEngine(PawpilotConfig())
    hits = []
    eng.on_event(lambda ev: hits.append(ev.data) if ev.name == "crash" else None)
    eng.feed(mk(sdk_active=True, on_job=True))
    eng.feed(mk(sdk_active=True, on_job=True, accel_x=30.0))
    assert hits and hits[-1]["source"] == "accel", "加速度尖峰应判碰撞"
    assert hits[-1]["accel_g"] >= 1.5
    # 合成值换算：30 m/s² ≈ 3.06 g
    s = TruckSnapshot()
    s.accel_x = 30.0
    assert 3.0 < s.accel_g < 3.2
    # 关掉该信号（crash_accel_g=0）→ 同样尖峰不判碰撞
    eng2 = EventEngine(PawpilotConfig({"crash_accel_g": 0}))
    names = []
    eng2.on_event(lambda ev: names.append(ev.name))
    eng2.feed(mk(sdk_active=True, on_job=True))
    eng2.feed(mk(sdk_active=True, on_job=True, accel_x=30.0))
    assert "crash" not in names, "crash_accel_g=0 时不应凭加速度判碰撞"

    # ② 时间窗累计：每帧 +2%（都低于单帧阈值 5%），累计跨阈值仍要判碰撞
    #    这正是实测事故的情形：总损伤 +3~7 点分散在多帧，旧版全程漏报
    eng3 = EventEngine(PawpilotConfig({"crash_accel_g": 0}))
    win = []
    eng3.on_event(lambda ev: win.append(ev.data) if ev.name == "crash" else None)
    eng3.feed(mk(sdk_active=True, on_job=True, wear_cabin=0.10))
    for i in range(1, 6):
        eng3.feed(mk(sdk_active=True, on_job=True, wear_cabin=0.10 + 0.02 * i))
    assert win, "累计损伤跨阈值应判碰撞（单帧判定会漏）"
    assert win[-1]["source"] == "damage"
    assert win[-1]["window_s"] == 5.0

    # ③ 单帧小抖动（+1%，远低于阈值）不该误报
    eng4 = EventEngine(PawpilotConfig({"crash_accel_g": 0}))
    soft = []
    eng4.on_event(lambda ev: soft.append(ev.name))
    eng4.feed(mk(sdk_active=True, on_job=True, wear_cabin=0.100))
    eng4.feed(mk(sdk_active=True, on_job=True, wear_cabin=0.103))
    eng4.feed(mk(sdk_active=True, on_job=True, wear_cabin=0.106))
    assert "crash" not in soft, "正常磨损增量不该误判碰撞"


def test_place_book_learn_and_passing():
    """地点簿：自学习地名坐标 → 途经判定（含冷却）。"""
    from plugin.plugins.neko_pawpilot.core.memory import MemoryStore
    from plugin.plugins.neko_pawpilot.core.places import PlaceBook

    book = PlaceBook(MemoryStore(FakeStore()))
    assert book.count() == 0
    assert book.learn("Berlin", 10000.0, 20000.0, "city") is True
    assert book.learn("", 1.0, 2.0) is False          # 空名不记
    assert book.learn("Berlin", 10000.0, 20000.0, "city") is True   # 再访计数
    assert book.count() == 1
    assert book.query_visits("Berlin") == 2

    near = book.nearest(10050.0, 20050.0, max_km=6.0)
    assert near and near["name"] == "Berlin"
    assert near["distance_km"] < 0.1
    # 远处找不到
    assert book.nearest(900000.0, 900000.0, max_km=6.0) is None

    # 途经：首次命中 → 播报；立刻再来 → 冷却挡住；超过冷却 → 再播
    hit = book.passing(10020.0, 20020.0, now=1000.0)
    assert hit and hit["name"] == "Berlin"
    assert book.passing(10020.0, 20020.0, now=1001.0) is None
    assert book.passing(10020.0, 20020.0, now=1000.0 + 1801.0) is not None


def test_radio_dj_segments():
    """猫娘电台：按时段选节目、间隔生效、事实行含节目要求。"""
    from plugin.plugins.neko_pawpilot.adapters.telemetry_client import TruckSnapshot
    from plugin.plugins.neko_pawpilot.core.config_model import PawpilotConfig
    from plugin.plugins.neko_pawpilot.core.radio import RadioDJ

    class Snap(TruckSnapshot):
        pass

    s = Snap()
    s.sdk_active = True
    s.on_job = True
    s.world_x, s.world_z = 1000.0, 2000.0
    s.time_abs_min = 2 * 60 + 30      # 深夜 02:30
    s.route_distance_km = 120000.0
    s.speed_mps = 22.0
    s.map_scale = 19.0
    dj = RadioDJ(PawpilotConfig())
    brief = dj.pick(s, now=1000.0, driving_since=0.0)
    assert brief and "猫娘电台" in brief
    assert "深夜节目" in brief or "night" in brief
    # 间隔未到 → 不再播
    assert dj.pick(s, now=1005.0, driving_since=0.0) is None
    # 未接单 → 不播
    s.on_job = False
    assert dj.pick(s, now=1000.0 + 700.0, driving_since=0.0) is None


def test_content_fact_builders():
    """开场故事 / 到达文化 / 途经介绍：事实行必须带上地名与要求。"""
    import asyncio

    from plugin.plugins.neko_pawpilot.adapters.telemetry_client import TruckSnapshot
    from plugin.plugins.neko_pawpilot.core.config_model import PawpilotConfig
    from plugin.plugins.neko_pawpilot.core.runtime import PawpilotRuntime

    async def _run():
        rt = PawpilotRuntime(_FakePluginForRt(), PawpilotConfig())
        s = TruckSnapshot()
        s.sdk_active = True
        s.on_job = True
        s.city_src = "Lyon"
        s.city_dst = "Milano"
        s.cargo = "易碎品"
        s.planned_distance_km = 620
        s.time_abs_min = 7 * 60
        opening = rt._opening_story_fact(s, ["上次这条线你超速了 3 次"])
        arrival = rt._arrival_culture_fact(s)
        passing = rt._passing_place_fact({"name": "Basel", "distance_km": 2.4, "visits": 3})
        return opening, arrival, passing

    opening, arrival, passing = asyncio.run(_run())
    for text, must in ((opening, "Lyon"), (opening, "Milano"), (opening, "易碎品")):
        assert must in text, f"开场事实行应含 {must}: {text}"
    assert "地域特色" in opening, "开场应要求介绍起点文化"
    assert "风土" in arrival and "Milano" in arrival, "到达应要求介绍当地风土"
    assert "Basel" in passing and "特点" in passing, "途经应要求讲该地特点"


def test_ferry_train_and_mountain_pass_events():
    """渡轮/火车（带真实站名）与山口（海拔变化）事件。"""
    from plugin.plugins.neko_pawpilot.adapters.telemetry_client import TruckSnapshot
    from plugin.plugins.neko_pawpilot.core.config_model import PawpilotConfig
    from plugin.plugins.neko_pawpilot.core.event_catalog import EVENT_CATALOG
    from plugin.plugins.neko_pawpilot.core.event_engine import EventEngine

    assert "ferry" in EVENT_CATALOG and "train" in EVENT_CATALOG
    assert "mountain_pass" in EVENT_CATALOG

    def mk(**kw):
        s = TruckSnapshot()
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    eng = EventEngine(PawpilotConfig())
    got = []
    eng.on_event(lambda ev: got.append((ev.name, ev.data)))
    eng.feed(mk(sdk_active=True, on_job=True))
    eng.feed(mk(sdk_active=True, on_job=True, ev_ferry=True,
                ferry_source="Calais", ferry_target="Dover"))
    names = [n for n, _ in got]
    assert "ferry" in names, "上渡轮应触发事件"
    data = dict(got)["ferry"]
    assert data["source"] == "Calais" and data["target"] == "Dover"

    # 火车
    eng.feed(mk(sdk_active=True, on_job=True, ev_ferry=True,
                ferry_source="Calais", ferry_target="Dover", ev_train=True,
                train_source="Hamburg", train_target="Stockholm"))
    assert "train" in [n for n, _ in got]

    # 山口：180 秒窗口内海拔爬升 > 阈值
    eng2 = EventEngine(PawpilotConfig())
    passes = []
    eng2.on_event(lambda ev: passes.append(ev.data) if ev.name == "mountain_pass" else None)
    eng2.feed(mk(sdk_active=True, on_job=True, world_y=50.0))
    for i in range(1, 5):
        eng2.feed(mk(sdk_active=True, on_job=True, world_y=50.0 + 40.0 * i))
    assert passes, "海拔变化超阈值应触发山口事件"
    assert passes[-1]["climb_m"] >= 120.0
    assert passes[-1]["direction"] in ("up", "down")


if __name__ == "__main__":
    test_manifest()
    print("manifest OK")
    test_config_section()
    print("config OK")
    test_event_engine_synthetic()
    print("event engine OK")
    test_emotion_layer()
    print("emotion OK")
    test_memory_system()
    print("memory OK")
    test_event_catalog()
    print("catalog OK")
    test_scenario_machine()
    print("scenario OK")
    test_safety_guard()
    print("safety OK")
    test_arbiter()
    print("arbiter OK")
    test_ledger()
    print("ledger OK")
    test_challenge()
    print("challenge OK")
    test_trip_summary()
    print("summary OK")
    test_knowledge()
    print("knowledge OK")
    test_proactive()
    print("proactive OK")
    test_profile()
    print("profile OK")
    test_small_talk()
    print("small_talk OK")
