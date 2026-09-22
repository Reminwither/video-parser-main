import browser_cookie3
import json
import pathlib
import re
import shutil


PROJECT = pathlib.Path(r"E:\视频\video-parser-main")
PROFILE = pathlib.Path(r"C:\Users\Administrator\AppData\Roaming\Codex\web\Codex")
COOKIE_DB = PROFILE / "Default" / "Partitions" / "codex-browser-app" / "Network" / "Cookies"
LOCAL_STATE = PROFILE / "Local State"
ENV_PATH = PROJECT / ".env"
BACKUP_PATH = PROJECT / ".env.before_codex_cookie"
STATUS_PATH = PROJECT / ".codex_cookie_extract_status.json"


def write_status(state, message, names=()):
    STATUS_PATH.write_text(
        json.dumps(
            {
                "state": state,
                "message": message,
                "cookie_names": list(names),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


try:
    if not COOKIE_DB.exists():
        raise RuntimeError(f"Cookie database not found: {COOKIE_DB}")

    jar = browser_cookie3.chrome(
        cookie_file=str(COOKIE_DB),
        key_file=str(LOCAL_STATE),
        domain_name="douyin.com",
    )

    cookies = {}
    for item in jar:
        if item.name and item.value and item.name not in cookies:
            cookies[item.name] = item.value

    required = ("UIFID", "ttwid")
    missing = [name for name in required if name not in cookies]
    if missing:
        raise RuntimeError("Required cookies missing: " + ", ".join(missing))

    cookie_header = "; ".join(f"{name}={value}" for name, value in cookies.items())
    env_text = ENV_PATH.read_text(encoding="utf-8")
    if not BACKUP_PATH.exists():
        shutil.copy2(ENV_PATH, BACKUP_PATH)

    replacement = lambda match: "DOUYIN_COOKIE=" + cookie_header
    if re.search(r"(?m)^DOUYIN_COOKIE=.*$", env_text):
        env_text = re.sub(r"(?m)^DOUYIN_COOKIE=.*$", replacement, env_text, count=1)
    else:
        env_text = env_text.rstrip("\r\n") + "\nDOUYIN_COOKIE=" + cookie_header + "\n"
    ENV_PATH.write_text(env_text, encoding="utf-8")
    write_status("complete", "Douyin cookies were written to .env.", cookies.keys())
    print(json.dumps({"state": "complete", "cookie_count": len(cookies), "cookie_names": list(cookies)}, ensure_ascii=False))
except Exception as exc:
    write_status("failed", str(exc))
    print(json.dumps({"state": "failed", "error": str(exc)}, ensure_ascii=False))
    raise
