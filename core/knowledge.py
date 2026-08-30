"""欧卡知识库：卡车/省油/货运经济/城市距离查询。"""

from __future__ import annotations

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


class KnowledgeBase:
    """欧卡知识库查询。"""

    def __init__(self) -> None:
        self.trucks = TRUCKS
        self.fuel_tips = FUEL_TIPS
        self.cargo = CARGO_ECONOMY
        self.distances = CITY_DISTANCES

    def truck_info(self, brand: str) -> Optional[str]:
        """查卡车参数。"""
        for key, info in self.trucks.items():
            if brand and key.lower() in brand.lower():
                return (f"{key}：{info['engines']}，油箱 {info['fuel_capacity']}L，"
                        f"{info['note']}")
        return None

    def fuel_tip(self) -> str:
        """随机一条省油技巧。"""
        import random
        return random.choice(self.fuel_tips)

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
            # 从城市距离表找这个城市能到的路线
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
