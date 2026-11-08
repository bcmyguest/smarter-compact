"""CONTEXT_EVICTION's superseded-command rule only covers read-only commands (evict.read_only)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context_proxy import Base  # noqa: E402
import test_eviction as te  # noqa: E402

from smarter_compact.evict import read_only  # noqa: E402

READ_ONLY = [
    "cat /app/a.py", "head -n 20 log.txt", "tail -f out.log", "wc -l src/*.py", "ls -la", "tree -L 2",
    "stat a b", "file bin/x", "find . -name '*.py'", "grep -rn alpha src", "rg TODO", "ag needle",
    "git status", "git diff HEAD~1", "git -C repo log --oneline -5", "git show HEAD", "git branch -a",
    "git --no-pager diff", "pwd", "env", "which python3", "sed -n '1,20p' a.py", "sed -n -e 5p a.py",
    "sed -n '/^def /p' a.py", "grep -c x a | head -1", "ls && pwd", "cat a; cat b", "ls x || ls y",
    "  git   status  ",
]
NOT_READ_ONLY = [
    "mario step RIGHT_RUN --frames 20", "make test", "python3 play.py", "npm test", "./game move left",
    "cat a > b", "grep x a >> log", "ls 2>&1", "cat a | tee b", "sed -i 's/a/b/' f", "sed -ni '1p' f",
    "sed --in-place=.bak -n 1p f", "sed 's/a/b/' f", "sed -n '1,5p;w out' f", "find . -delete",
    "find . -name x -exec rm {} ;", "git commit -m x", "git branch new-feature", "git branch -D old",
    "git diff --output=patch.txt", "git", "env FOO=1 make", "cat $(ls)", "cat `ls`", "ls & make",
    "cd /app && ls", "(ls)", "ls | xargs rm", "ls && make", "", "cat 'unterminated", "cat < a",
]


class ReadOnly(unittest.TestCase):
    def test_allowlisted_commands_and_chains_of_them_are_read_only(self):
        for command in READ_ONLY:
            with self.subTest(command=command):
                self.assertTrue(read_only(command))

    def test_stateful_unknown_redirected_or_in_place_commands_are_not(self):
        for command in NOT_READ_ONLY:
            with self.subTest(command=command):
                self.assertFalse(read_only(command))


class StatefulCommandEviction(Base):
    # Reuse the eviction helpers without inheriting (and re-running) test_eviction's tests.
    send = te.EvictionTests.send
    evicted = staticmethod(te.EvictionTests.evicted)

    def test_a_repeated_stateful_command_keeps_every_result(self):
        steps = [te.bash("mario step RIGHT_RUN --frames 20", "x=40 world=1-1"), *te.distinct(3),
                 te.bash("mario step RIGHT_RUN --frames 20", "x=80 world=1-1"), *te.distinct(1, 3)]
        self.assertEqual(self.evicted(self.send(steps, eviction_keep=100)), [])

    def test_a_repeated_read_only_command_still_goes(self):
        steps = [te.bash("git status", "modified: a.py"), *te.distinct(3), te.bash("git  status", "clean"), *te.distinct(1, 3)]
        self.assertEqual(self.evicted(self.send(steps, eviction_keep=100)), [0])

    def test_a_stateful_command_between_two_reads_does_not_change_the_rule(self):
        steps = [te.bash("grep score state.json", "score 0010"), te.bash("mario step A", "ok"), *te.distinct(2),
                 te.bash("grep score state.json", "score 0020"), *te.distinct(1, 3)]
        # Step 4 repeats step 0's grep, so step 0 goes; the stateful step 1 stays.
        self.assertEqual(self.evicted(self.send(steps, eviction_keep=100)), [0])

if __name__ == "__main__":
    unittest.main()
