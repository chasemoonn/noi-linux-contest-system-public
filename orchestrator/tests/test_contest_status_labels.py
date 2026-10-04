"""Ready desktops are not necessarily inside the OJ contest time window."""
from datetime import datetime, timezone
import unittest
from unittest import mock

from tests.test_web_submit import main


class ContestStatusLabelTests(unittest.TestCase):
    def render_login(self, state, now_ms, begin=1000, end=2000):
        contest = {"title": "clock fixture", "state": state,
                   "begin_at_ms": begin, "end_at_ms": end}
        with mock.patch.object(main, "datetime") as clock:
            clock.now.return_value = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
            return main._web_login_response(
                {"submit_token": "fixture", "candidate": "CSP001"}, contest
            ).body.decode()

    def test_ready_login_follows_half_open_contest_window(self):
        for at_ms, label in [(999, "等待开赛"), (1000, "进行中"),
                             (1999, "进行中"), (2000, "已截止，等待收卷")]:
            with self.subTest(at_ms=at_ms):
                self.assertIn("状态：" + label, self.render_login("ready", at_ms))

    def test_rescheduled_deadline_changes_label_without_state_transition(self):
        self.assertIn("状态：已截止，等待收卷", self.render_login("ready", 2000))
        self.assertIn("状态：进行中", self.render_login("ready", 2000, end=3000))
        self.assertIn("状态：等待开赛", self.render_login("ready", 2000, begin=2500, end=3000))

    def test_ready_without_complete_time_snapshot_does_not_claim_running(self):
        for begin, end in [(0, 0), (1000, 0), (0, 2000), (2000, 1000)]:
            with self.subTest(begin=begin, end=end):
                self.assertIn("状态：已就绪，等待时间同步", self.render_login("ready", 1500, begin, end))

    def test_operational_state_is_not_replaced_by_wall_clock(self):
        for state, label in [("preparing", "正在备赛"), ("collecting", "正在收卷"),
                             ("safe_wait", "安全等待"), ("safe_ended", "已安全结束"),
                             ("error", "需要教师处理")]:
            with self.subTest(state=state):
                self.assertIn("状态：" + label, self.render_login(state, 1500))


if __name__ == "__main__":
    unittest.main()
