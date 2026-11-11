#!/usr/bin/env bash
set -euo pipefail
if (( $# != 1 )); then echo 'Usage: create_swe_eval_toy.sh NEW_TASK_DIR' >&2; exit 2; fi
root=$1
[[ ! -e "$root" ]] || { echo 'Task directory already exists' >&2; exit 1; }
mkdir -p "$root/repo"
printf '%s\n' 'def add(a, b):' '    return a - b' > "$root/repo/calc.py"
cat > "$root/repo/test_calc.py" <<'PY'
import unittest
from calc import add
class TestAdd(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(add(-2, 3), 1)
if __name__ == '__main__':
    unittest.main()
PY
printf '%s\n' 'Fix calc.add(a, b) so that it returns the sum of its arguments.' > "$root/problem.md"
git -C "$root/repo" init -q
git -C "$root/repo" add calc.py test_calc.py
git -C "$root/repo" -c user.name='SWE eval fixture' -c user.email='swe-eval@example.invalid' commit -qm 'Create reproducible buggy task'
git -C "$root/repo" rev-parse HEAD | tee "$root/commit.txt"
