"""Run the shipped release script with fake Docker/HTTP, never a live daemon."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class AdminReleaseTests(unittest.TestCase):
    def run_release(self, failure=""):
        git_bash = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(git_bash) if os.name == "nt" and git_bash.is_file() else shutil.which("bash")
        self.assertIsNotNone(bash, "Bash is required for release script checks")
        with tempfile.TemporaryDirectory(prefix="vp-admin-release-") as tmp:
            root = Path(tmp)
            stubs = {
                "docker": """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$1" in
  build) [ "$FAKE_FAILURE" != build ] || exit 2 ;;
  inspect) printf '%s\n' sha256:previous ;;
  run)
    image=${!#}
    if [ "$image" = video-parser-admin:candidate ] && [ "$FAKE_FAILURE" = run ]; then exit 3; fi
    if [ "$image" = sha256:previous ] && [ "$FAKE_FAILURE" = rollback ]; then exit 4; fi
    printf '%s\n' "$image" > "$FAKE_STATE"
    printf '%s\n' container-id ;;
esac
""",
                "curl": """#!/usr/bin/env bash
image=$(cat "$FAKE_STATE" 2>/dev/null || true)
if [ "$image" = video-parser-admin:candidate ] && [[ "$FAKE_FAILURE" = health || "$FAKE_FAILURE" = rollback ]]; then exit 22; fi
url=${!#}
case "$url" in
  */health) [[ " $* " != *" -w "* ]] || printf 200 ;;
  */login) printf 200 ;;
  *) if [[ "$FAKE_FAILURE" = boundary && "$image" = video-parser-admin:candidate ]]; then printf 200; else printf 302; fi ;;
esac
""",
                "sleep": "#!/usr/bin/env bash\nexit 0\n",
            }
            for name, content in stubs.items():
                path = root / name
                path.write_text(content, encoding="utf-8", newline="\n")
                path.chmod(0o755)
            log, state = root / "commands", root / "image"
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ.get("PATH", ""),
                       FAKE_LOG=str(log), FAKE_STATE=str(state), FAKE_FAILURE=failure, FAKE_BIN=str(root))
            script = Path(__file__).resolve().parent / "ops" / "deploy-admin-container.sh"
            env["FAKE_SCRIPT"] = str(script)
            # Git Bash prepends its own tools when starting on Windows. Put the
            # stubs first inside Bash, so no real curl or Docker can be called.
            launch = 'if command -v cygpath >/dev/null; then FAKE_BIN=$(cygpath -u "$FAKE_BIN"); fi; export PATH="$FAKE_BIN:$PATH"; exec bash "$FAKE_SCRIPT"'
            result = subprocess.run([bash, "-c", launch], env=env, text=True, capture_output=True, timeout=20)
            return result, log.read_text() if log.exists() else "", state.read_text().strip() if state.exists() else ""

    def test_success_promotes_candidate_only_after_health_and_access_checks(self):
        result, log, image = self.run_release()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(image, "video-parser-admin:candidate")
        self.assertIn("tag video-parser-admin:candidate video-parser-admin:latest", log)

    def test_build_failure_keeps_original_container_running(self):
        result, log, _ = self.run_release("build")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("rm -f", log)
        self.assertNotIn("run -d", log)

    def test_run_health_and_access_failure_restore_exact_previous_image(self):
        for failure in ("run", "health", "boundary"):
            with self.subTest(failure=failure):
                result, log, image = self.run_release(failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(image, "sha256:previous")
                self.assertIn("Previous admin image restored and healthy", result.stderr)
                self.assertNotIn("tag video-parser-admin:candidate", log)

    def test_failed_restore_is_reported_as_failure(self):
        result, _, _ = self.run_release("rollback")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Admin rollback failed", result.stderr)


if __name__ == "__main__":
    unittest.main()
