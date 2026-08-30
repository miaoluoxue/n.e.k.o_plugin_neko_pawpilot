"""欧卡知识库：卡车/驾驶技巧/游戏机制/货运经济/城市距离查询。

知识分主题，玩家问到时按关键词匹配返回；猫娘聊天时也可引用。
"""

from __future__ import annotations

import random
from typing import Any, Dict, Optional

# 常用卡车参数（品牌 → 引擎/油箱/特点）
TRUCKS: Dict[str, Dict[str, Any]] = {
    "DAF": {"engines": "PACCAR MX-13", "fuel_capacity": 1300, "note": "欧洲主流，省油舒适"},
    "DAF XD": {"engines": "PACCAR MX-11", "fuel_capacity": 430, "note": "中长途全能"},
    "SCANIA": {"engines": "DC13/DC16 V8", "fuel_capacity": 1300, "note": "V8 声浪，动力强劲"},
    "VOLVO": {"engines": "D13/D16", "fuel_capacity": 1300, "note": "安全配置最全"},
    "MAN": {"engines": "D26/D38", "fuel_capacity": 1300, "note": "德系扎实"},
    "MERCEDES-BENZ": {"engines": "OM471", "fuel_capacity": 1300, "note": "舒适度标杆"},
    "IVECO": {"engines": "Cursor 13", "fuel_capacity": 1300, "note": "性价比之选"},
    "RENAULT": {"engines": "DE13", "fuel_capacity": 1300, "note": "法系浪漫"},
}

# 省油技巧（知识库问答）
FUEL_TIPS = [
    "保持经济转速 1100-1400 rpm，别让转速拉满",
    "提前预判刹车，少踩急刹，利用发动机制动",
    "巡航控制比脚踩更省油，高速上开起来",
    "胎压和挂车阻力影响油耗，别超重",
    "上坡前提前加速，别在坡上硬踩油门",
]

# 货运经济常识
CARGO_ECONOMY = {
    "重货": "钢材/木材这类重货单价高但油耗大，长途收益要看净赚",
    "轻货": "电子/日用品轻，省油，适合长途",
    "易碎": "玻璃/精密仪器怕颠，损坏要扣钱，稳着开",
    "冷藏": "冷链货物有温度要求，中途停车别太久",
}

# 城市间常用距离（km，欧洲主干线）
CITY_DISTANCES: Dict[str, Dict[str, int]] = {
    "Berlin": {"Hamburg": 280, "Munich": 580, "Frankfurt": 545, "Warsaw": 570},
    "Paris": {"Berlin": 1050, "Marseille": 770, "Lyon": 465},
    "London": {"Paris": 450, "Berlin": 1100},
    "Amsterdam": {"Berlin": 650, "Paris": 500},
    "Rome": {"Milan": 580, "Paris": 1400},
}

# ── 游戏机制知识（按主题，关键词命中 → 答复）──
GAME_KNOWLEDGE: Dict[str, Dict[str, Any]] = {
    "罚款": {
        "keywords": ("罚款", "罚单", "违章", "扣钱", "超速相机", "红灯"),
        "answer": "罚款主要来自超速相机、闯红灯和事故。超速相机拍了要扣钱，"
                  "路口红灯一定等绿灯喵。撞车罚款按损伤算，小心开省下的都是净赚！",
    },
    "疲劳": {
        "keywords": ("疲劳", "困", "强制休息", "睡觉", "休息"),
        "answer": "连续驾驶太久会触发强制休息，游戏要求去服务区/停车区睡觉。"
                  "困了别硬撑，疲劳驾驶罚款很重喵~",
    },
    "货物损坏": {
        "keywords": ("货物损坏", "完好率", "损伤", "货损", "碎了"),
        "answer": "货物损坏按完好率扣钱，撞车/急刹都会损伤货物。玻璃、精密仪器"
                  "最怕颠，提前刹车保持平稳喵。到货完好率越高收入越高！",
    },
    "升级": {
        "keywords": ("升级", "改装", "引擎", "变速箱", "车库", "买新车"),
        "answer": "升级顺序建议：先引擎（动力）→ 变速箱（省油）→ 刹车/轮胎（安全）。"
                  "赚到钱先去车行看看，引擎升级长途明显轻松喵~",
    },
    "档位": {
        "keywords": ("档位", "换挡", "挂挡", "手动挡", "离合器"),
        "answer": "起步一档，转速到 1500 左右升档；重货用低档位起步更稳。"
                  "如果不会手动挡，自动挡也能开，但手动挡爬坡更省油喵。",
    },
    "收费站": {
        "keywords": ("收费站", "过路费", "高速费", "toll"),
        "answer": "高速有收费站，按里程收费。赶时间走高速，不赶走国道免费"
                  "但慢。欧洲各国收费不一样，边境附近注意喵~",
    },
    "天气": {
        "keywords": ("天气", "下雨", "下雪", "雨天", "雪天", "雾"),
        "answer": "雨天路滑刹车距离变长，雪天更容易打滑。下雨记得开雨刷，"
                  "大雾开雾灯慢行。恶劣天气事故率高，小心喵！",
    },
    "燃料": {
        "keywords": ("加油", "柴油", "油量", "没油", "断油"),
        "answer": "油量低到红线就该找加油站，高速服务区基本都有。"
                  "柴油车别开到彻底没油，抛锚叫救援要花大钱喵~",
    },
    "任务选择": {
        "keywords": ("接单", "任务选择", "选任务", "什么任务", "跑什么"),
        "answer": "选任务看三样：每公里单价、货物类型、目的地。重货短途单价高，"
                  "轻货长途总量大；新城市解锁新区域，平衡着来喵~",
    },
    "收入": {
        "keywords": ("怎么赚钱", "赚钱", "收入", "收益", "利润"),
        "answer": "赚钱核心：接高价单（看每公里 €）+ 保持货物完好 + 别违章罚款。"
                  "升级引擎后接更重的货，长途单价更高喵！",
    },
    "品牌": {
        "keywords": ("哪个牌子", "什么牌子好", "买哪款", "卡车推荐", "推荐卡车"),
        "answer": "各品牌各有特点：斯堪尼亚 V8 动力猛，沃尔沃安全配置全，"
                  "奔驰舒适，DAF 省油。预算有限选 IVECO，追求体验上斯堪尼亚喵~",
    },
}

# 驾驶技巧知识（补充省油之外的技巧）
DRIVE_TIPS = [
    "高速巡航用定速巡航，右脚放松还省油",
    "下长坡用发动机制动/缓速器，别一直踩刹车，刹车会过热",
    "弯道提前减速入弯，出弯再加速，重车更容易侧翻",
    "超车留足距离，大车盲区大，别贴太近",
    "进服务区提前打转向灯，后面车多别急刹",
    "夜间会车用近光灯，远光会晃到对面司机",
]


class KnowledgeBase:
    """欧卡知识库查询。"""

    def __init__(self) -> None:
        self.trucks = TRUCKS
        self.fuel_tips = FUEL_TIPS
        self.cargo = CARGO_ECONOMY
        self.distances = CITY_DISTANCES
        self.game = GAME_KNOWLEDGE
        self.drive_tips = DRIVE_TIPS

    def truck_info(self, brand: str) -> Optional[str]:
        """查卡车参数。"""
        for key, info in self.trucks.items():
            if brand and key.lower() in brand.lower():
                return (f"{key}：{info['engines']}，油箱 {info['fuel_capacity']}L，"
                        f"{info['note']}")
        return None

    def fuel_tip(self) -> str:
        """随机一条省油技巧。"""
        return random.choice(self.fuel_tips)

    def drive_tip(self) -> str:
        """随机一条驾驶技巧。"""
        return random.choice(self.drive_tips)

    CARGO_KEYWORDS = {
        "重货": ("steel", "钢材", "木材", "矿石", "铁"),
        "易碎": ("glass", "玻璃", "精密", "电子", "显示器"),
        "冷藏": ("冷藏", "冷链", "食品", "牛奶"),
        "轻货": ("电子", "日用品", "衣物"),
    }

    def cargo_tip(self, cargo: str = "") -> str:
        """按货物给建议。"""
        if not cargo:
            return "货物运输注意控制车速和制动，稳字当头"
        for kind, keywords in self.CARGO_KEYWORDS.items():
            if any(k in cargo for k in keywords):
                return f"{cargo}是{kind}，{self.cargo[kind]}"
        return "货物运输注意控制车速和制动，稳字当头"

    def game_tip(self, text: str) -> Optional[str]:
        """按玩家提问匹配游戏机制知识。"""
        if not text:
            return None
        for topic, data in self.game.items():
            if any(k in text for k in data["keywords"]):
                return data["answer"]
        # 技巧/知识兜底：随机驾驶技巧
        if "技巧" in text or "怎么开" in text or "怎么驾驶" in text:
            return self.drive_tip()
        return None

    def city_distance(self, a: str, b: str) -> Optional[int]:
        """查两城距离。"""
        for city, table in self.distances.items():
            if a and city.lower() in a.lower():
                for other, dist in table.items():
                    if b and other.lower() in b.lower():
                        return dist
        return None

    def route_tip(self, src: str, dst: str) -> Optional[str]:
        """路线建议：查得到距离就报。"""
        dist = self.city_distance(src, dst)
        if not dist:
            return None
        hours = dist / 65  # 平均 65km/h 估算
        return f"{src}到{dst}约 {dist} km，不休息大概 {hours:.1f} 小时喵"

    def best_city_tip(self, dst: str = "") -> str:
        """城市收益建议（根据已知距离推性价比）。"""
        if dst:
            for city, table in self.distances.items():
                if dst and city.lower() in dst.lower():
                    longest = max(table.values())
                    return (f"{dst}是个大枢纽喵，跑长途收益高；"
                            f"比如到{list(table.keys())[0]}约 {list(table.values())[0]} km")
        return "欧洲货运看距离和货物类型喵，重货短途、轻货长途最划算"

    def fleet_tip(self, ledger: dict) -> str:
        """车队经营建议（按本月净赚）。"""
        net = float((ledger or {}).get("net", 0))
        if net > 20000:
            return ("本月净赚超 2 万 € 喵，可以考虑买第二台车雇司机跑路线了！"
                    "长期收益比单跑高")
        if net > 8000:
            return "收入不错喵，但先升级引擎/变速箱比买第二台车更划算~"
        return "现在现金流还紧喵，先攒钱把第一台车养好，别急着扩张车队~"
