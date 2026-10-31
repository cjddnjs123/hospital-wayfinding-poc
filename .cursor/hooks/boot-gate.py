#!/usr/bin/env python3
"""현재 gpt main의 core/boot.md를 읽기 전에는 파일 수정과 셸을 거절한다.

정본: cjddnjs123/gpt templates/boot_gate/boot_gate.py
제품 레포의 .cursor/hooks/boot-gate.py는 scripts/sync_boot_gate.py가 만든 사본이다.
사본을 직접 고치지 말고 정본을 고친 뒤 다시 배포한다.

클라우드 에이전트(Cursor·Claude·Codex)는 PC 전역 훅을 실행하지 않고 레포 안 훅만
실행한다. 이 훅은 세 AI의 훅 입력을 모두 받는다.
- 읽기 확인: Cursor beforeReadFile 본문, Read 도구 뒤(PostToolUse), 또는 셸로
  `cat .../core/boot.md`·`git -C <gpt> show <ref>:core/boot.md`를 실행한 출력이
  원격 main의 core/boot.md와 같으면 그 대화에 확인 표시를 남긴다.
- 거절: 확인 표시가 원격 main SHA와 다르면 파일 수정과 셸 명령을 거절한다.
  gpt를 받아 오고 부트를 읽는 명령만 예외로 통과한다.
- 시안 선행: 부트를 읽은 뒤에도 제품 화면 파일은 승인된 시안(충실도 2·3)이나
  사용자 원문의 시안 생략이 있을 때만 고친다. 정본은 workflow/34.
- 단계 상한: 사용자 최신 요청이 [분석/문제정의]·[기획/설계]·[재검토]면 git 레포 안
  planning/ 밖 파일 수정과 병합·되돌리기 명령을 거절한다. 정본은 workflow/20.
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

GPT_REPO = "cjddnjs123/gpt"
GPT_REMOTE = "https://github.com/cjddnjs123/gpt.git"
REMOTE_TTL = 120
WRITE_TOOLS = {
    "Write", "StrReplace", "Delete", "EditNotebook", "Edit", "ApplyPatch", "MultiEdit",
    "NotebookEdit", "apply_patch", "write_file", "edit_file",
}
SHELL_TOOLS = {"Shell", "Bash", "shell", "exec_command", "run_terminal_cmd", "local_shell"}
READ_TOOLS = {"Read", "read_file"}
DENY = (
    "[boot-gate] 이 대화에서 현재 gpt main의 core/boot.md를 읽기 전이라 막았습니다.\n"
    "허용: gpt clone·fetch·merge --ff-only, boot.md 전문 읽기(읽기 도구·cat·git show, &&만). "
    "자세히: gpt templates/boot_gate/README.md"
)
COPY_HINT = "\ngpt를 못 받으면 사본: {path}"
NO_REMOTE = (
    "[boot-gate] gpt main 현재 커밋을 확인하지 못해 막았습니다. "
    "cjddnjs123/gpt 읽기 권한(git 인증·gh 로그인·GPT_READ_TOKEN)이 필요합니다."
)
NO_REMOTE_HINT = (
    "\n시도: {reason}\n"
    "클라우드면 GPT_READ_TOKEN 비밀값을 넣고 새 세션, Claude 클라우드는 gpt 저장소도 붙이세요"
    "(templates/boot_gate/claude_cloud_setup.sh)."
)
SHA = re.compile(r"^[0-9a-f]{7,40}$")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
STATUS_FLAGS = {"--short", "--branch", "-sb", "-s", "-b"}
LOG_FLAGS = {"-1", "--oneline", "--max-count=1", "-n1", "-n", "1"}
REV_FLAGS = {"HEAD", "origin/main", "--abbrev-ref", "--verify", "--short"}
SHOW_REFS = {"HEAD", "origin/main", "main"}


def state_dir():
    override = os.environ.get("BOOT_GATE_STATE_DIR")
    if override:
        return override
    return os.path.join(tempfile.gettempdir(), "gpt-boot-gate")


def copy_path(directory, sha):
    """잠금이 읽기 토큰으로 받은 부트 원문 사본 위치(원격 main 커밋별)."""
    return os.path.join(directory, "boot", sha, "core", "boot.md")


def same_path(left, right):
    try:
        return os.path.normcase(os.path.realpath(native_path(left))) == os.path.normcase(os.path.realpath(native_path(right)))
    except (OSError, ValueError):
        return False


def read_copy(directory, sha):
    if not sha:
        return None
    try:
        with open(copy_path(directory, sha), encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def prepare_copy(directory, sha, fetch):
    """사본이 없으면 fetch(sha)로 받아 둔다. 실패하면 None."""
    if read_copy(directory, sha) is not None:
        return copy_path(directory, sha)
    try:
        text = fetch(sha)
    except Exception:
        text = None
    if not text:
        return None
    path = copy_path(directory, sha)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    replace_text(path, norm(text) + "\n")
    return path


def replace_text(path, text):
    """병렬 훅이 같은 파일을 바꿔도 깨지지 않게 프로세스별 임시 파일로 쓰고 교체한다."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows는 다른 프로세스가 대상을 열고 있는 동안 교체를 거부한다.
            if attempt == 4:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            time.sleep(0.05)


def safe_id(value):
    if not value or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", str(value)):
        return None
    if ".." in str(value):
        return None
    return str(value).replace(":", "_")


def norm(text):
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def event_of(payload):
    return str(payload.get("hook_event_name") or payload.get("hookEventName") or "").lower()


def conversation_of(payload):
    return payload.get("conversation_id") or payload.get("session_id") or payload.get("sessionId")


def is_cursor(payload):
    return "conversation_id" in payload or "cursor_version" in payload


def marker_path(directory, conversation_id):
    cid = safe_id(conversation_id)
    if not cid:
        return None
    return os.path.join(directory, cid + ".json")


def boot_digest(text):
    """부트 원문 내용 지문. main이 움직여도 core/boot.md가 같으면 다시 막지 않는 데 쓴다."""
    return hashlib.sha256(norm(text).encode("utf-8")).hexdigest()


def read_marker_data(directory, conversation_id):
    path = marker_path(directory, conversation_id)
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("sha"), str) or not FULL_SHA.fullmatch(data["sha"]):
        return {}
    return data


def read_marker(directory, conversation_id):
    return read_marker_data(directory, conversation_id).get("sha")


def write_marker(directory, conversation_id, sha, boot_text=None):
    path = marker_path(directory, conversation_id)
    if not path:
        return
    os.makedirs(directory, exist_ok=True)
    data = {"sha": sha}
    if boot_text is not None:
        data["boot"] = boot_digest(boot_text)
    replace_text(path, json.dumps(data))


def same_boot_unchanged(directory, conversation_id, sha, fetch):
    """표지의 SHA가 옛것이어도 현재 main의 부트 원문이 같으면 표지를 새 SHA로 옮기고 참."""
    data = read_marker_data(directory, conversation_id)
    if not data.get("boot"):
        return False
    text = read_copy(directory, sha)
    if text is None and fetch:
        path = prepare_copy(directory, sha, fetch)
        text = read_copy(directory, sha) if path else None
    if text is None or boot_digest(text) != data["boot"]:
        return False
    write_marker(directory, conversation_id, sha, text)
    return True


def tool_name(payload):
    return str(payload.get("tool_name") or payload.get("toolName") or "")


def tool_input(payload):
    raw = payload.get("tool_input")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def command_of(payload):
    if event_of(payload) in {"beforeshellexecution", "aftershellexecution"}:
        return str(payload.get("command") or "")
    inner = tool_input(payload)
    command = inner.get("command") or inner.get("cmd")
    if isinstance(command, list):
        if len(command) >= 3 and command[0] in {"bash", "sh", "zsh"} and command[1] in {"-lc", "-c"}:
            return str(command[2])
        return " ".join(shlex.quote(str(item)) for item in command)
    if command:
        return str(command)
    return str(payload.get("command") or "")


def cwd_of(payload):
    inner = tool_input(payload)
    return str(inner.get("working_directory") or inner.get("workdir") or inner.get("cwd") or payload.get("cwd") or "")


def is_pre(payload):
    return event_of(payload) in {"beforeshellexecution", "pretooluse"}


def is_shell_tool(payload):
    if event_of(payload) in {"beforeshellexecution", "aftershellexecution"}:
        return True
    return tool_name(payload) in SHELL_TOOLS


def is_gated(payload):
    if not is_pre(payload):
        return False
    return is_shell_tool(payload) or tool_name(payload) in WRITE_TOOLS


def has_shell_meta(command):
    return bool(re.search(r"[\n;&|<>`]|\$\(", command))


def argv_of(command):
    if os.name == "nt":
        command = command.replace("\\", "/")
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return None


def native_path(path):
    """Git Bash 경로(/c/x)를 Windows 경로(C:/x)로 바꾼다."""
    path = str(path or "")
    if os.name == "nt":
        match = re.fullmatch(r"/([A-Za-z])(/.*)?", path)
        if match:
            return match.group(1).upper() + ":" + (match.group(2) or "/")
    return path


def git_args(argv):
    """Return (repo_path or '', git sub-argv) for a git command, or None."""
    if not argv or argv[0] != "git":
        return None
    args = argv[1:]
    repo = ""
    if args[:1] == ["-C"] and len(args) >= 2:
        repo = args[1]
        args = args[2:]
    if not args or args[0].startswith("-"):
        return None
    return repo, args


def is_gpt_url(url):
    text = (url or "").strip().rstrip("/")
    return text.endswith(GPT_REPO) or text.endswith(GPT_REPO + ".git") or ("github.com/" + GPT_REPO) in text


def resolve_repo(path, cwd):
    path = native_path(path)
    cwd = native_path(cwd)
    if not path:
        return cwd or ""
    if os.path.isabs(path):
        return path
    return os.path.join(cwd, path) if cwd else path


def inside(path, root):
    try:
        path = os.path.realpath(native_path(path))
        root = os.path.realpath(native_path(root))
    except (OSError, ValueError):
        return False
    return path == root or path.startswith(root + os.sep)


def boot_show_ref(item):
    if ":" not in item:
        return None
    ref, _, file_path = item.partition(":")
    if file_path != "core/boot.md":
        return None
    if ref in SHOW_REFS or SHA.fullmatch(ref):
        return ref
    return None


def boot_read_target(command, cwd):
    """부트를 읽는 셸 명령이면 (gpt 저장소 후보 경로, git show ref 또는 None)을 돌려준다."""
    if not command or has_shell_meta(command):
        return None
    argv = argv_of(command.strip())
    if not argv:
        return None
    if argv[0] == "cat" and len(argv) == 2 and argv[1].replace("\\", "/").endswith("core/boot.md"):
        path = resolve_repo(argv[1], cwd)
        return os.path.dirname(os.path.dirname(path)), None
    if argv[0].casefold() == "get-content":
        path, seen, index = None, set(), 1
        while index < len(argv):
            item = argv[index]
            flag = item.casefold()
            if flag == "-raw" and flag not in seen:
                seen.add(flag)
            elif flag in {"-path", "-literalpath", "-encoding"} and index + 1 < len(argv):
                index += 1
                value = argv[index]
                if flag == "-encoding":
                    if flag in seen or value.casefold() not in {"utf8", "utf-8", "utf8bom", "utf8nobom"}:
                        return None
                    seen.add(flag)
                elif path is not None or value.startswith(("-", "$", "@")):
                    return None
                else:
                    path = value
            elif not item.startswith(("-", "$", "@")) and path is None:
                path = item
            else:
                return None
            index += 1
        if path and not re.search(r"[?*\[\]$]", path) and path.replace("\\", "/").endswith("core/boot.md"):
            target = resolve_repo(path, cwd)
            return os.path.dirname(os.path.dirname(target)), None
    parsed = git_args(argv)
    if parsed:
        repo_arg, args = parsed
        if args[0] == "show" and len(args) == 2 and boot_show_ref(args[1]):
            return resolve_repo(repo_arg, cwd), boot_show_ref(args[1])
    return None


def chain_segments(command):
    """`a && b`처럼 &&로만 이은 명령이면 각 명령 목록을, 아니면 None을 돌려준다."""
    if not command or "&&" not in command:
        return None
    segments = [item.strip() for item in command.split("&&")]
    if any(not item or has_shell_meta(item) for item in segments):
        return None
    return segments


def command_allowed(command, cwd, is_gpt_repo, workspace_roots):
    segments = chain_segments(command)
    if segments:
        return all(command_allowed(item, cwd, is_gpt_repo, workspace_roots) for item in segments)
    if not command or has_shell_meta(command):
        return False
    target = boot_read_target(command, cwd)
    if target:
        return is_gpt_repo(target[0])
    argv = argv_of(command.strip())
    if not argv:
        return False
    if argv[0] == "gh":
        return gh_allowed(argv[1:])
    parsed = git_args(argv)
    if not parsed:
        return False
    repo_arg, args = parsed
    repo = resolve_repo(repo_arg, cwd)
    sub = args[0]
    rest = args[1:]
    if sub == "clone":
        return clone_allowed(rest, workspace_roots, cwd)
    if sub == "ls-remote":
        return not repo_arg and rest == [GPT_REMOTE, "refs/heads/main"]
    if not is_gpt_repo(repo if repo_arg else native_path(cwd or "")):
        return False
    if sub == "fetch":
        return rest in (["origin"], ["origin", "main"])
    if sub == "rev-parse":
        return bool(rest) and all(item in REV_FLAGS or SHA.fullmatch(item) for item in rest)
    if sub == "status":
        return all(item in STATUS_FLAGS for item in rest)
    if sub == "log":
        return bool(rest) and all(item in LOG_FLAGS for item in rest)
    if sub == "merge":
        return rest == ["--ff-only", "origin/main"]
    return False


def clone_allowed(rest, workspace_roots, cwd):
    if rest[:4] != ["--branch", "main", "--single-branch", GPT_REMOTE] or len(rest) != 5:
        return False
    dest = resolve_repo(rest[4], cwd)
    return not any(inside(dest, root) for root in workspace_roots or [] if root)


def gh_allowed(args):
    if not args or args[0] != "api":
        return False
    rest = args[1:]
    blocked = {"-X", "--method", "-f", "-F", "--raw-field", "--input"}
    if any(item in blocked or str(item).startswith("--method=") for item in rest):
        return False
    positionals = []
    index = 0
    while index < len(rest):
        item = rest[index]
        if item == "--jq" and index + 1 < len(rest):
            index += 2
            continue
        if item.startswith("-"):
            return False
        positionals.append(item)
        index += 1
    if len(positionals) != 1:
        return False
    target = positionals[0]
    return target in {
        "repos/cjddnjs123/gpt/commits/main",
        "repos/cjddnjs123/gpt/contents/core/boot.md",
    } or target.startswith("repos/cjddnjs123/gpt/contents/core/boot.md?")


def response_texts(value):
    """도구 응답(문자열·사전·목록)에서 문자열을 모두 꺼낸다."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from response_texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from response_texts(item)


def observes_boot(payload):
    """관찰 이벤트가 core/boot.md 읽기일 때만 참. 아니면 원격 main을 조회하지 않는다."""
    event = event_of(payload)
    if event == "beforereadfile":
        path = payload.get("file_path")
    elif event == "posttooluse" and tool_name(payload) in READ_TOOLS:
        inner = tool_input(payload)
        path = inner.get("file_path") or inner.get("path") or inner.get("filePath")
    elif event in {"posttooluse", "aftershellexecution"} and is_shell_tool(payload):
        command = command_of(payload)
        segments = chain_segments(command)
        return bool(boot_read_target(segments[-1] if segments else command, cwd_of(payload)))
    else:
        return False
    return str(native_path(path or "")).replace("\\", "/").endswith("core/boot.md")


def mark_read(payload, directory, remote_sha, git):
    event = event_of(payload)
    if event == "beforereadfile":
        consider_file(payload.get("file_path"), payload.get("content"), payload, directory, remote_sha, git)
        return
    if event not in {"posttooluse", "aftershellexecution"}:
        return
    if event == "posttooluse" and tool_name(payload) in READ_TOOLS:
        inner = tool_input(payload)
        path = native_path(inner.get("file_path") or inner.get("path") or inner.get("filePath") or "")
        if not path.replace("\\", "/").endswith("core/boot.md"):
            return
        try:
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
        except OSError:
            return
        consider_file(path, content, payload, directory, remote_sha, git)
        return
    if not is_shell_tool(payload):
        return
    command = command_of(payload)
    segments = chain_segments(command)
    target = boot_read_target(segments[-1] if segments else command, cwd_of(payload))
    if not target:
        return
    copy = read_copy(directory, remote_sha)
    if target[1] is None and copy is not None and same_path(target[0], os.path.dirname(os.path.dirname(copy_path(directory, remote_sha)))):
        outputs = [payload.get("output")] if event == "aftershellexecution" else [payload.get("tool_response"), payload.get("tool_output")]
        if any(norm(text) == norm(copy) or norm(copy) in norm(text) for text in response_texts(outputs)):
            write_marker(directory, conversation_of(payload), remote_sha, copy)
        return
    repo = git.toplevel(target[0])
    if not repo or not git.is_gpt_repo(repo) or not remote_sha:
        return
    expected = git.show_boot(repo, remote_sha)
    if expected is None:
        return
    expected = norm(expected)
    outputs = [payload.get("output")] if event == "aftershellexecution" else [payload.get("tool_response"), payload.get("tool_output")]
    for text in response_texts(outputs):
        if norm(text) == expected or (len(expected) > 200 and expected in norm(text)):
            write_marker(directory, conversation_of(payload), remote_sha, expected)
            return


def consider_file(path, content, payload, directory, remote_sha, git):
    path = native_path(path)
    if not str(path).replace("\\", "/").endswith("core/boot.md") or not remote_sha:
        return
    if same_path(path, copy_path(directory, remote_sha)):
        copy = read_copy(directory, remote_sha)
        if copy is not None and norm(content or "") == norm(copy):
            write_marker(directory, conversation_of(payload), remote_sha, copy)
        return
    repo = git.toplevel(os.path.dirname(path))
    if not repo or not git.is_gpt_repo(repo):
        return
    if git.head(repo) != remote_sha:
        return
    expected = git.show_boot(repo, remote_sha)
    if expected is None or norm(content or "") != norm(expected):
        return
    write_marker(directory, conversation_of(payload), remote_sha, expected)


def copy_cat_allowed(command, cwd, directory, sha):
    target = boot_read_target(command, cwd)
    if not target or target[1] is not None or read_copy(directory, sha) is None:
        return False
    return same_path(target[0], os.path.dirname(os.path.dirname(copy_path(directory, sha))))


SCREEN_EXTS = (".blade.php", ".vue", ".tsx", ".jsx", ".svelte", ".cshtml")
SCREEN_PATH_RE = re.compile(
    r"""(?:"(?P<q>[^"]+\.(?:blade\.php|vue|tsx|jsx|svelte|cshtml))"|'(?P<a>[^']+\.(?:blade\.php|vue|tsx|jsx|svelte|cshtml))'|(?P<u>[^\s"'<>|;&]+\.(?:blade\.php|vue|tsx|jsx|svelte|cshtml)))""",
    re.IGNORECASE,
)
SKIP_PREVIEW_RE = re.compile(r"시안\s*없이|바로\s*구현|시안\s*필요\s*없음|시안\s*생략")
APPROVE_PREVIEW_RE = re.compile(r"승인|확정|이대로|좋아|추천대로|진행해")
REJECT_PREVIEW_RE = re.compile(r"거절|아니|안\s*돼|하지\s*마")
FIDELITY_RE = re.compile(r"충실도\s*(?:\*\*)?\s*[:：]\s*(?:\*\*)?\s*([123])")
# 웹 충실도 2는 피그마 프레임이다(figma/04). 글자만으로는 구현이 열리지 않는다.
FIGMA_DESIGN_URL_RE = re.compile(
    r"https://(?:www\.)?figma\.com/(?:design|board|make)/[A-Za-z0-9]{10,}",
    re.I,
)
PREVIEW_DENY = (
    "[시안 선행] 제품 화면 파일을 고치기 전에 승인된 피그마 시안이 없습니다. "
    "웹 화면의 충실도 2는 대화에 figma.com/design 주소가 있는 시안입니다. "
    "그 주소를 보여 준 뒤 승인하거나, 시안 없이 진행하려면 그 말을 사용자 메시지에 적어야 합니다. "
    "[구현]·진행해만으로는 시안이 생략되지 않습니다. 정본: gpt workflow/34, figma/04."
)
PREVIEW_UNREAD = (
    "[시안 선행] 제품 화면 파일을 고치려 하지만 이 대화 원문을 확인하지 못해 막습니다. "
    "승인된 시안인지 알 수 없습니다. 정본: gpt workflow/34."
)


def is_screen_file(path):
    """사용자에게 보이는 화면 파일인지. 시험 파일은 제외한다."""
    norm = str(path or "").replace("\\", "/").lower()
    if not norm.endswith(SCREEN_EXTS):
        return False
    if any(part in norm for part in ("/tests/", "/__tests__/", ".test.", ".spec.")):
        return False
    return True


READONLY_HEADS = {"rg", "grep", "cat", "type", "less", "more", "head", "tail", "wc", "ls", "dir", "findstr", "sort", "uniq",
                  "get-content", "gc", "select-string", "sls", "get-childitem", "gci", "test-path", "echo", "write-output",
                  "select-object", "select", "sort-object", "measure-object", "where-object", "format-table",
                  "format-list", "out-string", "jq"}
# 이 머리는 뒤 옵션이 없을 때만 읽기 전용이다.
READONLY_UNLESS = {"sed": ("-i", "--in-place"), "eslint": ("--fix",), "prettier": ("--write", "-w")}
READONLY_GIT = {"diff", "show", "log", "status", "grep", "blame", "fetch", "ls-files", "rev-parse", "remote", "merge-base"}
RUNNER_HEADS = {"npx", "bunx"}
QUOTED_RE = re.compile(r"\"[^\"]*\"|'[^']*'")
HARMLESS_REDIRECT_RE = re.compile(r"\d?>\s*(?:&\d|\$null|/dev/null|nul)(?![\w.])", re.IGNORECASE)


def split_chain(command):
    """따옴표 밖의 ; && || | 줄바꿈에서 명령을 나눈다."""
    text, parts, current, quote, index = command or "", [], [], "", 0
    while index < len(text):
        char = text[index]
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif text.startswith(("&&", "||"), index):
            parts.append("".join(current))
            current, index = [], index + 2
            continue
        elif char in ";|\n":
            parts.append("".join(current))
            current, index = [], index + 1
            continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def head_argv(segment):
    """실행할 명령 이름(소문자, .exe 제외)과 인자. npx·bunx와 그 옵션은 건너뛴다."""
    argv = argv_of(segment) or []
    while argv:
        head = os.path.basename(argv[0]).lower()
        head = head[:-4] if head.endswith(".exe") else head
        if head not in RUNNER_HEADS:
            return [head] + argv[1:]
        argv = argv[1:]
        while argv and argv[0].startswith("-"):
            argv = argv[1:]
    return []


def git_subcommand_args(args):
    while len(args) >= 2 and args[0] in {"-C", "-c"}:
        args = args[2:]
    return args


def shell_is_readonly(command):
    """이어 붙인 명령(; && || |)은 조각마다 읽기 전용이어야 한다. 파일로 내보내는 >는 쓰기다."""
    segments = split_chain(command)
    return bool(segments) and all(segment_is_readonly(part) for part in segments)


def segment_is_readonly(segment):
    if ">" in HARMLESS_REDIRECT_RE.sub("", QUOTED_RE.sub("", segment)):
        return False
    argv = head_argv(segment)
    if not argv:
        return False
    head, args = argv[0], argv[1:]
    if head in READONLY_HEADS:
        return True
    if head in READONLY_UNLESS:
        return not any(arg.startswith(flag) for arg in args for flag in READONLY_UNLESS[head])
    if head == "git":
        args = git_subcommand_args(args)
        return bool(args) and args[0] in READONLY_GIT
    return False


def write_paths_of(payload):
    """파일 수정 도구가 고치려는 경로(셸 제외)."""
    inner = tool_input(payload)
    candidates = []
    for key in ("path", "file_path", "filePath", "target_file", "target", "notebook_path"):
        value = inner.get(key)
        if isinstance(value, str):
            candidates.append(value)
    files = inner.get("files")
    if isinstance(files, list):
        for item in files:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                for key in ("path", "file_path"):
                    if isinstance(item.get(key), str):
                        candidates.append(item[key])
    patch = inner.get("patch") or inner.get("input")
    if isinstance(patch, str):
        candidates.extend(PATCH_FILE_RE.findall(patch))
    return candidates


PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.MULTILINE)


def screen_paths_of(payload):
    """이번 도구가 고치려는 화면 파일 경로."""
    found = []
    candidates = write_paths_of(payload)
    if is_shell_tool(payload):
        command = command_of(payload)
        if command and not shell_is_readonly(command):
            for match in SCREEN_PATH_RE.finditer(command):
                candidates.append(next(group for group in match.groups() if group))
    for path in candidates:
        if is_screen_file(path):
            found.append(path)
    return found


def flatten_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = []
        if isinstance(value.get("text"), str):
            parts.append(value["text"])
        content = value.get("content")
        if content is not None and content is not value:
            parts.append(flatten_text(content))
        return "\n".join(part for part in parts if part)
    if isinstance(value, list):
        return "\n".join(flatten_text(item) for item in value)
    return ""


TRANSCRIPT_TAIL = 2_000_000


def transcript_turns(path):
    """대화 기록 끝부분의 (역할, 본문) 목록. 잘린 첫 줄은 버려진다. 역할을 모르면 빈 목록."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - TRANSCRIPT_TAIL))
            raw = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    turns = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        role = str(data.get("role") or data.get("type") or "").lower()
        if role not in {"user", "human", "assistant", "ai"}:
            continue
        body = data.get("message", data)
        if role in {"user", "human"} and is_tool_result(body):
            continue
        turns.append((role, flatten_text(body)))
    return turns


def is_tool_result(body):
    """Claude 기록은 도구 결과도 user 줄로 남긴다. 사용자 원문이 아니다."""
    content = body.get("content") if isinstance(body, dict) else None
    return isinstance(content, list) and any(
        isinstance(item, dict) and item.get("type") == "tool_result" for item in content)


def transcript_path_of(payload):
    for key in ("transcript_path", "transcriptPath"):
        path = payload.get(key)
        if isinstance(path, str) and os.path.isfile(path):
            return path
    inner = tool_input(payload)
    for key in ("transcript_path", "transcriptPath"):
        path = inner.get(key)
        if isinstance(path, str) and os.path.isfile(path):
            return path
    cid = conversation_of(payload)
    if not cid or not re.fullmatch(r"[0-9a-fA-F-]{16,}", cid):
        return ""
    root = os.path.join(os.path.expanduser("~"), ".cursor", "projects")
    if not os.path.isdir(root):
        return ""
    name = cid + ".jsonl"
    try:
        projects = os.listdir(root)
    except OSError:
        return ""
    for project in projects:
        candidate = os.path.join(root, project, "agent-transcripts", cid, name)
        if os.path.isfile(candidate):
            return candidate
    return ""


def preview_allows(turns):
    """시안 생략, 충실도 3 승인, 또는 피그마 주소가 있는 충실도 2 승인만 통과."""
    grade = ""
    figma = False
    for role, text in turns:
        if role in {"assistant", "ai"}:
            if FIGMA_DESIGN_URL_RE.search(text or ""):
                figma = True
            found = FIDELITY_RE.findall(text or "")
            if found:
                grade = found[-1]
            continue
        if role not in {"user", "human"}:
            continue
        text = request_text(text)
        if "?" in text or "？" in text or REJECT_PREVIEW_RE.search(text):
            continue
        if SKIP_PREVIEW_RE.search(text):
            return True
        if grade == "2" and figma and APPROVE_PREVIEW_RE.search(text):
            return True
        if grade == "3" and APPROVE_PREVIEW_RE.search(text):
            return True
    return False


def visual_preview_block(payload):
    """화면 파일 수정인데 시안 증거가 없으면 거절 문구, 아니면 빈 글."""
    if not screen_paths_of(payload):
        return ""
    path = transcript_path_of(payload)
    if not path:
        return PREVIEW_UNREAD
    turns = transcript_turns(path)
    if turns is None:
        return PREVIEW_UNREAD
    if preview_allows(turns):
        return ""
    return PREVIEW_DENY


ANALYSIS_STAGES = {"분석/문제정의", "분석", "문제정의"}
PLANNING_STAGES = {"기획/설계", "기획", "설계", "재검토"}
OPEN_STAGES = {"구현", "재검증", "정리"}
STAGE_PREFIX_RE = re.compile(r"^\s*((?:\[[^\]\n]{1,30}\]\s*)+)")
USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
# 종료 훅 후속 지시도 대화 기록에 사용자 메시지로 남는다. 사용자 요청이 아니므로 단계 판정에서 건너뛴다.
HOOK_FOLLOWUP_RE = re.compile(r"^\s*(?:방금 답변에 지금 처리할 수 있는 일을|\[gpt 훅)")
# 접두사 단계 전부: 병합·되돌리기·게시·배포. 분석 단계만: 커밋·push·PR(기획 단계는 planning 커밋·PR 허용).
STAGE_GIT_ANY = {"rebase", "reset", "cherry-pick", "revert"}
STAGE_GIT_ANALYSIS = {"commit", "push"}
STAGE_GH_RELEASE = {"create", "upload", "edit", "delete"}
STAGE_DENY = (
    "[단계 상한] 사용자의 최신 요청이 [{stage}] 단계라서 {what}을(를) 막았습니다. "
    "이 단계 산출물은 채팅이나 planning/ 아래 문서로 냅니다. 구현은 사용자가 [구현]이나 "
    "접두사 없는 요청으로 넘긴 뒤 합니다. 정본: gpt workflow/20 단계별 쓰기 상한."
)


def request_text(text):
    match = USER_QUERY_RE.search(text or "")
    return match.group(1) if match else (text or "")


def latest_stage(turns):
    """사용자 최신 요청의 첫 줄 접두사가 쓰기 제한 단계면 그 이름, 아니면 빈 글."""
    for role, text in reversed(turns):
        if role not in {"user", "human"}:
            continue
        body = request_text(text)
        if not body.strip() or HOOK_FOLLOWUP_RE.match(body):
            continue
        first = next((line for line in body.splitlines() if line.strip()), "")
        match = STAGE_PREFIX_RE.match(first)
        if not match:
            return ""
        tokens = [token.strip() for token in re.findall(r"\[([^\]\n]+)\]", match.group(1))]
        stages = [token for token in tokens if token in ANALYSIS_STAGES | PLANNING_STAGES | OPEN_STAGES]
        if stages and not any(stage in OPEN_STAGES for stage in stages):
            return stages[0]
        return ""
    return ""


def in_git_repo(path, cwd):
    path = native_path(path)
    if not os.path.isabs(path):
        path = os.path.join(native_path(cwd or "") or os.getcwd(), path)
    folder = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(folder, ".git")):
            return True
        parent = os.path.dirname(folder)
        if parent == folder:
            return False
        folder = parent


def is_planning_path(path):
    norm_path = "/" + str(path or "").replace("\\", "/").lower()
    return "/planning/" in norm_path or "/.cursor/plans/" in norm_path


def stage_shell_level(command):
    """'any'(접두사 단계 전부 막음), 'analysis'(분석 단계만 막음), ''(제한 없음). 조각별 실행 명령만 본다."""
    level = ""
    for segment in split_chain(command):
        argv = head_argv(segment)
        head, args = (argv[0], argv[1:]) if argv else ("", [])
        if head == "git":
            args = git_subcommand_args(args)
            sub = args[0] if args else ""
            if sub in STAGE_GIT_ANY or (sub == "merge" and "--ff-only" not in args):
                return "any"
            if sub in STAGE_GIT_ANALYSIS:
                level = "analysis"
        elif head == "gh":
            if (args[:2] in (["pr", "merge"], ["workflow", "run"])
                    or (args[:1] == ["release"] and args[1:2] and args[1] in STAGE_GH_RELEASE)):
                return "any"
            if args[:1] == ["pr"] and args[1:2] and args[1] in {"create", "edit", "ready"}:
                level = "analysis"
        elif (head == "wrangler" and args[:1] in (["deploy"], ["publish"])) or (
                head == "vercel" and ("deploy" in args[:1] or "--prod" in args)):
            return "any"
    return level


def stage_block(payload):
    """단계 접두사 요청에서 상한을 넘는 쓰기면 거절 문구, 아니면 빈 글."""
    level = ""
    if is_shell_tool(payload):
        level = stage_shell_level(command_of(payload) or "")
        if not level:
            return ""
        what = "병합·되돌리기·게시 명령"
    else:
        paths = [p for p in write_paths_of(payload) if not is_planning_path(p) and in_git_repo(p, cwd_of(payload))]
        if not paths:
            return ""
        what = "레포 파일 수정"
    path = transcript_path_of(payload)
    turns = transcript_turns(path) if path else None
    stage = latest_stage(turns or [])
    if not stage:
        return ""
    if level == "analysis" and stage in PLANNING_STAGES:
        return ""
    return STAGE_DENY.format(stage=stage, what=what)


def finish_unlocked(payload):
    """부트 확인 뒤 단계 상한·시안 잠금. 이 판정이 실패하면 막지 않는다(부트 잠금만 실패 시 막음)."""
    try:
        reason = stage_block(payload) or visual_preview_block(payload)
    except Exception:
        reason = ""
    if reason:
        return "deny", reason
    return "allow", ""


def hooks_disabled():
    """공통 훅 비상 정지(AI_HOOKS_DISABLED=1 또는 ~/.ai-hooks/local.json의 "disabled": true)는 이 사본도 멈춘다."""
    if os.environ.get("AI_HOOKS_DISABLED") == "1":
        return True
    try:
        with open(os.path.join(os.path.expanduser("~"), ".ai-hooks", "local.json"), encoding="utf-8") as handle:
            return '"disabled":true' in "".join(handle.read().split())
    except OSError:
        return False


def evaluate(payload, *, directory, remote_sha, git, boot_copy=None):
    """판정 결과: ('allow'|'deny', 메시지)."""
    event = event_of(payload)
    if event in {"beforereadfile", "posttooluse", "aftershellexecution"}:
        try:
            if not observes_boot(payload):
                return "allow", ""
            sha = remote_sha()
        except Exception:
            sha = None
        try:
            mark_read(payload, directory, sha, git)
        except Exception:
            pass
        return "allow", ""
    if not is_gated(payload):
        return "allow", ""
    if is_shell_tool(payload) and command_allowed(
        command_of(payload),
        cwd_of(payload),
        git.is_gpt_repo,
        payload.get("workspace_roots") or [],
    ):
        return "allow", ""
    reason = ""
    try:
        sha = remote_sha()
    except RemoteUnavailable as error:
        sha, reason = None, str(error)
    except Exception:
        sha = None
    if not sha:
        return "deny", NO_REMOTE + (NO_REMOTE_HINT.format(reason=reason) if reason else "")
    if read_marker(directory, conversation_of(payload)) == sha:
        return finish_unlocked(payload)
    if same_boot_unchanged(directory, conversation_of(payload), sha, boot_copy):
        return finish_unlocked(payload)
    if is_shell_tool(payload) and copy_cat_allowed(command_of(payload), cwd_of(payload), directory, sha):
        return "allow", ""
    path = prepare_copy(directory, sha, boot_copy) if boot_copy else None
    return "deny", DENY + (COPY_HINT.format(path=path.replace("\\", "/")) if path else "")


class LiveGit:
    def _run(self, args, **kwargs):
        return subprocess.run(["git"] + args, capture_output=True, timeout=10, **kwargs)

    def toplevel(self, path):
        path = native_path(path)
        if not path or not os.path.isdir(path):
            return None
        result = self._run(["-C", path, "rev-parse", "--show-toplevel"], text=True)
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def is_gpt_repo(self, path):
        path = native_path(path)
        if not path or not os.path.isdir(path):
            return False
        result = self._run(["-C", path, "remote", "get-url", "origin"], text=True)
        return result.returncode == 0 and is_gpt_url(result.stdout)

    def head(self, repo):
        result = self._run(["-C", repo, "rev-parse", "HEAD"], text=True)
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def show_boot(self, repo, sha):
        result = self._run(["-C", repo, "show", f"{sha}:core/boot.md"])
        if result.returncode != 0:
            return None
        return result.stdout


class SourceFailure(Exception):
    """원격 SHA 조회 실패 사유(비밀값 없음)."""


class RemoteUnavailable(RuntimeError):
    """모든 조회가 실패했다. 메시지는 출처별 사유이며 값·토큰을 담지 않는다."""


def failure_reason(error):
    if isinstance(error, SourceFailure):
        return str(error)
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        return "네트워크 오류(" + str(error.reason)[:80] + ")"
    if isinstance(error, FileNotFoundError):
        return "명령 없음"
    if isinstance(error, subprocess.TimeoutExpired):
        return "시간 초과"
    return type(error).__name__


def _ls_remote():
    result = subprocess.run(
        ["git", "ls-remote", GPT_REMOTE, "refs/heads/main"],
        capture_output=True, text=True, timeout=20,
        env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
    )
    if result.returncode != 0:
        raise SourceFailure(f"실패(종료 코드 {result.returncode})")
    parts = result.stdout.split()
    return parts[0] if parts else None


def _gh_api():
    result = subprocess.run(
        ["gh", "api", f"repos/{GPT_REPO}/commits/main", "--jq", ".sha"],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode != 0:
        raise SourceFailure(f"실패(종료 코드 {result.returncode})")
    return result.stdout.strip()


def _token_api():
    token = os.environ.get("GPT_READ_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SourceFailure("토큰 환경 변수 없음")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{GPT_REPO}/commits/main",
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github.sha"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode().strip()


SOURCES = (_ls_remote, _gh_api, _token_api)
SOURCE_NAMES = {"_ls_remote": "git ls-remote", "_gh_api": "gh api", "_token_api": "토큰 API"}


def token_boot(sha):
    """읽기 토큰으로 원격 커밋의 core/boot.md 원문을 받는다(토큰은 출력하지 않는다)."""
    token = os.environ.get("GPT_READ_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token or not FULL_SHA.fullmatch(sha or ""):
        return None
    request = urllib.request.Request(
        f"https://api.github.com/repos/{GPT_REPO}/contents/core/boot.md?ref={sha}",
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github.raw"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8")


def gh_boot(sha):
    """gh 로그인으로 원격 커밋의 core/boot.md 원문을 받는다. 읽기 토큰이 없는 PC용."""
    if not FULL_SHA.fullmatch(sha or ""):
        return None
    result = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{GPT_REPO}/contents/core/boot.md?ref={sha}"],
        capture_output=True, timeout=20,
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace")


def fetch_boot(sha):
    for fetch in (token_boot, gh_boot):
        try:
            text = fetch(sha)
        except Exception:
            text = None
        if text:
            return text
    return None


def remote_main_sha(directory, now=None, sources=SOURCES):
    now = time.time() if now is None else now
    cache = os.path.join(directory, "remote-main.json")
    try:
        with open(cache, encoding="utf-8") as handle:
            data = json.load(handle)
        if now - float(data.get("at", 0)) < REMOTE_TTL and FULL_SHA.fullmatch(data.get("sha", "")):
            return data["sha"]
    except (OSError, ValueError, TypeError):
        pass
    sha = None
    reasons = []
    for source in sources:
        name = SOURCE_NAMES.get(getattr(source, "__name__", ""), getattr(source, "__name__", "조회"))
        try:
            sha = source()
        except Exception as error:
            sha = None
            reasons.append(f"{name} {failure_reason(error)}")
        else:
            if not (sha and FULL_SHA.fullmatch(sha)):
                reasons.append(f"{name} 결과 없음")
        if sha and FULL_SHA.fullmatch(sha):
            break
        sha = None
    if not sha:
        raise RemoteUnavailable("; ".join(reasons) or "조회 수단 없음")
    try:
        os.makedirs(directory, exist_ok=True)
        replace_text(cache, json.dumps({"sha": sha, "at": now}))
    except OSError:
        pass
    return sha


def emit(payload, decision, message):
    """세 AI가 모두 알아듣는 형식으로 판정을 낸다. 거절은 종료 코드 2다."""
    if decision == "allow":
        if is_cursor(payload):
            print(json.dumps({"permission": "allow"}))
        return 0
    if is_cursor(payload):
        # 종료 코드 2면 Cursor는 표준 출력 원문을 AI에게 넘긴다. 같은 문장을 여러 필드에 넣지 않는다.
        body = {"permission": "deny", "agent_message": message}
    else:
        body = {
            "permission": "deny",
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            },
        }
    print(json.dumps(body, ensure_ascii=False))
    print(message, file=sys.stderr)
    return 2


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        payload = None
    if hooks_disabled():
        return emit(payload if isinstance(payload, dict) else {}, "allow", "")
    if not isinstance(payload, dict):
        lowered = raw.lower()
        if "beforereadfile" in lowered or "posttooluse" in lowered or "aftershell" in lowered:
            return emit({"conversation_id": ""}, "allow", "")
        return emit({}, "deny", DENY)
    directory = state_dir()
    try:
        decision, message = evaluate(
            payload, directory=directory, remote_sha=lambda: remote_main_sha(directory), git=LiveGit(),
            boot_copy=fetch_boot,
        )
    except Exception:
        observe = event_of(payload) in {"beforereadfile", "posttooluse", "aftershellexecution"}
        decision, message = ("allow", "") if observe else ("deny", DENY)
    return emit(payload, decision, message)


if __name__ == "__main__":
    sys.exit(main())
