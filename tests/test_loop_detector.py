import unittest

from context.loop_detector import LoopDetection, LoopDetector


class LoopDetectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.detector = LoopDetector()

    def record_tool(self, tool_name: str, **args):
        self.detector.record_action("tool_call", tool_name=tool_name, args=args)

    def record_response(self, text: str):
        self.detector.record_action("response", text=text)

    def test_no_detection_below_threshold(self):
        self.record_tool("read_file", path="a.py")

        self.assertIsNone(self.detector.check_for_loop())

    def test_identical_tool_calls_detected(self):
        for _ in range(3):
            self.record_tool("read_file", path="a.py")

        detection = self.detector.check_for_loop()

        self.assertIsNotNone(detection)
        self.assertIn("times", detection.message)
        self.assertNotIn("tiems", detection.message)

    def test_offset_nudging_on_same_file_is_detected(self):
        for offset in (1, 2, 3):
            self.record_tool("read_file", path="a.py", offset=offset)

        self.assertIsNotNone(self.detector.check_for_loop())

    def test_meaningful_offset_progress_is_not_a_loop(self):
        for offset in (1, 400, 900):
            self.record_tool("read_file", path="a.py", offset=offset)

        self.assertIsNone(self.detector.check_for_loop())

    def test_different_paths_are_not_a_loop(self):
        for path in ("a.py", "b.py", "c.py"):
            self.record_tool("read_file", path=path)

        self.assertIsNone(self.detector.check_for_loop())

    def test_different_tools_are_not_a_loop(self):
        self.record_tool("read_file", path="a.py")
        self.record_tool("grep", path="a.py")
        self.record_tool("glob", path="a.py")

        self.assertIsNone(self.detector.check_for_loop())

    def test_string_arguments_normalized_for_whitespace_and_case(self):
        self.record_tool("shell", command="ls -la")
        self.record_tool("shell", command="LS   -la")
        self.record_tool("shell", command="ls -la\n")

        self.assertIsNotNone(self.detector.check_for_loop())

    def test_long_string_arguments_are_hashed_but_still_compared(self):
        content = "x " * 400
        for _ in range(3):
            self.record_tool("write_file", path="a.py", content=content)

        self.assertIsNotNone(self.detector.check_for_loop())

    def test_long_string_arguments_that_differ_are_not_a_loop(self):
        for suffix in ("alpha", "beta", "gamma"):
            self.record_tool("write_file", path="a.py", content=("x " * 400) + suffix)

        self.assertIsNone(self.detector.check_for_loop())

    def test_near_identical_responses_detected(self):
        self.record_response("I will now look at the configuration file again.")
        self.record_response("I will now look at the configuration file, again!")
        self.record_response("I will now look at the  configuration file again")

        detection = self.detector.check_for_loop()

        self.assertIsNotNone(detection)

    def test_distinct_responses_not_detected(self):
        self.record_response("Reading the configuration file now.")
        self.record_response("The tests all pass, so I am done here.")
        self.record_response("Let me check whether the migration script exists.")

        self.assertIsNone(self.detector.check_for_loop())

    def test_repeating_cycle_detected(self):
        for _ in range(3):
            self.record_tool("read_file", path="a.py")
            self.record_tool("edit", path="a.py", old_string="x", new_string="y")

        self.assertIsNotNone(self.detector.check_for_loop())

    def test_cycle_detection_tolerates_offset_jitter(self):
        for offset in (1, 2, 3):
            self.record_tool("read_file", path="a.py", offset=offset)
            self.record_tool("grep", path="a.py", pattern="foo")

        self.assertIsNotNone(self.detector.check_for_loop())

    def test_detection_result_is_string_compatible(self):
        for _ in range(3):
            self.record_tool("read_file", path="a.py")

        detection = self.detector.check_for_loop()

        self.assertIsInstance(detection, LoopDetection)
        self.assertEqual(f"{detection}", detection.message)

    def test_escalation_counts_and_aborts(self):
        for _ in range(3):
            self.record_tool("read_file", path="a.py")

        first = self.detector.check_for_loop()
        self.assertEqual(first.count, 1)
        self.assertEqual(first.severity, "warning")
        self.assertFalse(self.detector.should_abort())

        second = self.detector.check_for_loop()
        self.assertEqual(second.count, 2)
        self.assertFalse(self.detector.should_abort())

        third = self.detector.check_for_loop()
        self.assertEqual(third.count, 3)
        self.assertEqual(third.severity, "critical")
        self.assertTrue(third.is_critical)
        self.assertTrue(self.detector.should_abort())

    def test_clear_resets_history_and_escalation(self):
        for _ in range(3):
            self.record_tool("read_file", path="a.py")
        for _ in range(3):
            self.detector.check_for_loop()

        self.detector.clear()

        self.assertFalse(self.detector.should_abort())
        self.assertEqual(self.detector.detection_count, 0)
        self.assertIsNone(self.detector.check_for_loop())

    def test_non_dict_args_do_not_raise(self):
        self.detector.record_action("tool_call", tool_name="read_file", args="oops")

        self.assertIsNone(self.detector.check_for_loop())


if __name__ == "__main__":
    unittest.main()
