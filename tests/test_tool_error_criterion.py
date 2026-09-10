"""Tests for the tool_error criterion: does a result *report* a failure?

The old criterion was "the lowercased content contains error / failed /
exception / errno / traceback anywhere". Measured on one real transcript it fired
on 291 of 291 candidate results, of which 276 held no failure at all — every
source file and document that merely discusses errors qualified. Those 19k
characters of source then crowded every real signal out of the prompt budget.

Both directions are asserted here from cases found by replaying real transcripts,
because the failure mode is not "the regex is wrong" but "the regex is right
about the wrong thing".
"""
from __future__ import annotations

import pytest

from server import auto_memory as am


def _hits(text: str) -> bool:
    return bool(am._TOOL_ERROR_RE.search(am._plain(text)))


# ── must catch: a failure is being reported ───────────────────────────────────

@pytest.mark.parametrize("text", [
    "Traceback (most recent call last)\n  File \"x.py\", line 1",
    "ValueError: invalid literal for int()",
    "KeyError",
    "error: unknown option `--foo`",
    "FAILED tests/test_x.py::test_y",
    "bash: cco: command not found",
    "cat: /nope: No such file or directory",
    "sh: ./x: Permission denied",
    "process exited with code 2",
    "Exit code 1",
    "Exit code 2\nls: cannot access '/nope'",
    # Coloured output: pytest puts escapes *inside* the phrase, so the anchors
    # only work if ANSI is stripped first.
    "\x1b[31mFAILED\x1b[0m tests/x.py - AssertionError: assert 0 == 3",
    "\x1b[1;31mAssertionError: boom",
])
def test_real_failures_are_caught(text):
    assert _hits(text)


# ── must not catch: the word appears, nothing failed ──────────────────────────

@pytest.mark.parametrize("text", [
    # Each of these was a real false positive in the measured transcript.
    "60\t│           ├── errors.md    # 错误经验",       # docs/codebase.md line 60
    "data/knowledge/867aac932032/errors.md",              # an ls listing
    "57\t            except json.JSONDecodeError:",       # server/memory.py
    "        errors: list[str] | None = None,",           # adapters/base.py
    "  saveError(payload)",                              # an identifier
    'const onError = () => setFailed(true)',
    "- 25 个阶段全 SUCCESS，0 个 Traceback/Error/FAIL",   # a report of no errors
    "exit 0",                                            # success
    "process exited with code 0",
    # Shell *source* read by the Read tool, not a result. This is why the exit
    # branch is capitalized: `exit 1` appears in nearly every script in the repo,
    # and matching it made reading one look like running a failing one. Measured:
    # every hit the loose lowercase pattern found and the capitalized one does
    # not was Read output of a *.sh file.
    "  if [[ -z \"$1\" ]]; then\n    usage\n    exit 1\n  fi",
    "  34\t    exit 1",
    "# 用法: ./run_e2e.sh <case>\n#   失败时 exit 1",
    "# Error handling\n\nThis module documents failure modes.",
])
def test_mentions_of_failure_are_not_caught(text):
    assert not _hits(text)


def test_exit_zero_is_not_a_failure_but_exit_nonzero_is():
    """`[1-9]` rather than `\\d`: a clean exit must not read as a failure."""
    assert not _hits("Exit code 0")
    assert _hits("Exit code 9")
    assert not _hits("exited with code 0")
    assert _hits("exited with code 9")


def test_an_identifier_ending_in_error_needs_a_colon_or_line_end():
    """The load-bearing half of the pattern. Without `(?::|\\s*$)` every
    `fooError(` and `errors:` in source matched, which is where the 276 false
    positives came from."""
    assert _hits("RuntimeError: x")
    assert _hits("RuntimeError")
    assert not _hits("  myRuntimeErrorHandler(x)")


# ── the window is centred on the match ────────────────────────────────────────

def test_the_signal_contains_the_error_it_matched():
    """The old code took content[:800] from the head, so 99 of 289 signals (34%)
    reached the prompt without the error in them — the match sat at character
    2682 of a file listing and the model saw an unremarkable chunk of source."""
    text = "filler line\n" * 400 + "ValueError: the actual problem\n" + "tail\n" * 100
    pos = text.index("ValueError")
    assert pos > am._ERROR_WINDOW, "the error must start beyond a head slice"
    window = am._around(text, pos)
    assert "ValueError: the actual problem" in window
    assert len(window) <= am._ERROR_WINDOW


def test_short_content_is_returned_whole():
    assert am._around("KeyError: x", 0) == "KeyError: x"


def test_the_window_keeps_context_before_the_error():
    text = "x" * 2000 + "ValueError: y" + "z" * 2000
    window = am._around(text, 2000)
    assert window.startswith("x"), "some preceding context must survive"
    assert "ValueError: y" in window


def test_extraction_uses_the_criterion_and_not_a_substring_scan():
    """Guards the call site, not just the pattern. Every assertion above goes
    through `_TOOL_ERROR_RE` directly, so a regression that swapped the criterion
    back to a substring scan *inside* the extractor would leave them all green —
    that mutant survived a first run of this file.
    """
    class M:
        def __init__(self, content):
            self.role, self.type, self.content = "agent", "tool_result", content

    class T:
        id, status, num_turns = "t1", "failed", 1
        def __init__(self, msgs):
            self.messages = msgs

    noise = [
        M("60\t│           ├── errors.md    # 错误经验"),
        M("57\t            except json.JSONDecodeError:"),
        M("        errors: list[str] | None = None,"),
    ]
    assert am.extract_lesson_signals([T(noise)]) == [], "mentions must not extract"

    real = M("ValueError: the actual problem")
    signals = am.extract_lesson_signals([T(noise + [real])])
    assert len(signals) == 1, "only the real failure may become a signal"


def test_extraction_emits_signals_that_show_their_error():
    """End to end over the extractor, not just the helper: every tool_error
    signal must still match the criterion that selected it."""
    class M:
        role, type = "agent", "tool_result"
        content = "noise\n" * 300 + "\x1b[31mAssertionError: real\x1b[0m\n"

    class T:
        id, status, num_turns = "t1", "failed", 1
        messages = [M()]

    signals = am.extract_lesson_signals([T()])
    tool = [s for s in signals if s["source"] == "tool_error"]
    assert len(tool) == 1
    assert am._TOOL_ERROR_RE.search(tool[0]["content"]), "signal lost its error"
    assert "\x1b[" not in tool[0]["content"], "escapes must not reach the prompt"


# ── recovered failures ────────────────────────────────────────────────────────
#
# Reported by review: the whole-task gate discarded tool_error from a run that
# hit a real failing call, recovered, and finished inside 20 turns — which is
# where the reusable lesson usually is. Measured on 1787 real tasks: 326 such
# signals were being dropped, against 3614 kept, so this admits +9%.
#
# It only became safe once the Exit code branch stopped matching shell source:
# before that, every `Read` of a *.sh file from a successful task would qualify.

class _Msg:
    def __init__(self, role, mtype, content):
        self.role, self.type, self.content = role, mtype, content


class _Task:
    def __init__(self, status, turns, msgs):
        self.id, self.name = "t1", "t1"
        self.status, self.num_turns, self.messages = status, turns, msgs


def _sources(task) -> list[str]:
    return [s["source"] for s in am.extract_lesson_signals([task])]


def test_a_recovered_tool_error_in_a_successful_task_is_kept():
    task = _Task("success", 8, [
        _Msg("agent", "tool_result",
             "Exit code 1\n ! [rejected] main -> main (non-fast-forward)"),
        _Msg("agent", "text", "改用 pull --rebase 后推送成功"),
    ])
    assert _sources(task) == ["tool_error"]


def test_a_clean_successful_task_still_contributes_nothing():
    task = _Task("success", 3, [
        _Msg("agent", "tool_result", "42 files changed"),
        _Msg("agent", "text", "完成"),
    ])
    assert _sources(task) == []


def test_reading_a_shell_script_is_not_a_recovered_failure():
    """The regression this pairing could have introduced: `exit 1` in source."""
    task = _Task("success", 2, [
        _Msg("agent", "tool_result",
             "  32\t  if [[ -z \"$1\" ]]; then\n  33\t    exit 1\n  34\t  fi"),
    ])
    assert _sources(task) == []


def test_a_failed_task_still_contributes_everything():
    """The looser branch must not shadow the original one: a failed run keeps
    its user_correction signals too."""
    task = _Task("failed", 3, [
        _Msg("agent", "tool_result", "ValueError: boom"),
        _Msg("user", "text", "不对，应该用另一个参数"),
    ])
    assert sorted(_sources(task)) == ["tool_error", "user_correction"]


def test_a_recovered_error_is_not_flagged_as_a_failed_task():
    """The label rides into the prompt, so a recovered run must not be presented
    to the model as one that failed."""
    task = _Task("success", 5, [_Msg("agent", "tool_result", "Exit code 2\nboom")])
    sig = am.extract_lesson_signals([task])[0]
    assert "task_failed" not in sig and "high_turns" not in sig
