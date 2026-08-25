from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = (
    ROOT
    / "noi-linux-official"
    / "rootfs"
    / "usr"
    / "local"
    / "bin"
    / "contest-entrypoint.sh"
)
DESKTOP_CONFIG = (
    ROOT
    / "noi-linux-official"
    / "rootfs"
    / "usr"
    / "local"
    / "bin"
    / "configure-contest-desktop.sh"
)


class StudentDesktopContractTests(unittest.TestCase):
    def setUp(self):
        self.entrypoint = ENTRYPOINT.read_text(encoding="utf-8")

    def test_each_problem_gets_one_non_overwriting_canonical_cpp(self):
        for marker in (
            'source_target="${ANSWER_DIR}/${problem}/${problem}.cpp"',
            '[[ ! -e "${source_target}" && ! -L "${source_target}" ]]',
            '/dev/null "${source_target}"',
            '"${ANSWER_DIR}/${problem}/${problem}.cpp"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.entrypoint)
        self.assertNotIn('> "${source_target}"', self.entrypoint)

    def test_start_answer_launcher_opens_canonical_files_in_geany(self):
        for marker in (
            "03_开始答题.desktop",
            "03_开始答题.sh",
            'exec geany --new-instance "${files[@]}"',
            "Name=03_开始答题",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.entrypoint)
        desktop_config = DESKTOP_CONFIG.read_text(encoding="utf-8")
        self.assertIn("03_开始答题.desktop", desktop_config)

    def test_geany_execute_and_explicit_input_switch_are_configured(self):
        for marker in (
            'EX_00_CM="./%e"',
            "ibus-daemon --daemonize --xim --panel disable",
            'main-switch ""',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.entrypoint)
        self.assertNotIn("ibus-daemon --daemonize --replace", self.entrypoint)

    def test_official_home_cache_is_student_writable_before_gsettings(self):
        cache = self.entrypoint.index(
            'ensure_real_directory "${HOME_DIR}/.cache"'
        )
        dconf = self.entrypoint.index(
            'ensure_real_directory "${HOME_DIR}/.cache/dconf"'
        )
        input_setting = self.entrypoint.index(
            'gsettings set com.github.libpinyin.ibus-libpinyin.libpinyin'
        )
        self.assertLess(cache, input_setting)
        self.assertLess(dconf, input_setting)
        self.assertIn(
            'chown "${USER_NAME}:${USER_NAME}" "${HOME_DIR}"',
            self.entrypoint,
        )

    def test_student_instructions_state_web_first_per_problem_fallback(self):
        for marker in (
            "北京赛制优先使用网页递交",
            "同一道题一旦网页递交",
            "整场没有网页递交",
            "最后一次网页递交为准",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.entrypoint)


if __name__ == "__main__":
    unittest.main()
