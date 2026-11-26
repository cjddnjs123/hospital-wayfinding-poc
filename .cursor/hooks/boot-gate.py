#!/usr/bin/env python3
"""제품 화면 시안 선행과 단계 상한만 보는 레포 훅(옛 이름 boot-gate는 설치 경로 호환).

정본: cjddnjs123/gpt templates/boot_gate/boot_gate.py. 제품 레포 .cursor/hooks/boot-gate.py는
scripts/sync_boot_gate.py가 만든 사본이니 정본을 고친 뒤 다시 배포한다.
클라우드 에이전트는 레포 안 훅만 실행하므로 이 파일이 세 AI의 훅 입력을 모두 받는다.
- 시안 선행(workflow/34): 화면 파일은 승인된 시안·사용자 원문의 시안 생략·작은 수정의 `사후 검토:` 줄이 있을 때만 고친다.
- 단계 상한(workflow/20): [분석/문제정의]·[기획/설계]·[재검토] 요청이면 planning/ 밖 수정과 병합·되돌리기를 거절한다.
판정 중 오류는 막지 않는다(fail-open).
"""
import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

WRITE_TOOLS = {
    "Write", "StrReplace", "Delete", "EditNotebook", "Edit", "ApplyPatch", "MultiEdit",
    "NotebookEdit", "apply_patch", "write_file", "edit_file",
}
SHELL_TOOLS = {"Shell", "Bash", "shell", "exec_command", "run_terminal_cmd", "local_shell"}


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


SCREEN_EXTS = (".blade.php", ".vue", ".tsx", ".jsx", ".svelte", ".cshtml")
SCREEN_PATH_RE = re.compile(
    r"""(?:"(?P<q>[^"]+\.(?:blade\.php|vue|tsx|jsx|svelte|cshtml))"|'(?P<a>[^']+\.(?:blade\.php|vue|tsx|jsx|svelte|cshtml))'|(?P<u>[^\s"'<>|;&]+\.(?:blade\.php|vue|tsx|jsx|svelte|cshtml)))""",
    re.IGNORECASE,
)
SKIP_PREVIEW_RE = re.compile(r"시안\s*없이|바로\s*구현|시안\s*필요\s*없음|시안\s*생략")
APPROVE_PREVIEW_RE = re.compile(r"승인|확정|이대로|시안대로|좋아|추천대로|진행해")
REJECT_PREVIEW_RE = re.compile(r"거절|아니|안\s*돼|하지\s*마")
# 사후 검토 선언(workflow/34 §2.1).
POST_REVIEW_RE = re.compile(r"^[ \t]*\**[ \t]*사후[ \t]*검토[ \t]*\**[ \t]*[:：]", re.M)
FIDELITY_RE = re.compile(r"충실도\s*(?:\*\*)?\s*[:：]\s*(?:\*\*)?\s*([123])")
# 웹 충실도 2는 피그마 프레임이다(figma/04). 글자만으로는 구현이 열리지 않는다.
FIGMA_DESIGN_URL_RE = re.compile(
    r"https://(?:www\.)?figma\.com/(?:design|board|make)/[A-Za-z0-9]{10,}",
    re.I,
)
PREVIEW_DENY = (
    "[시안 선행] 승인된 피그마 시안(figma.com/design 주소)이 없습니다. 대표 승인은 새 화면·큰 변경만. "
    "작은 수정(문구·간격·색·화면 버그)은 「시안 없이 진행해」 문구를 요청하지 말고 `사후 검토: <무엇>` 줄을 "
    "가장 최근 사용자 메시지 뒤, 수정하는 답 글의 도구 호출 앞에 줄 맨 앞부터 콜론까지 쓰고 재시도합니다"
    "(숨은 생각·인용은 불인정). 새 사용자 메시지가 오면 전에 쓴 줄은 무효입니다. "
    "정본: gpt workflow/34 §2.1."
)
# 피그마 주소의 파일 키와 프레임 번호. 다른 세션(부모)에서 받은 승인을 승인 장부로 맞춰 본다.
FIGMA_NODE_RE = re.compile(r"https://(?:www\.)?figma\.com/(?:design|board|make)/([A-Za-z0-9]{10,})([^\s)\]>\"'`]*)", re.I)
NODE_ID_RE = re.compile(r"[?&]node-id=([0-9]+[-:][0-9]+)")
# 승인 장부: Claude Stop 훅(gpt templates/claude/hooks/stop-gate.py)이 대표가 승인한 시안의 파일·프레임을 적는다.
APPROVAL_LEDGER = os.path.join(os.path.expanduser("~"), ".ai-hooks", "figma-approvals.jsonl")
APPROVAL_LEDGER_DAYS = 45
PREVIEW_UNREAD = (
    "[시안 선행] 대화 원문을 못 읽어 시안 승인을 확인할 수 없어 화면 파일 수정을 막습니다. 정본: gpt workflow/34."
)
CURSOR_REVIEW_DIR = os.path.join(os.path.expanduser("~"), ".ai-hooks", "state", "cursor-post-review")
CURSOR_REVIEW_MAX_AGE = 900
CURSOR_REVIEW_GUIDANCE = (
    ' Cursor에서 작은 수정이면 편집 직전 읽기 전용 셸로 파일마다 '
    '`git -C "<워크트리 절대 경로>" diff -- "<파일 상대 경로>" # 사후 검토: <무엇>`을 실행하세요. '
    '그다음 같은 사용자 요청에서 해당 파일을 편집하고 실제 화면을 검토합니다.'
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
                  "format-list", "out-string", "jq",
                  # 폴더 이동만 하는 머리(2026-11-26 `cd` 뒤 화면 파일 목록 읽기를 화면 수정으로 오판해 Cursor가 멈췄다).
                  "cd", "chdir", "pushd", "popd", "set-location", "sl", "push-location", "pop-location", "pwd", "get-location"}
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


def is_narration_block(value):
    """화면에 보인 글이 저장된 thinking 블록(FIT-1084)."""
    sig = value.get("signature")
    if value.get("type") != "thinking" or not isinstance(sig, str) or not isinstance(value.get("thinking"), str):
        return False
    try:
        return b"narration" in base64.b64decode(sig[:32], validate=True)
    except ValueError:
        return False


# narration은 모델이 쓴 글을 서버가 요약해 다시 쓴 것이라 `사후 검토:`가 「사후 검토 결과,」
# 「사후 검토용으로」처럼 바뀌어 남는다(2026-11-23 피팅웹 기록). 그 줄 머리를 원래 선언으로 되돌린다.
NARRATION_POST_REVIEW_RE = re.compile(r"^([ \t]*\**[ \t]*사후[ \t]*검토)[^\s,，:：]*[ \t]*[,，:：]?", re.M)
NARRATION_POST_REVIEW_NEG = ("없이", "생략", "불필요", "필요 없", "안 ", "않")


def narration_text(text):
    def restore(match):
        rest = text[match.end():match.end() + 8].lstrip()
        if rest.startswith(NARRATION_POST_REVIEW_NEG):
            return match.group(0)
        return "사후 검토: "
    return NARRATION_POST_REVIEW_RE.sub(restore, text)


def flatten_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = []
        if isinstance(value.get("text"), str):
            parts.append(value["text"])
        if is_narration_block(value):
            parts.append(narration_text(value["thinking"]))
        content = value.get("content")
        if content is not None and content is not value:
            parts.append(flatten_text(content))
        return "\n".join(part for part in parts if part)
    if isinstance(value, list):
        return "\n".join(flatten_text(item) for item in value)
    return ""


TRANSCRIPT_CHUNK = 1_000_000
TRANSCRIPT_LINE_CAP = 2_000_000   # 이보다 긴 줄(api-request-blob 등 수 MB)은 읽지 않고 건너뛴다.
TRANSCRIPT_SCAN_MAX = 64_000_000  # 거꾸로 훑는 최대 바이트.
TRANSCRIPT_TURNS_WANT = 400       # 역할 있는 줄을 이만큼 모으면 멈춘다.


def transcript_lines_reversed(handle):
    """파일 끝에서 앞으로 한 줄씩(바이트) 낸다. 너무 긴 줄은 내지 않고 버린다."""
    pos = handle.seek(0, os.SEEK_END)
    scanned, carry, dropping = 0, b"", False
    while pos > 0 and scanned < TRANSCRIPT_SCAN_MAX:
        size = min(TRANSCRIPT_CHUNK, pos)
        pos -= size
        scanned += size
        handle.seek(pos)
        chunk = handle.read(size)
        if dropping:
            cut = chunk.rfind(b"\n")
            if cut < 0:
                continue
            chunk, dropping = chunk[:cut + 1], False
        parts = (chunk + carry).split(b"\n")
        carry = parts[0]
        if len(carry) > TRANSCRIPT_LINE_CAP:
            carry, dropping = b"", True
        for line in reversed(parts[1:]):
            if 0 < len(line) <= TRANSCRIPT_LINE_CAP:
                yield line
    if pos == 0 and carry and not dropping:
        yield carry


def transcript_turns(path):
    """대화 기록 끝부분의 (역할, 본문) 목록(오래된 것부터). 역할을 모르면 빈 목록.

    끝에서 거꾸로 읽고, 수 MB짜리 api-request-blob 줄은 건너뛰며 역할 있는 줄을 충분히 모을 때까지 읽는다.
    """
    turns = []
    try:
        with open(path, "rb") as handle:
            for raw in transcript_lines_reversed(handle):
                if b"api-request-blob" in raw[:300]:
                    continue
                try:
                    data = json.loads(raw.decode("utf-8", "replace"))
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
                if len(turns) >= TRANSCRIPT_TURNS_WANT:
                    break
    except OSError:
        return None
    turns.reverse()
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
    """시안 생략·충실도 3 승인·피그마 주소 있는 승인·승인 장부에 있는 시안·최신 사용자 메시지 뒤 사후 검토만 통과.

    충실도 줄이 빠진 피그마 주소 승인은 충실도 2로 본다(줄이 빠졌다고 다시 묻지 않는다). `충실도: 1`만 열지 않는다.
    시안 이미지를 대화방에 올렸는지는 승인 요청을 보내는 순간 Stop 훅이 보고, 구현 때 다시 묻지 않는다(2026-11-18).
    """
    grade = ""
    figma = False
    post_review = False
    for role, text in turns:
        if role in {"assistant", "ai"}:
            if POST_REVIEW_RE.search(text or ""):
                post_review = True
            if FIGMA_DESIGN_URL_RE.search(text or ""):
                figma = True
            found = FIDELITY_RE.findall(text or "")
            if found:
                grade = found[-1]
            continue
        if role not in {"user", "human"}:
            continue
        text = request_text(text)
        if not HOOK_FOLLOWUP_RE.match(text):
            post_review = False
        said = said_clauses(text)
        if any(SKIP_PREVIEW_RE.search(c) for c in said):
            return True
        if any(APPROVE_PREVIEW_RE.search(c) for c in said) and (grade == "3" or (figma and grade != "1")):
            return True
    return post_review or ledger_allows(turns)


REPLY_MARK_RE = re.compile(r"<!--\s*reply\s*\d+\s*-->")
QUOTE_LINE_RE = re.compile(r"^\s*>")
# 한 메시지에 질문·지시·승인이 섞이면 글머리(*·-·숫자)·줄·문장 끝(?·.·!)마다 나눠 본다.
CLAUSE_SPLIT_RE = re.compile(r"\n|(?<=[?？.!。])\s+|\s+(?:[*•·-]|\d+[.)])\s+")


def said_clauses(text):
    """대표가 직접 쓴 마디 가운데 질문·거절이 아닌 것. 답글 인용(`> ` 줄)은 AI 글이라 뺀다.

    2026-11-24 「* 녹화도 가능해? * 어디에 넣을까? * 시안은 승인할게.」가 질문표 하나 때문에 승인으로 안 세어졌다.
    질문·거절은 그 마디에서만 승인을 지운다. 「승인할까?」처럼 승인 낱말이 든 질문은 여전히 승인이 아니다.
    """
    own = "\n".join(line for line in REPLY_MARK_RE.sub("", text or "").splitlines() if not QUOTE_LINE_RE.match(line))
    out = []
    for clause in CLAUSE_SPLIT_RE.split(own):
        clause = clause.strip().lstrip("*•·- ").strip()
        if clause and not ("?" in clause or "？" in clause or REJECT_PREVIEW_RE.search(clause)):
            out.append(clause)
    return out


def figma_nodes(text):
    """글 속 피그마 주소의 (파일 키, 프레임 번호 'a:b' 또는 '') 목록."""
    found = []
    for key, rest in FIGMA_NODE_RE.findall(text or ""):
        node = NODE_ID_RE.search(rest)
        found.append((key, node.group(1).replace("-", ":") if node else ""))
    return found


def ledger_allows(turns, ledger=None):
    """이 대화(부모가 보낸 인계문 포함)에 나온 피그마 프레임이 다른 세션에서 이미 승인됐으면 통과."""
    wanted = set()
    for _role, text in turns:
        wanted.update(figma_nodes(text))
    if not wanted:
        return False
    try:
        with open(ledger or APPROVAL_LEDGER, encoding="utf-8") as handle:
            rows = handle.read().splitlines()[-2000:]
    except OSError:
        return False
    import time
    oldest = time.time() - APPROVAL_LEDGER_DAYS * 86400
    for raw in rows:
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(row, dict) or float(row.get("epoch") or 0) < oldest:
            continue
        key = str(row.get("file") or "")
        nodes = {str(node).replace("-", ":") for node in row.get("nodes") or ()}
        for want_key, want_node in wanted:
            # 프레임이 적힌 승인은 같은 프레임만, 프레임 없는 파일 주소 승인은 같은 파일 주소만 연다(다른 프레임은 안 연다).
            if want_key == key and ((want_node and want_node in nodes) or (not want_node and not nodes)):
                return True
    return False


def cursor_review_key(payload, path):
    """Cursor가 붙인 대화·생성 ID와 실제 파일 경로에만 선언을 연결한다."""
    conversation = payload.get("conversation_id")
    generation = payload.get("generation_id")
    if not all(isinstance(value, str) and value for value in (conversation, generation)):
        return ""
    canonical = os.path.normcase(os.path.realpath(path))
    key = hashlib.sha256("\0".join((conversation, generation, canonical)).encode()).hexdigest()
    return os.path.join(CURSOR_REVIEW_DIR, key)


def record_cursor_review(payload):
    """Cursor가 현재 답변을 JSONL에 쓰기 전에 작은 수정 선언을 받을 수 있는 읽기 전용 명령."""
    if event_of(payload) != "beforeshellexecution" or not is_cursor(payload):
        return
    command = command_of(payload)
    prefix, _, comment = command.partition(" # ")
    if not re.fullmatch(r"사후[ \t]+검토[ \t]*[:：][ \t]*\S[^\r\n]*", comment):
        return
    if re.search(r"[\r\n;&|<>$`%!^*?\[\]]", command):
        return
    args = argv_of(prefix) or []
    if len(args) != 6 or args[:2] != ["git", "-C"] or args[3:5] != ["diff", "--"]:
        return
    root, relative = native_path(args[2]), native_path(args[5])
    if not os.path.isabs(root) or os.path.isabs(relative) or os.path.splitdrive(relative)[0]:
        return
    if not os.path.exists(os.path.join(root, ".git")):
        return
    root = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root, relative))
    if os.path.normcase(os.path.commonpath((root, path))) != os.path.normcase(root):
        return
    if not is_screen_file(path) or not os.path.isfile(path):
        return
    mark = cursor_review_key(payload, path)
    if not mark:
        return
    os.makedirs(CURSOR_REVIEW_DIR, exist_ok=True)
    with open(mark, "ab"):
        os.utime(mark, None)


def cursor_review_allows(payload, paths):
    if not is_cursor(payload) or event_of(payload) != "pretooluse" or tool_name(payload) not in {"Write", "StrReplace", "Edit"}:
        return False
    if len(paths) != 1:
        return False
    try:
        path = native_path(paths[0])
        if not os.path.isabs(path) or not os.path.isfile(path):
            return False
        mark = cursor_review_key(payload, path)
        return bool(mark) and 0 <= time.time() - os.stat(mark).st_mtime <= CURSOR_REVIEW_MAX_AGE
    except (OSError, ValueError):
        return False


def visual_preview_block(payload):
    """화면 파일 수정인데 시안 증거가 없으면 거절 문구, 아니면 빈 글."""
    paths = screen_paths_of(payload)
    if not paths:
        return ""
    path = transcript_path_of(payload)
    if not path:
        return PREVIEW_UNREAD
    turns = transcript_turns(path)
    if turns is None:
        return PREVIEW_UNREAD
    if preview_allows(turns) or cursor_review_allows(payload, paths):
        return ""
    if is_cursor(payload) and event_of(payload) == "pretooluse":
        return PREVIEW_DENY + CURSOR_REVIEW_GUIDANCE
    return PREVIEW_DENY


ANALYSIS_STAGES = {"분석/문제정의", "분석", "문제정의"}
PLANNING_STAGES = {"기획/설계", "기획", "설계", "재검토"}
OPEN_STAGES = {"구현", "재검증", "정리"}
STAGE_PREFIX_RE = re.compile(r"^\s*((?:\[[^\]\n]{1,30}\]\s*)+)")
USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
# 종료 훅 후속 지시는 사용자 요청이 아니라 판정에서 건너뛴다.
HOOK_FOLLOWUP_RE = re.compile(r"^\s*(?:방금 답변에 지금 처리할 수 있는 일을|\[gpt 훅)")
# 모든 제한 단계: 병합·되돌리기·게시·배포. 분석 단계만: 커밋·push·PR.
STAGE_GIT_ANY = {"rebase", "reset", "cherry-pick", "revert"}
STAGE_GIT_ANALYSIS = {"commit", "push"}
STAGE_GH_RELEASE = {"create", "upload", "edit", "delete"}
STAGE_DENY = (
    "[단계 상한] 최신 요청이 [{stage}] 단계라 {what}을(를) 막았습니다. 산출물은 채팅이나 planning/ 문서로 내고, "
    "구현은 사용자가 [구현]이나 접두사 없는 요청으로 넘긴 뒤 합니다. 정본: gpt workflow/20."
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
    """단계 상한·시안 잠금. 이 판정이 실패하면 막지 않는다."""
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


def emit(payload, decision, message):
    """Cursor는 JSON 판정, Claude와 Codex는 거절 시 종료 코드 2를 읽는다."""
    if decision == "allow":
        if is_cursor(payload):
            print(json.dumps({"permission": "allow"}))
        return 0
    if is_cursor(payload):
        print(json.dumps({"permission": "deny", "user_message": message, "agent_message": message}))
        print(message, file=sys.stderr)
        return 0
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


def evaluate(payload):
    """판정 결과: ('allow'|'deny', 메시지). 쓰기·셸 직전만 본다."""
    try:
        if not is_gated(payload):
            return "allow", ""
    except Exception:
        return "allow", ""
    try:
        record_cursor_review(payload)
    except (OSError, ValueError):
        # 등록에 실패해도 이후 편집의 시안 검사는 그대로 실행한다.
        pass
    return finish_unlocked(payload)


REPO_COPY = os.path.join(".cursor", "hooks", "boot-gate.py")
DELEGATED_ENV = "BOOT_GATE_DELEGATED"


def worktree_copy(payload):
    """작업 폴더(워크트리)에 이 파일과 다른 사본이 있으면 그 경로.

    Claude 레포 훅 명령 `$(git rev-parse --show-toplevel)`은 훅 프로세스 위치로 풀려 워크트리 세션에서도
    저장소 본 폴더(다른 세션 브랜치)의 옛 사본이 돌았다(2026-11-26 리드파인더: 「사후 검토:」 불인정).
    훅 입력의 작업 폴더에서 위로 올라가며 처음 만나는 사본이 그 작업의 코드다. 레포 사본만 넘긴다.
    """
    here = os.path.realpath(os.path.abspath(__file__))
    if not os.path.normcase(here).endswith(os.path.normcase(os.sep + REPO_COPY)):
        return ""  # PC 전역 실행 판·정본 원본은 넘기지 않는다.
    start = native_path(cwd_of(payload))
    if not start or not os.path.isabs(start):
        return ""
    folder = os.path.realpath(start)
    while True:
        candidate = os.path.join(folder, REPO_COPY)
        if os.path.isfile(candidate):
            if os.path.normcase(os.path.realpath(candidate)) == os.path.normcase(here):
                return ""
            with open(candidate, "rb") as theirs, open(here, "rb") as mine:
                return "" if theirs.read() == mine.read() else candidate
        parent = os.path.dirname(folder)
        if parent == folder:
            return ""
        folder = parent


def delegate(copy, raw):
    """작업 폴더 사본에 같은 입력을 넘기고 그 판정을 그대로 낸다. 실패하면 None(여기서 판정)."""
    env = dict(os.environ, **{DELEGATED_ENV: "1", "PYTHONIOENCODING": "utf-8"})
    try:
        proc = subprocess.run([sys.executable, copy], input=raw, capture_output=True, env=env, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 2):
        return None
    sys.stdout.buffer.write(proc.stdout)
    sys.stderr.buffer.write(proc.stderr)
    return proc.returncode


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    data = sys.stdin.buffer.read()
    raw = data.decode("utf-8", "replace")
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or hooks_disabled():
        return emit(payload if isinstance(payload, dict) else {}, "allow", "")
    if os.environ.get(DELEGATED_ENV) != "1":
        try:
            copy = worktree_copy(payload)
        except OSError:
            copy = ""
        if copy:
            code = delegate(copy, data)
            if code is not None:
                return code
    return emit(payload, *evaluate(payload))


if __name__ == "__main__":
    sys.exit(main())
