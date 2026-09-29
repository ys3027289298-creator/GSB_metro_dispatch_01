# -*- coding: utf-8 -*-
"""metro_game.core 的回归测试：覆盖八个深层 bug 与核心不变量。"""
import json
import os
import tempfile
import unittest

from metro_game.core import (
    BASE_TRAVEL_MINUTES, BREAKDOWN_SPEED_FACTOR, Game, INITIAL_SATISFACTION,
    TRAIN_DOCKED, TRAIN_MOVING,
)


def quiet_rng(game):
    """让随机客流事件不触发，保证断言不依赖固定种子的具体序列。"""
    game.rng.random = lambda: 1.0
    return game


def assert_invariants(test_case, game):
    """信号、容量、占用键一致性必须始终成立。"""
    # 信号区间：每个键至多被一列移动中的列车占用。
    seen = {}
    for train in game.trains.values():
        if train.is_moving:
            key = game._section_key(
                train.line_name, train.from_idx, train.to_idx)
            test_case.assertNotIn(key, seen)
            seen[key] = train.tid
            test_case.assertIn(key, game.occupancy)
    # occupancy 集合与移动列车完全对应；列车绝不超载。
    test_case.assertEqual(set(seen), set(game.occupancy))
    for train in game.trains.values():
        test_case.assertLessEqual(train.load, train.capacity)


class MetroInvariantTests(unittest.TestCase):

    def test_01_same_time_transfer_is_served(self):
        """同一时刻到达换乘客流不被跳过。"""
        game = quiet_rng(Game())
        # a 从 S4 开向换乘站 S3；b 从 T3 开向 S3，随后继续环线。
        game.add_train("a", "1号线", "S4", capacity=3, direction=-1)
        game.add_train("b", "2号线", "T3", capacity=3, direction=1)
        pax = game.add_passenger("p1", "S4", "T1")
        game.dispatch("a")                       # a: S4->S3, eta=2
        self.assertEqual(pax.on_train, "a")
        game.advance(2)                          # tick2: a 到达 S3，p1 换乘
        self.assertEqual(pax.origin, "S3")
        self.assertIsNone(pax.on_train)
        self.assertIsNone(pax.served_time)
        self.assertIn(pax.pid,
                      [p.pid for p in game.waiting.get("S3", [])])
        game.dispatch("b")                       # b: T3->S3, eta=4
        game.advance(2)                          # tick4: b 同刻到 S3
        self.assertIsNone(pax.served_time)       # b 下一程才载客
        game.dispatch("b")                       # b: S3->T1，p1 同刻登车
        self.assertEqual(pax.on_train, "b")
        game.advance(2)                          # tick6: 送达 T1
        self.assertEqual(pax.served_time, 6)
        self.assertTrue(pax.delivered)
        assert_invariants(self, game)

    def test_02_cancel_releases_signal_section(self):
        """取消列车立即释放信号区间，随后另一列车可进入同一区间。"""
        game = quiet_rng(Game())
        game.add_train("x", "1号线", "S3", capacity=2, direction=-1)
        self.assertTrue(game.dispatch("t1"))  # t1: S1->S2
        self.assertTrue(game.dispatch("x"))   # x:  S3->S2（不同区间）
        self.assertEqual(len(game.occupancy), 2)
        # t1 仍占用 S1<->S2 时，反向列车无法进入同一区间。
        game.add_train("y", "1号线", "S2", capacity=2, direction=-1)
        self.assertFalse(game.dispatch("y"))
        self.assertEqual(game.last_error, "signal_busy")
        # 取消 t1 后区间立即释放。
        self.assertTrue(game.cancel_train("t1"))
        self.assertNotIn("t1", game.trains)
        self.assertEqual(game.occupancy, {
            game._section_key("1号线", 1, 2)})
        # 现在 y 可进入 S2->S1（x 仍占用 S2<->S3）。
        self.assertTrue(game.dispatch("y"))
        self.assertEqual(len(game.occupancy), 2)
        assert_invariants(self, game)

    def test_03_loop_route_counts_station_once(self):
        """环线行程不把末站重复计入；直线不产生回卷虚边。"""
        game = Game()
        route = game.find_route("S3", "S3")
        self.assertEqual(route, ["S3"])
        route = game.find_route("T3", "T2")
        self.assertEqual(route, ["T3", "T2"])
        self.assertEqual(len(route), len(set(route)))
        route = game.find_route("S1", "S6")
        self.assertEqual(route, ["S1", "S2", "S3", "S4", "S5", "S6"])
        # 直线两端相邻必须是单向的，不允许 S6 直接回到 S1。
        self.assertNotIn("S1", game.neighbors("S6"))
        assert_invariants(self, game)

    def test_04_full_train_truncates_boarding(self):
        """满载列车不接受超出容量的乘客。"""
        game = quiet_rng(Game())
        for i in range(5):
            game.add_passenger("p%d" % i, "S1", "S5")
        self.assertTrue(game.dispatch("t1"))  # capacity=3
        train = game.trains["t1"]
        self.assertEqual(train.load, 3)
        self.assertLessEqual(train.load, train.capacity)
        waiting = [p.pid for p in game.waiting["S1"]]
        self.assertEqual(len(waiting), 2)
        game.advance(1)
        for other in game.trains.values():
            self.assertLessEqual(other.load, other.capacity)
        assert_invariants(self, game)

    def test_05_breakdown_extends_eta_and_delays(self):
        """区间内故障立即延长到站时间，并据实累计延误分钟。"""
        game = quiet_rng(Game())
        game.add_passenger("p1", "S1", "S3")
        game.dispatch("t1")
        train = game.trains["t1"]
        eta_before = train.eta
        self.assertTrue(game.set_breakdown("t1", True))
        self.assertEqual(
            train.eta - eta_before,
            BASE_TRAVEL_MINUTES * (BREAKDOWN_SPEED_FACTOR - 1))
        game.advance(BASE_TRAVEL_MINUTES)
        self.assertTrue(train.is_moving)  # 尚未到站
        game.advance(BASE_TRAVEL_MINUTES)
        self.assertEqual(train.status, TRAIN_DOCKED)
        self.assertEqual(
            game.delays,
            BASE_TRAVEL_MINUTES * (BREAKDOWN_SPEED_FACTOR - 1))
        assert_invariants(self, game)

    def test_06_closed_station_blocked_in_routing(self):
        """临时封站同时影响显示与寻路。"""
        game = quiet_rng(Game())
        self.assertTrue(game.close_station("S4"))
        self.assertTrue(game.is_closed("S4"))
        self.assertIsNone(game.find_route("S1", "S5"))
        self.assertNotIn("S4", game.neighbors("S3"))
        self.assertIsNone(game.find_route("S4", "S1"))
        game.dispatch("t1")  # t1: S1->S2 成功
        self.assertTrue(game.trains["t1"].is_moving)
        self.assertFalse(game.dispatch("t1"))
        self.assertEqual(game.last_error, "not_docked")
        game.advance(2)  # t1 到 S2，下一步 S3 之后是封闭的 S4，不影响到达
        self.assertTrue(game.reopen_station("S4"))
        self.assertIsNotNone(game.find_route("S1", "S5"))
        assert_invariants(self, game)

    def test_07_duplicate_requests_settle_once(self):
        """同一回合重复推进/结算只生效一次。"""
        game = quiet_rng(Game())
        game.add_passenger("p1", "S1", "S5")
        self.assertTrue(game.advance(1, request_id="req-1"))
        self.assertFalse(game.advance(1, request_id="req-1"))
        self.assertEqual(game.clock, 1)
        self.assertTrue(game.settle(request_id="req-2"))
        satisfaction_after_first = game.satisfaction
        self.assertTrue(game.settle(request_id="req-2"))
        self.assertTrue(game.settle())  # 同回合无编号重复也只结算一次
        self.assertEqual(game.satisfaction, satisfaction_after_first)
        self.assertLess(satisfaction_after_first, INITIAL_SATISFACTION)
        # 新回合结算才再次生效。
        game.advance(1)
        self.assertTrue(game.settle())
        self.assertLess(game.satisfaction, satisfaction_after_first)
        assert_invariants(self, game)

    def test_08_undo_restores_delay_and_satisfaction(self):
        """回滚恢复时钟、延误分钟数与满意度。"""
        game = quiet_rng(Game())
        game.dispatch("t1")
        game.set_breakdown("t1", True)
        game.advance(4)
        self.assertGreater(game.delays, 0)
        broken_satisfaction = game.satisfaction
        self.assertLess(broken_satisfaction, INITIAL_SATISFACTION)
        game.add_passenger("p9", "S1", "S6")
        before_settle = game.satisfaction
        game.settle()
        self.assertLess(game.satisfaction, before_settle)
        self.assertTrue(game.undo())  # 撤销结算
        self.assertEqual(game.satisfaction, before_settle)
        self.assertTrue(game.undo())  # 撤销新增乘客
        self.assertNotIn("p9", game.all_passengers)
        self.assertTrue(game.undo())  # 撤销推进
        self.assertEqual(game.clock, 0)
        self.assertEqual(game.delays, 0)
        self.assertEqual(game.satisfaction, INITIAL_SATISFACTION)
        assert_invariants(self, game)

    def test_09_save_load_roundtrip_keeps_state(self):
        """JSON 存档/读档后状态、占用、满意度与随机序列保持一致。"""
        game = quiet_rng(Game())
        game.add_passenger("p1", "S1", "S4")
        game.dispatch("t1")
        game.advance(2)
        game.close_station("S5")
        game.settle()
        payload = json.loads(json.dumps(game.to_dict()))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "save.json")
            game.save(path)
            restored = Game(seed=999)
            restored.load(path)
        self.assertEqual(restored.clock, game.clock)
        self.assertEqual(restored.delays, game.delays)
        self.assertEqual(restored.satisfaction, game.satisfaction)
        self.assertEqual(restored.delivered_count, game.delivered_count)
        self.assertEqual(restored.occupancy, game.occupancy)
        self.assertEqual(
            {n: [p.pid for p in q] for n, q in restored.waiting.items()},
            {n: [p.pid for p in q] for n, q in game.waiting.items()})
        self.assertEqual(restored.rng.getstate()[1:], game.rng.getstate()[1:])
        self.assertEqual(restored.to_dict()["version"], payload["version"])
        assert_invariants(self, restored)

    def test_10_demo_is_deterministic_and_invariant(self):
        """固定种子演示可重复，运行期间不变量成立。"""
        from io import StringIO
        from contextlib import redirect_stdout

        from metro_game.core import run_demo
        buf1, buf2 = StringIO(), StringIO()
        with redirect_stdout(buf1):
            game1 = run_demo()
        with redirect_stdout(buf2):
            game2 = run_demo()
        self.assertEqual(buf1.getvalue(), buf2.getvalue())
        self.assertEqual(game1.to_dict(), game2.to_dict())
        assert_invariants(self, game1)


if __name__ == "__main__":
    unittest.main()
