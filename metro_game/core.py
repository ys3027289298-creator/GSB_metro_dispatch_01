"""地铁调度文字游戏核心逻辑。

包含线路（直线/环线）、车站（换乘/临时封站）、列车（信号区间占用/
容量/故障降速/取消）、换乘客流、回合推进、撤销与 JSON 存档。

核心不变量：

1. 同一时刻到达的换乘客流会在同一站停服务周期内完成换乘，不会被跳过。
2. 信号区间同一时刻最多被一列移动中的列车占用；取消列车立即释放。
3. 环线行程按邻接图最短路计算，每个车站只计一次，末站不重复。
4. 列车在任何时刻载客数都不超过容量，登车按剩余座位截断。
5. 故障列车按降速后的运行时长推进，并据实累计延误分钟数。
6. 临时封站同时影响显示与寻路：封闭站从路径与邻接中剔除。
7. 同一回合（请求编号）重复提交只结算一次，回合时钟不重复推进。
8. 撤销基于全量快照，时钟、延误分钟数与满意度一并恢复。
"""
from __future__ import annotations

import copy
import json
import random
from collections import deque

DEFAULT_SEED = 20260929
SAVE_VERSION = 1

BASE_TRAVEL_MINUTES = 2      # 每个区间的基准运行分钟数
BREAKDOWN_SPEED_FACTOR = 2   # 故障时运行时长倍数（降速）
DELAY_PENALTY_PER_MIN = 2    # 每分钟延误扣除的满意度
WAITING_PENALTY_PER_PAX = 1  # 每个尚未送达乘客在回合结算时扣除的满意度
INITIAL_SATISFACTION = 100
RANDOM_PAX_CHANCE = 0.15     # 每分钟随机客流事件概率（固定种子可复现）

LINE_1 = ("1号线", ["S1", "S2", "S3", "S4", "S5", "S6"], False)
LINE_2_LOOP = ("2号线", ["S3", "T1", "T2", "T3"], True)

TRAIN_DOCKED = "docked"
TRAIN_MOVING = "moving"
TRAIN_CANCELLED = "cancelled"


class Station:
    """地铁车站，可属于多条线路（换乘站）。"""

    def __init__(self, name, lines=None, closed=False):
        self.name = name
        self.lines = list(lines or [])
        self.closed = closed

    def to_dict(self):
        return {"name": self.name, "lines": list(self.lines),
                "closed": self.closed}

    @classmethod
    def from_dict(cls, data):
        return cls(data["name"], lines=data.get("lines", []),
                   closed=data.get("closed", False))


class Line:
    """线路。loop=True 时末站与首站相邻形成环线。"""

    def __init__(self, name, stations, loop=False):
        self.name = name
        self.stations = list(stations)
        self.loop = loop

    def index(self, station_name):
        try:
            return self.stations.index(station_name)
        except ValueError:
            return -1

    def advance_index(self, idx, direction):
        """返回 (新下标, 新方向)。环线取模；直线到端点自动折返。"""
        n = len(self.stations)
        if n == 0:
            return idx, direction
        if self.loop:
            return (idx + direction) % n, direction
        nxt = idx + direction
        if nxt < 0 or nxt >= n:
            direction = -direction
            nxt = idx + direction
        return nxt, direction

    def to_dict(self):
        return {"name": self.name, "stations": list(self.stations),
                "loop": self.loop}

    @classmethod
    def from_dict(cls, data):
        return cls(data["name"], data["stations"], loop=data.get("loop", False))


class Passenger:
    """乘客。arrive_time 非 None 表示已送达。"""

    def __init__(self, pid, origin, dest, arrival_time=0,
                 board_time=None, served_time=None, on_train=None):
        self.pid = pid
        self.origin = origin
        self.dest = dest
        self.arrival_time = arrival_time  # 进入站台（可换乘）的时刻
        self.board_time = board_time      # 本次登车时刻
        self.served_time = served_time    # 送达时刻
        self.on_train = on_train

    @property
    def delivered(self):
        return self.served_time is not None

    def to_dict(self):
        return {
            "pid": self.pid, "origin": self.origin, "dest": self.dest,
            "arrival_time": self.arrival_time, "board_time": self.board_time,
            "served_time": self.served_time, "on_train": self.on_train,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["pid"], data["origin"], data["dest"],
            arrival_time=data.get("arrival_time", 0),
            board_time=data.get("board_time"),
            served_time=data.get("served_time"),
            on_train=data.get("on_train"),
        )


class Train:
    """列车：在某条线路上按 direction 运行，占用信号区间。"""

    def __init__(self, tid, line_name, station, capacity=4, direction=1,
                 status=TRAIN_DOCKED, eta=0, broken=False,
                 from_idx=None, to_idx=None, onboard=None, etd=None):
        self.tid = tid
        self.line_name = line_name
        self.station = station  # docked 时所在站；moving 时为出发站
        self.capacity = capacity
        self.direction = direction
        self.status = status
        self.eta = eta
        self.broken = broken
        self.from_idx = from_idx
        self.to_idx = to_idx
        self.onboard = list(onboard or [])
        self.etd = etd

    @property
    def load(self):
        return len(self.onboard)

    @property
    def is_moving(self):
        return self.status == TRAIN_MOVING

    def travel_minutes(self):
        minutes = BASE_TRAVEL_MINUTES
        if self.broken:
            minutes *= BREAKDOWN_SPEED_FACTOR
        return minutes

    def to_dict(self):
        return {
            "tid": self.tid, "line_name": self.line_name,
            "station": self.station, "capacity": self.capacity,
            "direction": self.direction, "status": self.status,
            "eta": self.eta, "broken": self.broken,
            "from_idx": self.from_idx, "to_idx": self.to_idx,
            "etd": self.etd,
            "onboard": list(self.onboard),
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["tid"], data["line_name"], data["station"],
            capacity=data.get("capacity", 4),
            direction=data.get("direction", 1),
            status=data.get("status", TRAIN_DOCKED),
            eta=data.get("eta", 0),
            broken=data.get("broken", False),
            from_idx=data.get("from_idx"),
            to_idx=data.get("to_idx"),
            onboard=data.get("onboard", []),
            etd=data.get("etd"),
        )


class Game:
    """地铁调度游戏主控。"""

    def __init__(self, seed=DEFAULT_SEED):
        self.seed = seed
        self.rng = random.Random(seed)
        self.clock = 0
        self.stations = {}       # name -> Station
        self.lines = {}          # name -> Line
        self.trains = {}         # tid -> Train
        self.waiting = {}        # station -> [Passenger]（FIFO）
        self.all_passengers = {}  # pid -> Passenger
        self.occupancy = set()   # {(line_name, from_idx, to_idx)}
        self.delays = 0          # 累计延误分钟数
        self.satisfaction = INITIAL_SATISFACTION
        self.delivered_count = 0
        self.last_error = ""
        self._pax_seq = 0
        self._train_seq = 0
        self._req_seq = 0
        self._seen_requests = set()
        self._settled_clocks = set()  # 已结算的回合，重复结算被忽略
        self._undo_stack = []
        self._setup_default()

    # ---- 默认网络 ----

    def _setup_default(self):
        for line_name, station_names, loop in (LINE_1, LINE_2_LOOP):
            self.add_line(line_name, station_names, loop=loop)
        self.add_train("t1", "1号线", "S1", capacity=3)
        self.add_train("t2", "2号线", "T1", capacity=3)

    def add_line(self, name, station_names, loop=False):
        self.lines[name] = Line(name, station_names, loop=loop)
        for station_name in station_names:
            station = self.stations.get(station_name)
            if station is None:
                station = Station(station_name)
                self.stations[station_name] = station
            if name not in station.lines:
                station.lines.append(name)
        return self.lines[name]

    def add_train(self, tid, line_name, station, capacity=4, direction=1):
        line = self.lines[line_name]
        train = Train(tid, line_name, station, capacity=capacity,
                      direction=direction)
        train.station = line.stations[line.advance_index(
            line.index(station), 0)[0]]
        self.trains[tid] = train
        return train

    # ---- 邻接 / 寻路（封站与环线） ----

    def is_closed(self, station_name):
        station = self.stations.get(station_name)
        return station is None or station.closed

    def neighbors(self, station_name):
        """开放邻站（保持线路顺序，结果确定）。环线末站不重复出现。"""
        result = []
        for line in self.lines.values():
            idx = line.index(station_name)
            if idx < 0:
                continue
            order = line.stations
            count = len(order)
            if line.loop and count > 1:
                candidates = ((idx - 1) % count, (idx + 1) % count)
            else:
                candidates = (cand for cand in (idx - 1, idx + 1)
                              if 0 <= cand < count)
            for cand in candidates:
                other = order[cand]
                if other == station_name:
                    continue
                if not self.is_closed(other) and other not in result:
                    result.append(other)
        return result

    def find_route(self, origin, dest):
        """BFS 最短路（按站名访问，环线末站不会被重复计入）。

        封闭车站不可作为起点、终点或中途节点；不可达返回 None。
        """
        if origin == dest:
            return [origin]
        if self.is_closed(origin) or self.is_closed(dest):
            return None
        prev = {origin: None}
        queue = deque([origin])
        while queue:
            current = queue.popleft()
            for nxt in self.neighbors(current):
                if nxt in prev:
                    continue
                prev[nxt] = current
                if nxt == dest:
                    route = [dest]
                    while prev[route[-1]] is not None:
                        route.append(prev[route[-1]])
                    route.reverse()
                    return route
                queue.append(nxt)
        return None

    def ride_for(self, passenger, train):
        """乘客乘坐该列车的一站行程；无法乘坐返回 None。

        必须同时满足：乘客当前位置开放、目的站可达、路径第二站与列车
        下一站一致、且该站与当前站同属列车线路（保证可在线路上移动）。
        """
        if self.is_closed(passenger.origin) or self.is_closed(passenger.dest):
            return None
        route = self.find_route(passenger.origin, passenger.dest)
        if not route or len(route) < 2:
            return None
        line = self.lines[train.line_name]
        if line.index(passenger.origin) < 0 or line.index(route[1]) < 0:
            return None
        next_idx, _ = line.advance_index(line.index(train.station),
                                         train.direction)
        if line.stations[next_idx] != route[1]:
            return None
        return route

    # ---- 客流 ----

    def add_passenger(self, pid, origin, dest, arrival_time=None):
        """登记乘客并进入站台；重复 pid 幂等。"""
        existing = self.all_passengers.get(pid)
        if existing is not None:
            return existing
        if origin not in self.stations or dest not in self.stations:
            self.last_error = "bad_station"
            return None
        passenger = Passenger(
            pid, origin, dest,
            arrival_time=self.clock if arrival_time is None else arrival_time,
        )
        self.all_passengers[pid] = passenger
        self.waiting.setdefault(origin, []).append(passenger)
        return passenger

    def _random_arrivals(self):
        """固定种子下可复现的随机到站客流。"""
        names = [n for n, s in self.stations.items() if not s.closed]
        if len(names) < 2 or self.rng.random() >= RANDOM_PAX_CHANCE:
            return
        origin = self.rng.choice(names)
        dest = self.rng.choice([n for n in names if n != origin])
        if self.find_route(origin, dest) is None:
            return
        self._pax_seq += 1
        self.add_passenger("p%d" % self._pax_seq, origin, dest)

    # ---- 信号区间 ----

    @staticmethod
    def _section_key(line_name, from_idx, to_idx):
        """区间占用键：同一区间双向视为同一物理闭塞分区。"""
        return (line_name, min(from_idx, to_idx), max(from_idx, to_idx))

    # ---- 故障 / 发车 / 取消 ----

    def set_breakdown(self, tid, broken=True):
        """设置/解除故障降速。已在区间内时，故障立即延长到站时间。"""
        train = self.trains.get(tid)
        if train is None:
            self.last_error = "not_found"
            return False
        if broken and not train.broken and train.is_moving:
            extra = BASE_TRAVEL_MINUTES * (BREAKDOWN_SPEED_FACTOR - 1)
            train.eta += extra
        train.broken = broken
        return True

    def repair(self, tid):
        return self.set_breakdown(tid, broken=False)

    def dispatch(self, tid):
        """从停靠站发车。封站或区间被占用时拒绝，状态保持不变。"""
        train = self.trains.get(tid)
        if train is None:
            self.last_error = "not_found"
            return False
        if train.status != TRAIN_DOCKED:
            self.last_error = "not_docked"
            return False
        line = self.lines[train.line_name]
        idx = line.index(train.station)
        next_idx, next_direction = line.advance_index(idx, train.direction)
        target = line.stations[next_idx]
        if self.is_closed(target):
            self.last_error = "station_closed"
            return False
        key = self._section_key(line.name, idx, next_idx)
        if key in self.occupancy:
            self.last_error = "signal_busy"
            return False
        self._push_undo()
        train.direction = next_direction
        self._board(train, self.clock)
        train.from_idx = idx
        train.to_idx = next_idx
        train.etd = self.clock
        train.eta = self.clock + train.travel_minutes()
        train.status = TRAIN_MOVING
        self.occupancy.add(key)
        return True

    def cancel_train(self, tid):
        """取消列车：立即释放信号区间；车上乘客回站台等待。幂等。"""
        train = self.trains.get(tid)
        if train is None:
            return False
        if train.status == TRAIN_CANCELLED:
            return False
        self._push_undo()
        if train.is_moving and train.from_idx is not None:
            key = self._section_key(train.line_name,
                                    train.from_idx, train.to_idx)
            self.occupancy.discard(key)
        station = train.station
        for pid in train.onboard:
            passenger = self.all_passengers[pid]
            passenger.origin = station
            passenger.arrival_time = self.clock
            passenger.board_time = None
            passenger.on_train = None
            self.waiting.setdefault(station, []).append(passenger)
        train.status = TRAIN_CANCELLED
        train.onboard = []
        del self.trains[tid]
        return True

    # ---- 封站 ----

    def close_station(self, name, closed=True):
        """临时封站：同时影响显示（closed 标记）与寻路（邻接剔除）。"""
        station = self.stations.get(name)
        if station is None:
            self.last_error = "not_found"
            return False
        self._push_undo()
        station.closed = closed
        return True

    def reopen_station(self, name):
        return self.close_station(name, closed=False)

    # ---- 到站、换乘与登车 ----

    def _alight_or_stay(self, train, now):
        """到站后：到站乘客送达；不能继续乘坐本车的乘客下车换乘。"""
        station = train.station
        line = self.lines[train.line_name]
        staying = []
        for pid in train.onboard:
            passenger = self.all_passengers[pid]
            if passenger.dest == station:
                passenger.served_time = now
                passenger.on_train = None
                self.delivered_count += 1
                continue
            passenger.origin = station
            if self.ride_for(passenger, train) is not None:
                staying.append(pid)
            else:
                # 换乘客流：到达时刻记为当前时刻（含同一时刻）。
                passenger.arrival_time = now
                passenger.board_time = None
                passenger.on_train = None
                self.waiting.setdefault(station, []).append(passenger)
        train.onboard = staying

    def _board(self, train, now):
        """按 FIFO 与剩余容量截断层，列车绝不超载。"""
        queue = self.waiting.get(train.station)
        if not queue:
            return
        remaining = list(queue)
        queue.clear()
        for passenger in remaining:
            if train.load >= train.capacity:
                queue.append(passenger)
                continue
            # 同一时刻到达（含换乘客流）必须参与本轮登车，故用 <=。
            if (passenger.arrival_time <= now
                    and self.ride_for(passenger, train) is not None):
                passenger.board_time = now
                passenger.on_train = train.tid
                train.onboard.append(passenger.pid)
            else:
                queue.append(passenger)

    def _service_station(self, station_name, now):
        """先完成本时刻所有到站下车/换乘，再统一组织所有停靠列车登车。"""
        docked = [t for t in self.trains.values()
                  if t.status == TRAIN_DOCKED and t.station == station_name]
        for train in docked:
            self._board(train, now)

    def _arrive(self, train, now):
        line = self.lines[train.line_name]
        key = self._section_key(line.name, train.from_idx, train.to_idx)
        self.occupancy.discard(key)
        train.station = line.stations[train.to_idx]
        train.from_idx = train.to_idx = None
        train.status = TRAIN_DOCKED
        actual = now - (train.etd if train.etd is not None else now)
        if actual > BASE_TRAVEL_MINUTES:
            delay = actual - BASE_TRAVEL_MINUTES
            self.delays += delay
            self.satisfaction = max(
                0, self.satisfaction - DELAY_PENALTY_PER_MIN * delay
            )
        train.etd = None
        self._alight_or_stay(train, now)

    # ---- 回合推进（重复输入幂等） ----

    def advance(self, minutes=1, request_id=None):
        """推进分钟数；同一 request_id 重复提交只生效一次。"""
        if request_id is not None:
            if request_id in self._seen_requests:
                return False
            self._seen_requests.add(request_id)
        self._push_undo()
        for _ in range(max(1, minutes)):
            self.clock += 1
            now = self.clock
            self._random_arrivals()
            arriving = sorted(
                (t for t in self.trains.values()
                 if t.is_moving and t.eta <= now),
                key=lambda t: (t.eta, t.tid),
            )
            served_stations = []
            for train in arriving:
                self._arrive(train, now)
                if train.station not in served_stations:
                    served_stations.append(train.station)
            for station_name in served_stations:
                self._service_station(station_name, now)
            docked_stations = sorted({
                t.station for t in self.trains.values()
                if t.status == TRAIN_DOCKED
            })
            for station_name in docked_stations:
                if station_name not in served_stations:
                    self._service_station(station_name, now)
        return True

    # ---- 结算（每回合最多一次） ----

    def settle(self, request_id=None):
        """结算当前回合：等待中的乘客产生满意度惩罚。每回合只结算一次。"""
        if self.clock in self._settled_clocks:
            return self._settlement()
        if request_id is not None and request_id in self._seen_requests:
            return self._settlement()
        if request_id is not None:
            self._seen_requests.add(request_id)
        self._push_undo()
        waiting_count = sum(
            1 for p in self.all_passengers.values() if not p.delivered
        )
        self.satisfaction = max(
            0,
            self.satisfaction - WAITING_PENALTY_PER_PAX * waiting_count,
        )
        self._settled_clocks.add(self.clock)
        return self._settlement()

    def _settlement(self):
        return {
            "clock": self.clock,
            "delivered": self.delivered_count,
            "waiting": sum(
                1 for p in self.all_passengers.values() if not p.delivered
            ),
            "delays": self.delays,
            "satisfaction": self.satisfaction,
        }

    def report(self):
        s = self._settlement()
        return (
            "结算: 时刻={clock} 送达={delivered} 等待={waiting} "
            "延误={delays} 满意度={satisfaction}"
        ).format(**s)

    def status(self):
        trains = " ".join(
            "%s@%s(%s%s)" % (
                t.tid, t.station,
                {"moving": "运行", "docked": "停靠",
                 "cancelled": "取消"}.get(t.status, t.status),
                "/故障" if t.broken else "",
            )
            for t in self.trains.values()
        )
        closed = ",".join(n for n, s in self.stations.items() if s.closed)
        return ("时刻=%d 列车[%s] 封闭=[%s] 延误=%d 满意度=%d"
                % (self.clock, trains, closed, self.delays,
                   self.satisfaction))

    # ---- 撤销（全量快照） ----

    def _push_undo(self):
        self._undo_stack.append({
            "clock": self.clock,
            "stations": copy.deepcopy(self.stations),
            "lines": copy.deepcopy(self.lines),
            "trains": copy.deepcopy(self.trains),
            "waiting": copy.deepcopy(self.waiting),
            "all_passengers": copy.deepcopy(self.all_passengers),
            "occupancy": set(self.occupancy),
            "delays": self.delays,
            "satisfaction": self.satisfaction,
            "delivered_count": self.delivered_count,
            "pax_seq": self._pax_seq,
            "train_seq": self._train_seq,
            "settled_clocks": set(self._settled_clocks),
            "rng_state": self.rng.getstate(),
        })

    def undo(self):
        """撤销最近一次状态变更，时钟/延误/满意度一并恢复。空栈无操作。"""
        if not self._undo_stack:
            return False
        snapshot = self._undo_stack.pop()
        self.clock = snapshot["clock"]
        self.stations = snapshot["stations"]
        self.lines = snapshot["lines"]
        self.trains = snapshot["trains"]
        self.waiting = snapshot["waiting"]
        self.all_passengers = snapshot["all_passengers"]
        self.occupancy = snapshot["occupancy"]
        self.delays = snapshot["delays"]
        self.satisfaction = snapshot["satisfaction"]
        self.delivered_count = snapshot["delivered_count"]
        self._pax_seq = snapshot["pax_seq"]
        self._train_seq = snapshot["train_seq"]
        self._settled_clocks = snapshot["settled_clocks"]
        self.rng.setstate(snapshot["rng_state"])
        return True

    # ---- 存档 ----

    def to_dict(self):
        version, state, gauss = self.rng.getstate()
        return {
            "version": SAVE_VERSION,
            "seed": self.seed,
            "clock": self.clock,
            "stations": [s.to_dict() for s in self.stations.values()],
            "lines": [line.to_dict() for line in self.lines.values()],
            "trains": [t.to_dict() for t in self.trains.values()],
            "waiting": {
                name: [p.pid for p in queue]
                for name, queue in self.waiting.items()
            },
            "passengers": [p.to_dict() for p in self.all_passengers.values()],
            "occupancy": [list(key) for key in sorted(self.occupancy)],
            "delays": self.delays,
            "satisfaction": self.satisfaction,
            "delivered_count": self.delivered_count,
            "pax_seq": self._pax_seq,
            "train_seq": self._train_seq,
            "settled_clocks": sorted(self._settled_clocks),
            "rng_state": [version, list(state), gauss],
        }

    def save(self, path):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)
        return path

    def load(self, path):
        """读档恢复全部状态与随机数，客流序列不偏移。"""
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(fh) if False else json.load(handle)
        self.seed = data["seed"]
        self.clock = data["clock"]
        self.lines = {
            line["name"]: Line.from_dict(line)
            for line in data["lines"]
        }
        self.stations = {
            s["name"]: Station.from_dict(s)
            for s in data["stations"]
        }
        self.trains = {
            t["tid"]: Train.from_dict(t) for t in data["trains"]
        }
        self.all_passengers = {
            p["pid"]: Passenger.from_dict(p) for p in data["passengers"]
        }
        self.waiting = {}
        for name, pids in data["waiting"].items():
            self.waiting[name] = [self.all_passengers[pid] for pid in pids]
        self.occupancy = {
            tuple(key) for key in data.get("occupancy", [])
        }
        self.delays = data.get("delays", 0)
        self.satisfaction = data.get("satisfaction", INITIAL_SATISFACTION)
        self.delivered_count = data.get("delivered_count", 0)
        self._pax_seq = data.get("pax_seq", 0)
        self._train_seq = data.get("train_seq", 0)
        self._settled_clocks = set(data.get("settled_clocks", []))
        version, state, gauss = data["rng_state"]
        self.rng = random.Random(self.seed)
        self.rng.setstate((version, tuple(state), gauss))
        self._seen_requests = set()
        self._undo_stack = []
        self.last_error = ""
        return self

    # ---- 文本命令（供测试与演示使用；重复编号幂等） ----

    def execute(self, line, request_id=None):
        parts = line.split()
        if not parts:
            return "ERR 未知命令"
        cmd = parts[0].upper()
        args = parts[1:]
        try:
            if cmd == "ADVANCE":
                minutes = int(args[0]) if args else 1
                rid = args[1] if len(args) > 1 else request_id
                if not self.advance(minutes=minutes, request_id=rid):
                    return "OK 重复请求已忽略"
                return "OK 时刻=%d" % self.clock
            if cmd == "PASSENGER" and len(args) >= 3:
                self._pax_seq += 1
                pid = args[0]
                passenger = self.add_passenger(pid, args[1], args[2])
                if passenger is None:
                    self._pax_seq -= 1
                    return "ERR %s" % self.last_error
                return "OK 乘客=%s" % pid
            if cmd == "TRAIN" and len(args) >= 3:
                tid = args[0]
                capacity = int(args[3]) if len(args) > 3 else 4
                direction = int(args[4]) if len(args) > 4 else 1
                if tid in self.trains:
                    return "ERR duplicate_train"
                self.add_train(tid, args[1], args[2],
                               capacity=capacity, direction=direction)
                return "OK 列车=%s" % tid
            if cmd == "DISPATCH" and len(args) == 1:
                if self.dispatch(args[0]):
                    return "OK 发车"
                return "ERR %s" % self.last_error
            if cmd == "CANCEL" and len(args) == 1:
                if self.cancel_train(args[0]):
                    return "OK 已取消"
                return "ERR 无法取消"
            if cmd == "FAULT" and len(args) >= 1:
                broken = not (len(args) > 1 and args[1] == "OFF")
                if self.set_breakdown(args[0], broken=broken):
                    return "OK 故障=%s" % ("降速" if broken else "修复")
                return "ERR %s" % self.last_error
            if cmd == "CLOSE" and len(args) >= 1:
                closed = not (len(args) > 1 and args[1] == "OPEN")
                if self.close_station(args[0], closed=closed):
                    return "OK %s" % ("封站" if closed else "解封")
                return "ERR %s" % self.last_error
            if cmd == "ROUTE" and len(args) == 2:
                route = self.find_route(args[0], args[1])
                return "OK %s" % ("-".join(route) if route else "不可达")
            if cmd == "UNDO":
                return "OK 已撤销" if self.undo() else "ERR 无可撤销"
            if cmd == "SAVE" and len(args) == 1:
                self.save(args[0])
                return "OK 已存档"
            if cmd == "LOAD" and len(args) == 1:
                self.load(args[0])
                return "OK 已读档"
            if cmd == "SETTLE":
                rid = args[0] if args else request_id
                self.settle(request_id=rid)
                return self.report()
            if cmd == "STATUS":
                return "OK %s" % self.status()
        except (ValueError, KeyError, OSError) as exc:
            return "ERR %s" % exc
        return "ERR 未知命令"

    def next_request_id(self):
        self._req_seq += 1
        return "r%d" % self._req_seq


DEMO_SCRIPT = [
    "PASSENGER p1 S1 S3",
    "PASSENGER p2 S2 S4",
    "PASSENGER p3 T2 S4",
    "DISPATCH t1",
    "DISPATCH t2",
    "ADVANCE 2",
    "TRAIN t3 2号线 T2 2",
    "DISPATCH t1",
    "DISPATCH t2",
    "DISPATCH t3",
    "ADVANCE 2",
    "CLOSE S4",
    "ROUTE S1 S5",
    "FAULT t1 OFF",
    "CLOSE S4 OPEN",
    "DISPATCH t1",
    "FAULT t1",
    "ADVANCE 2",
    "SETTLE",
    "STATUS",
]


def run_demo(seed=DEFAULT_SEED):
    """固定种子、无交互的演示。"""
    game = Game(seed=seed)
    print("seed=%d" % game.seed)
    for line in DEMO_SCRIPT:
        print("> %s -> %s" % (line, game.execute(line)))
    print("final: %s" % game.status())
    return game


MENU = """\
==== 地铁调度 ====
1. 推进时刻 (ADVANCE [分钟])
2. 新增乘客 (PASSENGER 编号 起点 终点)
3. 发车 (DISPATCH 列车)
4. 取消列车 (CANCEL 列车)
5. 设置/解除故障降速 (FAULT 列车 [OFF])
6. 临时封站/解封 (CLOSE 车站 [OPEN])
7. 查询路径 (ROUTE 起点 终点)
8. 结算本回合 (SETTLE)
9. 撤销上一步 (UNDO)
s. 存档 (SAVE 路径)
l. 读档 (LOAD 路径)
d. 固定种子演示
q. 退出
"""


def main(argv=None):
    """命令行菜单循环。"""
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--demo":
        run_demo()
        return 0
    game = Game()
    print(MENU)
    while True:
        try:
            line = input("metro> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("q", "quit", "exit"):
            break
        if line == "d":
            run_demo(game.seed)
            continue
        print(game.execute(line))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
