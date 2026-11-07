#!/usr/bin/env python3
"""제품 화면 시안 선행과 단계 상한만 보는 레포 훅(옛 이름 boot-gate는 설치 경로 호환으로 유지).

정본: cjddnjs123/gpt templates/boot_gate/boot_gate.py
제품 레포의 .cursor/hooks/boot-gate.py는 scripts/sync_boot_gate.py가 만든 사본이다.
사본을 직접 고치지 말고 정본을 고친 뒤 다시 배포한다.

클라우드 에이전트(Cursor·Claude·Codex)는 PC 전역 훅을 실행하지 않고 레포 안 훅만
실행하므로 이 파일이 세 AI의 훅 입력을 모두 받는다.
- 시안 선행: 제품 화면 파일은 승인된 시안(충실도 2·3)이나 사용자 원문의 시안 생략이
  있을 때만 고친다. 정본은 workflow/34.
  2026-11-07 대표 지시(「시안 잠금 훅에 사후 검토 통과 경로를 넣어줘」): 작은 수정(문구·간격·색·기존 구성요소
  안 조정·화면 버그)은 도우미가 최신 사용자 메시지 뒤에 `사후 검토:` 줄을 쓰면 통과하고, 구현 뒤 실제 화면
  캡처로 사후 검토한다(workflow/34 §2.1). 새 화면·큰 변경은 계속 시안 승인이 필요하다.
- 단계 상한: 사용자 최신 요청이 [분석/문제정의]·[기획/설계]·[재검토]면 git 레포 안
  planning/ 밖 파일 수정과 병합·되돌리기 명령을 거절한다. 정본은 workflow/20.
2026-11-06 덜어내기 추천 3(대표 승인 "추천안대로 할게"): core/boot.md를 읽기 전 거절하던 필독 검사는 지웠다.
판정 중 오류는 막지 않는다(fail-open).
"""
import json
import os
import re
import shlex
import sys

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
APPROVE_PREVIEW_RE = re.compile(r"승인|확정|이대로|좋아|추천대로|진행해")
REJECT_PREVIEW_RE = re.compile(r"거절|아니|안\s*돼|하지\s*마")
# 작은 화면 수정의 사후 검토 선언(workflow/34 §2.1). 최신 사용자 메시지 뒤 도우미 글에 있어야 한다.
POST_REVIEW_RE = re.compile(r"^[ \t]*\**[ \t]*사후[ \t]*검토[ \t]*\**[ \t]*[:：]", re.M)
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
    "새 화면·고객이 보는 큰 변경이 아닌 작은 수정(문구·간격·색·기존 구성요소 안 조정·화면 버그)이면 "
    "`사후 검토: <무엇>` 줄을 먼저 쓰고 구현 뒤 실제 화면 캡처로 보고합니다. "
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
    """시안 생략, 충실도 3 승인, 피그마 주소가 있는 충실도 2 승인, 또는 최신 사용자 메시지 뒤 `사후 검토:` 선언만 통과."""
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
        if "?" in text or "？" in text or REJECT_PREVIEW_RE.search(text):
            continue
        if SKIP_PREVIEW_RE.search(text):
            return True
        if grade == "2" and figma and APPROVE_PREVIEW_RE.search(text):
            return True
        if grade == "3" and APPROVE_PREVIEW_RE.search(text):
            return True
    return post_review


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


def evaluate(payload):
    """판정 결과: ('allow'|'deny', 메시지). 쓰기·셸 직전만 본다."""
    try:
        if not is_gated(payload):
            return "allow", ""
    except Exception:
        return "allow", ""
    return finish_unlocked(payload)


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
    if not isinstance(payload, dict) or hooks_disabled():
        return emit(payload if isinstance(payload, dict) else {}, "allow", "")
    return emit(payload, *evaluate(payload))


if __name__ == "__main__":
    sys.exit(main())
