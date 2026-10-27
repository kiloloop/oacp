#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""Claude PreToolUse hook: enforce the active runtime envelope.

Registered once by ``oacp setup claude`` (static shim). On every Bash / Edit /
Write / NotebookEdit call it looks for ``active_envelope.json`` in the agent
workspace of the project owning the tool call's cwd. No envelope means no
opinion: the hook exits 0 with no output and the harness's normal permission
flow proceeds — exactly today's behavior.

With an envelope active, the hook classifies the call against the compiled
constraints and emits a PreToolUse ``permissionDecision``:

- ``deny``  — the call breaches a declared-false capability, targets a repo
  outside the receiver allowlist, or drifts past ``expected_files_touched``
  (denied with the canonical ``[oacp-envelope] Blocked: autonomy threshold
  exceeded`` opener so the session pivots to the §E checkpoint protocol).
  The file ceiling is the compiled bound widened by any granted §E
  re-authorization: the envelope stays immutable, and the grant recorded in
  the task's audit record is read as an overlay at enforcement time.
- ``ask``   — the hook cannot confidently classify the call (exotic compound
  command, unresolvable repo). The exact command is escalated for
  just-in-time human review instead of blanket-denied or silently allowed.
- allow     — emitted as *no output*, never as an explicit ``allow`` decision,
  so the envelope can only tighten the harness's permission surface, never
  bypass it.

``oacp send`` is never denied: it is the §E notification pipe. The exemption
is exactly that wide — other oacp subcommands are classified. Envelope
self-modification is denied: ``oacp envelope compile`` always, and ``oacp
envelope clear`` until the task's newest audit record shows a terminal
``result.final_state`` — the protocol's documented completion clear is
validated against that record instead of blanket-denied, so an enforced
session can exit its own envelope exactly once the task lifecycle is over.
Determinable Bash write targets (redirects and common writer programs) pass
through the same secret/dependency/file-counter gate as Edit/Write calls.
Protocol bookkeeping surfaces — the receiver's own audit/inbox/outbox
directories and the runtime scratchpad — are the enforcement layer's
instrumentation, not task scope: they never consume the
``expected_files_touched`` budget (the secret and dependency gates still
apply, and containment is judged on resolved paths so symlinks cannot smuggle
task scope under an exempt root). The receiver's ``state/`` directory is the
opposite of exempt: the active envelope lives there, and writing, removing,
or relocating it from inside the session is denied as self-modification.
Trust roots (receiver pins and the project catalog) are authority-bearing
auth config, gated by ``touches_auth_config_or_secrets``.

Every ``ask`` and ``deny`` emitted under an active envelope is also appended
as one JSON line to ``agents/<receiver>/audit/envelope_decisions.jsonl``
(time, task, session, tool, decision, reason), so the escalations a leg
costs can be counted. The ledger is best-effort and never changes a decision.

Ask-branch census for a reply-only research leg (every mutation flag false).
Shapes that are provably reads pass under any profile; the rest keep the
fail-closed ``ask``, each for the reason given:

- ``gh search …`` (a read in every subcommand) and a single-pipeline write
  to ``$TMPDIR/…`` that resolves inside the runtime scratchpad: allowed.
- ``… | xargs <utility> …`` and ``find … -exec <utility> … ;`` / ``+``
  where the nested program is one of the plain read-only utilities the
  degraded fallback trusts: allowed — the program is visible in argv and
  stays a read whatever operands it receives. ``xargs`` with no program
  (its ``echo`` default), a nested shell, ``-ok``/``-okdir``/``-delete``,
  or any other nested program keeps the ask.
- ``sed -i '' …`` (the BSD empty backup suffix): the suffix is no longer
  read as the script, so only the file operands are gated.
- ``oacp autonomy finalize … --validate`` / ``--dry-run``: allowed, they
  persist nothing (the dry run only takes the record lock). Every other
  ``oacp autonomy`` form — a terminal write, a checkpoint, ``outcome`` —
  edits the task's own audit record, the authority this hook reads to
  widen the live envelope: an ask by design, never self-sanctioned.
- ``oacp verify <message> --attach-audit <record>`` where the record is the
  active envelope's own admission record (content identity inside the
  canonical ``audit/autonomy_decisions/`` directory, standalone command,
  selectors matching the envelope) and the message hashes to the record's
  ``message_sha256``: allowed — it is the canonical writer of the receiver's
  verification stamp, the same class as ``oacp autonomy finalize``. Any
  other record, other message bytes, ``--quarantine`` or an abbreviation:
  ask.
- ``command -v NAME`` / ``-V`` (and ``type``, ``which``, ``hash``): allowed —
  a lookup runs nothing. Every other wrapper flag (``env -i …``) still asks.
- ``python3 -c`` whose code parses into the positive read-only grammar
  (listed builtins and modules, read-only attributes, literal read modes on
  every open-like call), behind read-only interpreter options, in a static
  segment that redirects to no file: allowed. Otherwise the ask names the
  first construct outside the grammar (a module, an attribute, a mode, a
  shell expansion inside the code, a redirect, …). ``node -e``, ``perl -e``
  and ``ruby -e`` bodies have no read-only grammar here and ask.
- A deny-listed destructive token inside a literal quoted pattern operand of
  ``grep`` / ``rg`` / ``awk`` / ``sed -n`` (directly or under ``xargs`` /
  ``find -exec``), judged at that operand's own position, is data and no
  longer denies; the token in command position, in an unquoted word, in a
  heredoc body, inside a ``$(…)`` within the quotes, in an awk or sed script
  that can run a command or write a file, or in any other program's operand
  still does.
- ``bash <script> …`` with a literal script path is trusted by pin, never by
  its text. A path the receiver pinned by digest (``envelope.trusted_scripts``
  in its config, carried into the envelope) passes with any arguments while
  the file hashes to its pin, under an env prefix limited to the names the
  pin lists. Any other literal script asks as not registered: the pin is the
  human's review of the script's bytes, so the ask happens once per script
  version, at registration, instead of a parse of the text at every run.
- ``T=$(<path>/<token-helper> <app>) && GH_TOKEN="$T" gh …`` (the App-identity
  recipe): every earlier segment is such a mint and every join is ``&&``, so
  nothing can retarget gh — the final segment is judged as the standalone
  ``gh`` command, under the allow classes and the repository gate. A ``;``,
  a ``cd``, any other assignment or helper keeps the compound ask.
- ``sh -c``, ``eval``, a shell flag, a variable script path, a changed
  pinned file, and wrappers carrying other flags run code the classifier
  cannot see: ask. Run the underlying commands directly, or pin the script.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from autonomy_gate import DESTRUCTIVE_PATTERNS, load_yaml_file
from envelope_compiler import (
    ENVELOPE_FILENAME,
    SESSION_CLAIM_FILENAME,
    envelope_path,
    load_envelope,
    write_envelope,
    write_session_claim,
)

SESSION_CLAIM_STEM = SESSION_CLAIM_FILENAME.rsplit(".", 1)[0]
# One JSON line per non-allow decision under an active envelope: the
# receiver's own ask/deny ledger beside the autonomy records. Never read as
# a record — the audit sweeps glob ``*.yaml``.
DECISION_LOG_FILENAME = "envelope_decisions.jsonl"

REASON_PREFIX = "[oacp-envelope]"
BLOCKED_OPENER = f"{REASON_PREFIX} Blocked: autonomy threshold exceeded"
# Shell expansion syntax the classifier cannot statically resolve: the shell
# expands globs/braces AFTER classification, so a pattern operand can reach a
# protected path while its literal spelling does not. Such operands escalate.
EXPANSION_SYNTAX_RE = re.compile(r"[*?\[\]{}]")
SUBSTITUTION_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
# Every Bash output-redirect spelling that names a file: `>`, `>>`, the
# noclobber override `>|` (the scanner keeps `>|` inside its segment, so
# the `|` here is never a pipe), `<>`, `&>` / `&>>`, and the combined
# stdout+stderr form `>&word`. The same `>&` prefix also spells file
# descriptor duplication and closure (`>&2`, `2>&1`, `>&-`): the word
# after a `dup` match is a file only when it is not digits or `-`.
REDIRECT_TARGET_RE = re.compile(
    r">>?(?:(?P<dup>&)|\|)?\s*(?P<word>[^\s;|&<>]+)"
)
# `<(cmd)` / `>(cmd)`: the inner command runs in its own process and is
# classified as a segment, like `$(cmd)`.
PROCESS_SUBSTITUTION_RE = re.compile(r"[<>]\(([^()]*)\)")
# A short-option cluster ending in GNU `-t` (`-t`, `-rt`, `-vtDIR`): the
# target-directory flag of cp/mv/install. Case-sensitive — `-T` is the
# opposite flag.
SHORT_TARGET_DIRECTORY_RE = re.compile(r"^-[A-Za-z]*t(.*)$")
ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# `<<WORD`, `<<-WORD`, `<<'WORD'`, `<<"WORD"`, `<<\WORD` (not the `<<<`
# here-string). The delimiter word is what ends the body.
HEREDOC_OPERATOR_RE = re.compile(
    r"<<(-?)\s*(?:'([^']+)'|\"([^\"]+)\"|(\\)?([A-Za-z_][A-Za-z0-9_]*))"
)
GIT_URL_RE = re.compile(
    r"(?:[/:])(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)

WRAPPER_COMMANDS = {"command", "builtin", "nohup", "time", "env"}
# Programs that execute arbitrary nested commands the classifier cannot see
# through — always escalated, never allowed through unclassified.
SHELL_INDIRECTION = {
    "sh", "bash", "zsh", "dash", "ksh", "fish", "eval", "exec", "xargs",
    "source", ".",
}
# Shells that run the script file named as their first operand. A literal
# script the receiver pinned by digest passes and any other literal script
# asks as not registered (_classify_script_invocation); a `-c` body, a
# shell flag, a variable path or any other program in SHELL_INDIRECTION
# keeps the ask.
SCRIPT_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
# Where a script shell may be named by path: the system and package-manager
# bin directories (the spelling, after `..` normalization) and the trees
# those resolve into through symlinks (a Homebrew `bin/bash` links into its
# Cellar). A bare name resolves through PATH like every program the hook
# classifies; a path anywhere else — a scratch or repository file that
# merely shares a shell's name, a relative spelling — keeps the ask.
INTERPRETER_BIN_DIRS = (
    "/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/opt/local/bin",
)
INTERPRETER_REAL_PREFIXES = (
    "/bin/", "/usr/", "/opt/homebrew/", "/opt/local/", "/nix/store/",
)
# Bytes of a script the hook reads to hash against its pin; a larger file is
# not a skill wrapper and keeps the ask.
SCRIPT_READ_LIMIT = 1024 * 1024
# Shell syntax words a segment may start with:
# they carry no command of their own, so they are dropped and the words
# after them are judged (_strip_syntax_head).
SCRIPT_SYNTAX_WORDS = {
    "if", "elif", "then", "else", "do", "while", "until", "!", "{", "}",
    "(", ")", "fi", "done", "esac", ";;",
}
# Keywords whose remaining words are data, never a command: the list a loop
# or a case iterates (a substitution inside it is judged as its own
# segment). A `function` line is a definition: its name is dropped and the
# body judged, on the same line or the next ones.
SCRIPT_DATA_KEYWORDS = {"for", "select", "case"}
# Builtins that head a segment without naming a program: a test evaluates
# its operands and runs nothing (a substitution inside it is judged as its
# own segment), so its brackets are not an expanded program name.
TEST_BUILTINS = {"[", "[["}
# A function name is any word free of shell metacharacters (bash allows more
# than identifiers); the glued form carries `()` and whatever follows it in
# one token (`f()`, `f(){`, `f()(`, `f(){cmd`).
FUNCTION_NAME_RE = re.compile(r"[^\s()$`'\"{}|&;<>]+\Z")
GLUED_FUNCTION_HEADER_RE = re.compile(r"[^\s()$`'\"{}|&;<>]+\(\)(.*)\Z")
# One mint of the App-identity recipe: `NAME=$(<path>/<token-helper> <app>)`,
# optionally double-quoted. The helper's basename is fixed; its directory
# is any path free of shell operators and expansion.
MINT_ASSIGNMENT_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)=(\"?)\$\((?:[^\s()$`;|&<>\"']*/)?gh-app-token"
    r"[ \t]+[A-Za-z0-9][A-Za-z0-9_.-]*\)\2$"
)
# Known runners whose nested command is visible in argv: recursively
# classified instead of escalated (F-007: `uv run oacp envelope clear`).
RUNNER_VERBS = {"uv": "run", "poetry": "run", "pipx": "run", "npm": "exec",
                "pnpm": "exec", "yarn": "exec"}
DIRECT_RUNNERS = {"npx", "uvx"}
# Bytes of a message file the attach-audit check hashes; a larger file is
# not a protocol message and keeps the ask (the verifier bounds reads too).
MESSAGE_HASH_LIMIT = 4 * 1024 * 1024
# Interpreter inline-code flags: the code is one argv token, so the hook can
# read it. Python code is admitted through the positive grammar below; node,
# perl and ruby bodies have no read-only grammar here and keep the ask, so no
# inline body reaches the plain-program path unread.
INTERPRETER_CODE_FLAGS = {
    "python": {"-c"}, "python2": {"-c"}, "python3": {"-c"},
    "node": {"-e", "--eval", "-p", "--print"},
    "nodejs": {"-e", "--eval", "-p", "--print"},
    "ruby": {"-e"}, "perl": {"-e", "-E"},
}
INLINE_CODE_FAMILY = {
    "python": "python", "python2": "python", "python3": "python",
    "node": "node", "nodejs": "node", "ruby": "ruby", "perl": "perl",
}
# Short-option clusters that end in the code flag (`python3 -Ic …`,
# `perl -ne …`): the code is the next token. Letters other than the code flag
# are the interpreter's own single-letter options.
INLINE_CODE_CLUSTER_RE = {
    "python": re.compile(r"^-[BbdEIOPqRsSuvx]*c$"),
    "perl": re.compile(r"^-[0aCcdDFiIlMmnpstTuUvwWX]*[eE]$"),
    "ruby": re.compile(r"^-[0aCdEFhiIlnpsSTvwWx]*e$"),
}
# Python options that may precede `-c` and change no file: single-letter
# clusters from this set, `-W <filter>`, and `-X <option>` for the listed
# implementation options (`-X pycache_prefix=…` writes bytecode). Anything
# else — `-i`, an unknown spelling — asks; `-m` or a script path before `-c`
# means the argv runs no inline code.
PY_INLINE_OPTION_RE = re.compile(r"^-[BbdEIOPqRsSuvx]+$")
PY_INLINE_X_OPTION_RE = re.compile(
    r"^(?:utf8|dev|faulthandler|importtime|tracemalloc|warn_default_encoding|"
    r"no_debug_ranges|int_max_str_digits=\d+|frozen_modules=(?:on|off))$"
)
# The positive read-only grammar for `python3 -c` code, judged on its syntax
# tree: every free name is one of these builtins or a name the code binds,
# every import names a module below and one of its listed members (every
# set is explicit: a module that exposed the modules it imports would hand
# out `os` through `glob.os`), every attribute of a module alias is in that
# module's member set,
# every other attribute is in PY_READONLY_ATTRIBUTES, and every call to an
# open-like name carries only a literal read mode. Nothing admitted can run a
# process, import outside the table, reach a dunder, or open a file for
# writing; the ask names the first construct outside the grammar.
PY_READONLY_BUILTINS = frozenset("""
    abs all any ascii bin bool bytes callable chr classmethod dict divmod
    enumerate filter float format frozenset hasattr hash hex id int isinstance
    issubclass iter len list map max min next oct open ord pow print property
    range repr reversed round set slice sorted staticmethod str sum tuple zip
    exit quit __name__
    BaseException Exception ArithmeticError AssertionError AttributeError
    FileNotFoundError IndexError IOError IsADirectoryError KeyError
    KeyboardInterrupt LookupError NameError NotADirectoryError
    NotImplementedError OSError PermissionError RuntimeError StopIteration
    SystemExit TypeError UnicodeDecodeError UnicodeEncodeError UnicodeError
    ValueError ZeroDivisionError
""".split())
PY_READONLY_MODULES: Dict[str, frozenset] = {
    "ast": frozenset("""
        AST And Assert Assign AsyncFor AsyncFunctionDef AsyncWith Attribute
        AugAssign Await BinOp BitAnd BitOr BitXor BoolOp Break Call ClassDef
        Compare Constant Continue Del Delete Dict DictComp Div Eq ExceptHandler
        Expr Expression FloorDiv For FormattedValue FunctionDef GeneratorExp
        Global Gt GtE If IfExp Import ImportFrom In Interactive Invert Is IsNot
        JoinedStr LShift Lambda List ListComp Load Lt LtE MatMult Match Mod
        Module Mult Name NodeTransformer NodeVisitor Nonlocal Not NotEq NotIn
        Or Pass Pow RShift Raise Return Set SetComp Slice Starred Store Sub
        Subscript Try Tuple UAdd USub UnaryOp While With Yield YieldFrom alias
        arg arguments comprehension dump get_docstring iter_child_nodes
        iter_fields keyword literal_eval match_case parse unparse walk withitem
    """.split()),
    "base64": frozenset("""
        a85decode a85encode b16decode b16encode b32decode b32encode b64decode
        b64encode b85decode b85encode decodebytes encodebytes
        standard_b64decode standard_b64encode urlsafe_b64decode
        urlsafe_b64encode
    """.split()),
    "binascii": frozenset("""
        Error a2b_base64 a2b_hex b2a_base64 b2a_hex crc32 hexlify unhexlify
    """.split()),
    "bisect": frozenset("""
        bisect bisect_left bisect_right insort insort_left insort_right
    """.split()),
    "calendar": frozenset("""
        Calendar TextCalendar day_abbr day_name isleap leapdays month
        month_abbr month_name monthrange timegm weekday weekheader
    """.split()),
    "codecs": frozenset({"decode", "encode", "open"}),
    "collections": frozenset("""
        ChainMap Counter OrderedDict defaultdict deque namedtuple
    """.split()),
    "configparser": frozenset({"ConfigParser", "Error", "RawConfigParser"}),
    "copy": frozenset({"Error", "copy", "deepcopy"}),
    "csv": frozenset("""
        DictReader DictWriter Error QUOTE_ALL QUOTE_MINIMAL QUOTE_NONE
        QUOTE_NONNUMERIC Sniffer excel excel_tab field_size_limit
        list_dialects reader unix_dialect writer
    """.split()),
    "dataclasses": frozenset("""
        FrozenInstanceError asdict astuple dataclass field fields
        is_dataclass make_dataclass replace
    """.split()),
    "datetime": frozenset("""
        MAXYEAR MINYEAR UTC date datetime time timedelta timezone tzinfo
    """.split()),
    "decimal": frozenset("""
        Context Decimal DivisionByZero InvalidOperation ROUND_CEILING
        ROUND_DOWN ROUND_FLOOR ROUND_HALF_EVEN ROUND_HALF_UP ROUND_UP
        getcontext localcontext
    """.split()),
    "difflib": frozenset("""
        Differ HtmlDiff SequenceMatcher context_diff get_close_matches ndiff
        restore unified_diff
    """.split()),
    "enum": frozenset({"Enum", "Flag", "IntEnum", "IntFlag", "StrEnum", "auto", "unique"}),
    "fnmatch": frozenset({"filter", "fnmatch", "fnmatchcase", "translate"}),
    "fractions": frozenset({"Fraction"}),
    "functools": frozenset("""
        cache cmp_to_key lru_cache partial reduce total_ordering wraps
    """.split()),
    "glob": frozenset({"escape", "glob", "has_magic", "iglob"}),
    "hashlib": frozenset("""
        algorithms_available algorithms_guaranteed blake2b blake2s
        file_digest md5 new sha1 sha224 sha256 sha384 sha3_256 sha3_512
        sha512
    """.split()),
    "heapq": frozenset("""
        heapify heappop heappush heappushpop heapreplace merge nlargest
        nsmallest
    """.split()),
    "html": frozenset({"escape", "unescape"}),
    "io": frozenset({"BytesIO", "StringIO"}),
    "itertools": frozenset("""
        accumulate batched chain combinations combinations_with_replacement
        compress count cycle dropwhile filterfalse groupby islice pairwise
        permutations product repeat starmap takewhile tee zip_longest
    """.split()),
    "json": frozenset("""
        JSONDecodeError JSONDecoder JSONEncoder dump dumps load loads
    """.split()),
    "math": frozenset("""
        acos asin atan atan2 ceil comb copysign cos cosh degrees dist e exp
        fabs factorial floor fmod frexp fsum gcd hypot inf isclose isfinite
        isinf isnan isqrt lcm ldexp log log10 log2 modf nan perm pi pow prod
        radians sin sinh sqrt tan tanh tau trunc
    """.split()),
    "operator": frozenset({"itemgetter"}),
    "os.path": frozenset("""
        abspath altsep basename commonpath commonprefix curdir devnull dirname
        exists expanduser expandvars extsep getatime getctime getmtime getsize
        isabs isdir isfile islink ismount join lexists normcase normpath
        pardir pathsep realpath relpath samefile sep split splitdrive splitext
    """.split()),
    "pathlib": frozenset("""
        Path PosixPath PurePath PurePosixPath PureWindowsPath
    """.split()),
    "plistlib": frozenset("""
        FMT_BINARY FMT_XML InvalidFileException UID dump dumps load loads
    """.split()),
    "pprint": frozenset("""
        PrettyPrinter isreadable isrecursive pformat pp pprint saferepr
    """.split()),
    "random": frozenset("""
        Random SystemRandom choice choices gauss getrandbits randint random
        randrange sample seed shuffle uniform
    """.split()),
    "re": frozenset("""
        A ASCII DOTALL I IGNORECASE M MULTILINE Match Pattern S U UNICODE
        VERBOSE X compile error escape findall finditer fullmatch match purge
        search split sub subn
    """.split()),
    "secrets": frozenset("""
        SystemRandom choice compare_digest randbelow token_bytes token_hex
        token_urlsafe
    """.split()),
    "shlex": frozenset({"join", "quote", "split"}),
    "statistics": frozenset("""
        NormalDist StatisticsError fmean geometric_mean harmonic_mean mean
        median median_high median_low mode multimode pstdev pvariance
        quantiles stdev variance
    """.split()),
    "string": frozenset("""
        Formatter Template ascii_letters ascii_lowercase ascii_uppercase
        capwords digits hexdigits octdigits printable punctuation whitespace
    """.split()),
    "struct": frozenset("""
        Struct calcsize error iter_unpack pack unpack unpack_from
    """.split()),
    "sys": frozenset("""
        argv byteorder executable exit hexversion maxsize platform stderr
        stdin stdout version version_info
    """.split()),
    "tarfile": frozenset({"is_tarfile", "open"}),
    "textwrap": frozenset({"TextWrapper", "dedent", "fill", "indent", "shorten", "wrap"}),
    "time": frozenset("""
        gmtime localtime mktime monotonic perf_counter sleep strftime strptime
        time time_ns
    """.split()),
    "tomllib": frozenset({"TOMLDecodeError", "load", "loads"}),
    "typing": frozenset("""
        TYPE_CHECKING Any Callable ClassVar Dict Final FrozenSet Generic
        Iterable Iterator List Literal Mapping MutableMapping NamedTuple
        Optional Protocol Sequence Set Tuple TypeVar TypedDict Union cast
        overload
    """.split()),
    "unicodedata": frozenset("""
        bidirectional category combining decimal decomposition digit
        east_asian_width is_normalized lookup mirrored name normalize numeric
        unidata_version
    """.split()),
    "urllib.parse": frozenset("""
        ParseResult SplitResult parse_qs parse_qsl quote quote_plus unquote
        unquote_plus urldefrag urlencode urljoin urlparse urlsplit urlunparse
        urlunsplit
    """.split()),
    "uuid": frozenset("""
        NAMESPACE_DNS NAMESPACE_OID NAMESPACE_URL NAMESPACE_X500 UUID uuid1
        uuid3 uuid4 uuid5
    """.split()),
    "xml.etree.ElementTree": frozenset("""
        Element ElementTree ParseError SubElement XMLParser dump fromstring
        indent iterparse parse tostring
    """.split()),
    "yaml": frozenset("""
        YAMLError dump safe_dump safe_dump_all safe_load safe_load_all
    """.split()),
    "zipfile": frozenset({"ZipFile", "is_zipfile"}),
    "zoneinfo": frozenset({"ZoneInfo", "ZoneInfoNotFoundError", "available_timezones"}),
}
# Attribute names that are reads or in-memory operations on every object the
# modules above can hand back (str, bytes, containers, read handles, paths,
# matches, dates, digests, archive members, syntax nodes). `write`,
# `truncate`, `unlink`, `rename`, `extractall`, `system`, `load`, … are
# absent by construction, so a handle, path or archive opened for reading
# cannot be turned into a write, and an aliased module cannot be reached
# through a container.
PY_READONLY_ATTRIBUTES = frozenset("""
    _asdict _fields _make _replace
    add anchor append appendleft args as_integer_ratio as_posix as_uri
    astimezone bit_length block_size body buffer capitalize casefold center
    clear close closed col_offset colno combine comment compress_type
    conjugate copy count cwd date date_time day days decode denominator
    difference digest digest_size discard doc drive elements encode encoding
    end endswith errno exists expandtabs expanduser extend extractfile
    fieldnames file_size filename filename2 find flags flush format
    format_map from_bytes fromisoformat fromkeys fromtimestamp fullmatch func
    get getinfo getmember getmembers getnames getvalue glob group groupdict
    groups hex hexdigest home hour id imag index infolist insert intersection
    is_absolute is_dir is_file is_integer is_relative_to is_symlink isalnum
    isalpha isatty isdecimal isdigit isdir isdisjoint isfile isidentifier
    islower isnumeric isocalendar isoformat isoweekday isspace issubset
    issuperset istitle isupper items iterdir join joinpath keys keywords
    lastgroup lastindex line_num lineno ljust lower lstat lstrip major match
    maketrans max microsecond micro min minor minute mode month most_common
    msg mtime name namelist now numerator owner parent parents partition
    parts pattern pop popitem popleft pos read read_bytes read_text readable
    readline readlines readlink real relative_to releaselevel remove
    removeprefix removesuffix resolve reverse rfind rglob rindex rjust rotate
    rpartition rsplit rstrip safe_substitute samefile search second seek
    seekable serial setdefault size sort span split splitlines st_atime
    st_ctime st_dev st_gid st_ino st_mode st_mtime st_nlink st_size st_uid
    start startswith stat stem string strftime strip strptime sub subn
    substitute subtract suffix suffixes swapcase symmetric_difference tell
    template testzip time timestamp timetuple title to_bytes toordinal total
    total_seconds translate tzinfo tzname union update upper utc
    utcfromtimestamp utcnow utcoffset value values walk weekday with_name
    with_stem with_suffix writable writeheader writerow writerows year zfill
    attrib dialect findtext getroot iter itertext set tag tail text
""".split())
# Attribute names a `pathlib` object uses to rename or copy a file
# (`Path.replace`; `Path.copy` on 3.14) and that are reads on strings, dates
# and containers: they pass only while `pathlib` is not imported.
PY_PATHLIB_CONFLICTS = frozenset({"copy", "replace"})
# Callables that open a file: admitted only as the callee of a call whose
# mode, positional or `mode=`, is a literal read mode (or absent).
PY_OPEN_LIKE = frozenset({
    "open", "ZipFile", "TarFile", "GzipFile", "BZ2File", "LZMAFile", "FileIO",
    "fdopen",
})
PY_READ_MODE_RE = re.compile(r"^(?:r[bt]?|[bt]r|rU|U|r:(?:\*|gz|bz2|xz)?)$")
PY_PRIVATE_ATTRIBUTES_ALLOWED = frozenset({"_asdict", "_fields", "_make", "_replace"})
# `command -v NAME` / `command -V NAME` (optionally `-p`) look a name up and
# run nothing. Any other wrapper flag keeps the ask (F-002).
COMMAND_LOOKUP_FLAG_RE = re.compile(r"^-[pvV]+$")
# Programs whose quoted operands are patterns or scripts the program matches
# against its input: a deny-listed destructive token quoted there is data,
# not a command. awk and sed scripts can run commands or write files, so their
# operands count only when the script is extracted from argv and proves inert:
# an awk program with no redirect, pipe, `system`, `getline` or `@` directive;
# a sed script under `-n` built only from addresses and print-class commands.
PATTERN_OPERAND_PROGRAMS = {"grep", "egrep", "fgrep", "rg", "awk", "gawk", "mawk", "nawk", "sed"}
AWK_SCRIPT_ESCAPE_RE = re.compile(r"\bsystem\s*\(|\bgetline\b|[|>@]")
AWK_VALUE_OPTIONS = {"-F", "-v", "-W", "--field-separator", "--assign"}
AWK_PLAIN_OPTIONS = {
    "--posix", "--traditional", "-c", "-C", "--copyright", "-O", "--optimize",
    "-S", "--sandbox", "-n", "--non-decimal-data", "-N", "--use-lc-numeric",
    "-b", "--characters-as-bytes", "-M", "--bignum", "-t", "--lint-old",
    "-r", "--re-interval", "-L", "--lint",
}
SED_PLAIN_OPTIONS = {
    "-E", "-r", "--regexp-extended", "-s", "--separate", "-u", "--unbuffered",
    "-z", "--null-data", "--posix", "--debug", "--sandbox",
}
SED_CLUSTER_LETTERS = frozenset("nErsuz")
# sed commands that print, delete or shuffle the pattern/hold space and touch
# no file: p = l d h H g G x n N q Q z F. `s` and `y` are parsed with their
# delimited fields; `w`, `W`, `e`, `r`, `R`, `a`, `i`, `c`, branches and
# labels are outside the grammar.
SED_SIMPLE_COMMANDS = frozenset("p=ldhHgGxnNqQzF")
# A `$` that the shell would expand inside double quotes, or a backtick. A
# trailing `$`, `$)` or `$"` is literal (a regex anchor), so only these spellings
# disqualify a double-quoted operand from being pattern data.
LIVE_EXPANSION_RE = re.compile(r"\$[A-Za-z_{(@*#?!$0-9-]|`")
# xargs options whose value is the NEXT argv token; attached spellings
# (`-I{}`, `-n1`, `--replace={}`) are single tokens and skip naturally.
# `-e`, `-i` and `-l` take only attached values in GNU xargs.
XARGS_VALUE_OPTIONS = {
    "-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s",
    "--arg-file", "--delimiter", "--eof", "--replace", "--max-lines",
    "--max-args", "--max-procs", "--max-chars", "--process-slot-var",
}
PROTECTED_BRANCHES = {"main", "master"}
# git push flags that can update refs far beyond a single PR branch.
GIT_PUSH_BULK_FLAGS = {
    "--mirror", "--all", "--tags", "--follow-tags", "--delete", "-d", "--prune",
}
# oacp subcommands an enveloped session may always run (the send pipe plus
# read-only surfaces). `envelope compile|--extend` is self-modification of
# the active constraints and is denied; `envelope clear` is the documented
# completion step, sanctioned only by a terminal audit record (see
# _classify_envelope_clear); everything else escalates.
OACP_ALWAYS_ALLOWED = {"send", "inbox", "validate", "doctor", "watch", "help", ""}
# `result.final_state` values that mark the task lifecycle over. `pending`
# (the admission birth state) and `paused` are deliberately absent: an
# admitted task keeps its envelope until finalization, and a
# checkpoint-paused task re-authorizes via the §E flow (recompile with
# --extend), never via clear. `done` counts only with its `completed_at_utc`
# stamp — the finalizer always writes both, and a stamp-less `done` is a
# pre-0.5.2 auto-accepted birth that was never finalized.
TERMINAL_AUDIT_STATES = {"done", "error", "cancelled"}
# Receiver-workspace directories that are protocol bookkeeping, not task
# scope: audit records, inbox claims, outbox copies. Deliberately NOT the
# receiver's state/ (the active envelope is the policy object — direct
# writes are self-modification and deny) nor trust roots (authority-bearing
# auth config, gated by touches_auth_config_or_secrets). The receiver's
# config.yaml sits above all of these and stays fully gated.
BOOKKEEPING_AGENT_SUBDIRS = ("audit", "inbox", "outbox")
# Runtime scratchpad roots (reply/body-file composition). Deliberately
# narrower than the OS temp root: exempting all of /tmp would let arbitrary
# staging escape the counter, and would swallow test fixtures on CI.
SCRATCHPAD_PREFIXES = (
    "/tmp/claude-",
    "/private/tmp/claude-",
    "/tmp/claude/",
    "/private/tmp/claude/",
)
# The harness runs each sandboxed Bash call with TMPDIR set to its runtime
# scratchpad and exports that directory to hooks under this name. The hook
# process's own TMPDIR is the login temp dir and is never consulted.
SANDBOX_TMPDIR_ENV_VAR = "CLAUDE_TMPDIR"
SANDBOX_TMPDIR_TARGET_RE = re.compile(r"^\$(?:\{TMPDIR\}|TMPDIR)(?P<rest>/.*)?$")
# One expansion the shell performs that is exactly the TMPDIR value: no
# operator, modifier or subscript, and not the prefix of a longer name.
SANDBOX_TMPDIR_REFERENCE_RE = re.compile(r"\$(?:\{TMPDIR\}|TMPDIR(?![A-Za-z0-9_\[]))")
# Descriptor duplications (`2>&1`, `>&2`, `<&-`, `&>file`) are redirects,
# not the `&` list and background operators.
REDIRECT_DUPLICATION_RE = re.compile(r"\d*[<>]&(?:\d+|-)?|&>>?")

DEPENDENCY_FILENAMES = {
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "uv.lock",
    "poetry.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Gemfile",
    "Gemfile.lock",
}
PACKAGE_MANAGERS = {
    "pip",
    "pip3",
    "uv",
    "npm",
    "yarn",
    "pnpm",
    "brew",
    "poetry",
    "cargo",
    "gem",
    "apt",
    "apt-get",
}
DEPENDENCY_VERBS = {"install", "add", "remove", "uninstall"}

GH_READONLY_ACTIONS = {"view", "list", "status", "checks", "diff", "download"}
# Groups whose every subcommand is a read: `gh search {repos,issues,prs,
# commits,code}` queries the search API and mutates nothing, so the group
# passes whole instead of falling through to the fail-closed escalation.
GH_READONLY_GROUPS = {"search"}
GH_PR_CLASS = {("pr", "create"), ("pr", "edit"), ("pr", "ready"), ("pr", "update-branch")}
GH_COMMENT_CLASS = {("pr", "comment"), ("pr", "review"), ("issue", "comment")}
# Constraint-gated classes for the granular capabilities beyond PR artifacts
# and comments. `merges_pr` is exactly the merge; `files_issues` is issue
# lifecycle plus `label create` (issue-adjacent metadata — labels must be
# creatable before they can be applied). Everything else in the groups stays
# denied below. These are consulted BEFORE the deny classes: a declared-true
# capability wins over the group-level deny, a declared-false one denies with
# the constraint named.
GH_MERGE_CLASS = {("pr", "merge")}
GH_ISSUE_CLASS = {("issue", "create"), ("issue", "edit"), ("issue", "close"), ("label", "create")}
GH_DENY_CLASS = {
    ("pr", "close"),
    ("pr", "reopen"),
    ("pr", "lock"),
    ("pr", "unlock"),
    ("issue", "reopen"),
    ("issue", "delete"),
    ("issue", "transfer"),
    ("issue", "pin"),
    ("issue", "unpin"),
    ("issue", "lock"),
    ("issue", "unlock"),
}
GH_DENY_GROUPS = {"release", "repo", "gist", "secret", "variable", "label", "ruleset"}
GH_AUTH_MUTATIONS = {"login", "logout", "refresh", "setup-git", "token"}
GH_WORKFLOW_MUTATIONS = {
    ("workflow", "run"),
    ("workflow", "enable"),
    ("workflow", "disable"),
    ("run", "rerun"),
    ("run", "cancel"),
    ("cache", "delete"),
}
# Value-taking gh flags: skip flag AND value when deriving group/action, so a
# flag's value can never be misread as a positional (F-003: `gh --repo X pr
# merge` must classify as pr/merge, not X/pr). Unknown flags skip only
# themselves; if that misparses an exotic value-taking flag, the resulting
# unknown group/action falls into the fail-closed ask below.
GH_VALUE_FLAGS = {
    "-R", "--repo", "-X", "--method", "-H", "--header", "-q", "--jq",
    "-t", "--template", "-b", "--body", "--body-file", "-B", "--base",
    "--head", "--title", "-m", "--milestone", "-a", "--assignee",
    "-l", "--label", "-p", "--project", "--hostname", "-A", "--app",
}
# gh api flags that implicitly switch the request from GET to POST.
GH_API_MUTATION_FLAGS = {"-f", "--field", "-F", "--raw-field", "--input"}
# Bash writer programs whose file targets are determinable from argv.
BASH_WRITERS = {"touch", "tee", "cp", "mv", "install", "truncate"}
# Filesystem mutators whose OPERANDS (sources and destinations alike) must
# honor the state/trust protection: deleting, relocating, copying out, or
# aliasing protected material is as much a mutation as writing it.
FS_MUTATORS = {"rm", "unlink", "mv", "cp", "install", "truncate", "shred", "ln"}


class Decision:
    __slots__ = ("action", "reason", "new_files")

    def __init__(
        self,
        action: str,
        reason: str = "",
        new_files: Optional[List[str]] = None,
    ) -> None:
        self.action = action
        self.reason = reason
        self.new_files = new_files or []


ALLOW = Decision("allow")


def _deny(reason: str) -> Decision:
    return Decision("deny", _prefixed_reason(reason))


def _ask(reason: str) -> Decision:
    return Decision("ask", _prefixed_reason(reason))


def _prefixed_reason(reason: str) -> str:
    """Return one operator-visible reason with the stable OACP tag."""
    if reason.startswith(REASON_PREFIX):
        return reason
    return f"{REASON_PREFIX} {reason}"


def _with_message_id(decision: Decision, message_id: str) -> Decision:
    """Add the active task id to a non-allow decision when it is known."""
    if decision.action == "allow" or not message_id:
        return decision
    return Decision(
        decision.action,
        f"{_prefixed_reason(decision.reason)} [task {message_id}]",
        decision.new_files,
    )


class WorkspaceContext:
    """Workspace facts resolved by ``process()`` that classification needs
    beyond the envelope's own constraints: where the receiver's bookkeeping
    surfaces live, and which audit records can sanction a completion clear or
    widen the file budget. ``None`` (no resolved workspace, e.g. direct
    ``classify`` calls) keeps every context-dependent decision fail-closed."""

    __slots__ = ("oacp_root", "project", "receiver", "message_id", "message_sha256")

    def __init__(
        self,
        oacp_root: Path,
        project: str,
        receiver: str,
        message_id: str,
        message_sha256: str = "",
    ) -> None:
        self.oacp_root = oacp_root
        self.project = project
        self.receiver = receiver
        self.message_id = message_id
        self.message_sha256 = message_sha256

    def agent_dir(self) -> Path:
        return (
            self.oacp_root / "projects" / self.project / "agents" / self.receiver
        )

    def audit_dir(self) -> Path:
        return self.agent_dir() / "audit" / "autonomy_decisions"

    def state_dir(self) -> Path:
        return self.agent_dir() / "state"

    def decision_log_path(self) -> Path:
        return self.agent_dir() / "audit" / DECISION_LOG_FILENAME

    def authority_roots(self) -> List[Tuple[str, Path]]:
        """Receiver surfaces the enveloped session may never modify itself.

        Both enforcement sites — the write-target gate and the role-agnostic
        filesystem-mutator operand gate — consume this one list, so the two
        boundaries cannot drift apart. ``state/`` holds the active envelope;
        ``audit/autonomy_decisions/`` holds the records whose recorded
        re-authorization can widen that envelope's live bound. Modifying
        either from inside the session is self-authorization, and that is
        true regardless of operand role: deleting or relocating a record
        changes which record governs just as surely as rewriting one.
        """
        return [
            ("envelope state", self.state_dir()),
            ("autonomy audit record", self.audit_dir()),
        ]

    def trust_roots(self) -> List[Path]:
        return [
            self.agent_dir() / "trust",
            self.oacp_root / "projects" / self.project / "trust",
        ]

    def bookkeeping_prefixes(self) -> List[str]:
        agent_dir = self.agent_dir()
        return [str(agent_dir / name) for name in BOOKKEEPING_AGENT_SUBDIRS]


def _record_decision(
    context: WorkspaceContext,
    decision: Decision,
    payload: Dict[str, Any],
    session_id: str,
) -> None:
    """Append one JSON line per non-allow decision to the receiver's ledger.

    Without it an approved ``ask`` leaves no trace anywhere — not in the
    transcript, the audit record, or any log — so the escalations a leg
    costs cannot be counted. The line carries the time, the active task,
    the harness session, the tool, the decision and the operator-visible
    reason (which already quotes the offending segment where one exists).
    Best effort: a ledger that cannot be written never changes the outcome.
    """
    if decision.action == "allow":
        return
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "message_id": context.message_id or None,
        "session_id": session_id or None,
        "tool": str(payload.get("tool_name") or ""),
        "decision": decision.action,
        "reason": decision.reason,
    }
    try:
        path = context.decision_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass

# ── Workspace discovery ───────────────────────────────────────────────────────


def find_project(cwd: Path) -> Optional[str]:
    """Walk upward from cwd looking for an OACP workspace marker."""
    for root in (cwd, *cwd.parents):
        for name in (".oacp", "workspace.json"):
            marker = root / name
            if not (marker.is_file() or marker.is_symlink()):
                continue
            try:
                data = json.loads(marker.resolve().read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("project_name"):
                return str(data["project_name"])
    return None


# ── Path classification ───────────────────────────────────────────────────────


def is_secret_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    if name == ".env" or name.startswith(".env."):
        return True
    if "credentials" in name.lower():
        return True
    if name.endswith((".pem", ".key")):
        return True
    if "/.ssh/" in normalized or normalized.startswith((".ssh/", "~/.ssh/")):
        return True
    parts = [part for part in normalized.split("/") if part]
    for index in range(len(parts) - 2):
        if parts[index] == "agents" and parts[index + 2] in ("config.yaml", "config.yml"):
            return True
    return False


def is_dependency_path(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    if name in DEPENDENCY_FILENAMES:
        return True
    return name.startswith("requirements") and name.endswith(".txt")


_TOML_TABLE_RE = re.compile(
    r"^[ \t]*\[(?P<name>[^\[\]\r\n]+)\][ \t]*(?:#.*)?(?:\r?\n)?$"
)
_PROJECT_VERSION_RE = re.compile(
    r"^(?P<prefix>[ \t]*version[ \t]*=[ \t]*)"
    r"(?P<literal>\"(?:\\.|[^\"\\])*\"|'[^']*')"
    r"(?P<suffix>[ \t]*(?:#.*)?)(?P<newline>\r?\n)?$"
)


def _canonicalize_project_version(document: str) -> Optional[Tuple[str, str]]:
    """Replace the sole ``[project] version`` literal with a sentinel.

    This is deliberately narrower than a general TOML parser: the exemption
    must prove that every byte outside one ordinary version-string literal is
    unchanged. Unsupported or ambiguous shapes fail closed and keep the
    dependency-manifest denial.
    """
    lines = document.splitlines(keepends=True)
    current_table: Optional[str] = None
    match_index: Optional[int] = None
    version_literal: Optional[str] = None
    canonical_line: Optional[str] = None

    for index, line in enumerate(lines):
        table = _TOML_TABLE_RE.fullmatch(line)
        if table:
            current_table = table.group("name").strip()
            continue
        if line.lstrip().startswith("["):
            current_table = None
            continue
        if current_table != "project":
            continue
        version = _PROJECT_VERSION_RE.fullmatch(line)
        if version is None:
            continue
        if match_index is not None:
            return None
        match_index = index
        version_literal = version.group("literal")
        canonical_line = (
            f"{version.group('prefix')}\"<oacp-project-version>\""
            f"{version.group('suffix')}{version.group('newline') or ''}"
        )

    if match_index is None or version_literal is None or canonical_line is None:
        return None
    lines[match_index] = canonical_line
    return "".join(lines), version_literal


def _preview_file_tool_change(
    tool_name: str,
    tool_input: Dict[str, Any],
    normalized_path: str,
) -> Optional[Tuple[str, str]]:
    """Reconstruct a file tool's pre/post documents without writing them."""
    try:
        before = Path(normalized_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None

    if tool_name == "Write":
        content = tool_input.get("content")
        if not isinstance(content, str):
            return None
        return before, content
    if tool_name != "Edit":
        return None

    old = tool_input.get("old_string")
    new = tool_input.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or not old:
        return None
    occurrences = before.count(old)
    if occurrences == 0:
        return None
    replace_all = tool_input.get("replace_all") is True
    if not replace_all and occurrences != 1:
        return None
    after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
    return before, after


def _is_version_only_pyproject_edit(
    tool_name: str,
    tool_input: Dict[str, Any],
    normalized_path: str,
) -> bool:
    """True only when an Edit/Write changes one project-version literal."""
    if Path(normalized_path).name != "pyproject.toml":
        return False
    preview = _preview_file_tool_change(tool_name, tool_input, normalized_path)
    if preview is None:
        return False
    before, after = preview
    before_version = _canonicalize_project_version(before)
    after_version = _canonicalize_project_version(after)
    if before_version is None or after_version is None:
        return False
    before_canonical, before_literal = before_version
    after_canonical, after_literal = after_version
    return (
        before_literal != after_literal
        and before_canonical == after_canonical
    )


def _normalize_file_path(path: str, cwd: str) -> str:
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    return os.path.normpath(expanded)


def _within(canonical: str, root: str) -> bool:
    return canonical == root or canonical.startswith(root + os.sep)


def sandbox_tmpdir_is_unmodified(command: str, stripped: str) -> bool:
    """Whether every ``$TMPDIR`` the shell expands in ``command`` is the call's own.

    ``stripped`` is ``command`` with its heredoc bodies removed. The
    resolution trusts the TMPDIR the call starts with, so the command must
    be unable to change it before a write target is expanded, and each
    ``$TMPDIR`` left after quote removal must be one the shell expands.
    Both hold for a single pipeline of simple commands whose only expansions
    are ``$TMPDIR``/``${TMPDIR}`` references outside single quotes and
    escapes. Within one simple command, words and redirects are expanded
    before any builtin assignment runs, and a pipeline stage never sees an
    assignment made by another stage. A list (``;``, ``&&``, ``||``, ``&``,
    newline) is refused because a later command sees assignments made
    earlier, including by the last pipeline stage, which zsh runs in the
    current shell. Grouping and brace syntax, backticks, any other mention
    of TMPDIR, and expanding heredoc bodies are refused too: each can
    construct or assign the name before the target is expanded.
    """
    if "`" in stripped:
        return False
    for match in HEREDOC_OPERATOR_RE.finditer(command):
        if match.group(5) is not None and match.group(4) is None:
            return False
    view = SANDBOX_TMPDIR_REFERENCE_RE.sub(
        lambda found: " " * len(found.group(0)), _shell_syntax_view(stripped)
    )
    view = REDIRECT_DUPLICATION_RE.sub(" ", view.strip().replace("|&", "|"))
    if re.search(r"[;&\n(){}]|\|\|", view):
        return False
    references = 0
    quote: Optional[str] = None
    index = 0
    while index < len(stripped):
        char = stripped[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "$":
            match = SANDBOX_TMPDIR_REFERENCE_RE.match(stripped, index)
            if match is None:
                return False
            references += 1
            index = match.end()
            continue
        if char == '"':
            quote = None if quote == '"' else '"'
        elif char == "'" and quote is None:
            quote = "'"
        index += 1
    return stripped.count("TMPDIR") == references


def resolve_sandbox_tmpdir_target(target: str, sandboxed: bool = True) -> Optional[str]:
    """Literal scratchpad path for a ``$TMPDIR/...`` Bash write target, or None.

    The harness mandates ``$TMPDIR`` for temporary files and, for a sandboxed
    call, expands it to the runtime scratchpad — a spelling the variable gate
    would otherwise escalate. The hook cannot see the call's shell state, but
    the harness names that directory in ``CLAUDE_TMPDIR``, so the target
    resolves when every condition holds: the call stays sandboxed (no per-call
    opt-out) and its command cannot have changed TMPDIR (the caller checks
    ``sandbox_tmpdir_is_unmodified``), ``$TMPDIR`` or ``${TMPDIR}`` is the
    whole first component, the remainder carries no further variable,
    substitution or expansion syntax and no ``..`` component, the named
    directory lies under a scratchpad prefix, and the path, with its
    symlinks resolved, stays inside it. Anything else returns None and keeps the
    existing escalation. A session whose sandbox is disabled outright still
    exports the variable, so the per-call opt-out is the only sandbox signal
    available here.
    """
    if not sandboxed:
        return None
    match = SANDBOX_TMPDIR_TARGET_RE.match(target)
    if match is None:
        return None
    rest = match.group("rest") or ""
    if "$" in rest or "`" in rest or EXPANSION_SYNTAX_RE.search(rest):
        return None
    # A link another pipeline stage creates after classification would
    # change where `..` lands, so a parent component is never resolved.
    if ".." in rest.split("/"):
        return None
    root = os.environ.get(SANDBOX_TMPDIR_ENV_VAR) or ""
    if not os.path.isabs(root):
        return None
    canonical_root = os.path.realpath(root)
    if not canonical_root.startswith(SCRATCHPAD_PREFIXES):
        return None
    resolved = os.path.realpath(canonical_root + rest)
    if not _within(resolved, canonical_root):
        return None
    return resolved


def is_bookkeeping_path(
    normalized: str, context: Optional[WorkspaceContext]
) -> bool:
    """True for paths that are enforcement instrumentation, not task scope.

    Covers the runtime scratchpad (reply/body-file composition) and the
    receiver's own workspace bookkeeping directories. Exempt paths never
    consume ``expected_files_touched``; the secret and dependency gates run
    before this predicate and still apply to them. Containment is checked on
    the resolved filesystem target (realpath on both sides), so a symlink
    planted under an exempt root that points into ordinary task scope stays
    counted.
    """
    canonical = os.path.realpath(normalized)
    if canonical.startswith(SCRATCHPAD_PREFIXES):
        return True
    if context is None:
        return False
    for prefix in context.bookkeeping_prefixes():
        if _within(canonical, os.path.realpath(prefix)):
            return True
    return False


# ── Repo resolution ───────────────────────────────────────────────────────────


def parse_repo_from_url(url: str) -> Optional[str]:
    match = GIT_URL_RE.search(url.strip())
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def resolve_cwd_repo(git_dir: str, remote: str = "origin") -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", git_dir, "remote", "get-url", remote],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return parse_repo_from_url(completed.stdout)


def resolve_current_branch(git_dir: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", git_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _repo_gate(
    repo: Optional[str],
    constraints: Dict[str, Any],
    action_label: str,
) -> Optional[Decision]:
    """Shared allowlist/visibility gate for gh and git mutations."""
    if constraints.get("public_visibility"):
        return _deny(
            f"envelope declares public_visibility: true is not allowed at "
            f"runtime; {action_label} paused for review"
        )
    if repo is None:
        return _ask(
            f"cannot resolve the target repository for {action_label}; "
            "escalating for review"
        )
    target = str(constraints.get("target_repo") or "")
    allowlist = constraints.get("private_repo_allowlist") or []
    if target and repo != target:
        return _deny(
            f"{action_label} targets {repo}, but the envelope pins "
            f"target_repo {target}"
        )
    if repo not in allowlist:
        return _deny(
            f"{action_label} targets {repo}, which is outside the receiver's "
            "private_repo_allowlist"
        )
    return None


# ── Bash classification ───────────────────────────────────────────────────────


def _strip_wrappers(tokens: List[str]) -> Tuple[List[str], bool, List[str]]:
    """Return (remaining tokens, needs_ask, stripped env assignments).

    needs_ask is True when a wrapper carries its own flags (`env -i gh ...`):
    flag/value consumption differs per wrapper, so the real program cannot be
    identified reliably — escalate instead of guessing (F-002) — and when a
    wrapper is spelled by a path outside the system bin directories
    (`/tmp/env gh …`): an executable that merely shares a wrapper's name
    (_trusted_interpreter). A wrapper spelled bare or by its system path
    (`/usr/bin/env gh …`) is dropped like any other. The stripped
    assignments are preserved because they change the wrapped program's
    behavior (`OACP_HOME=… oacp envelope clear` retargets the clear) and the
    classifier must be able to see that.
    """
    index = 0
    saw_wrapper = False
    assignments: List[str] = []
    while index < len(tokens):
        token = tokens[index]
        if ENV_ASSIGNMENT_RE.match(token):
            assignments.append(token)
            index += 1
            continue
        if token.rsplit("/", 1)[-1] in WRAPPER_COMMANDS:
            if not _trusted_interpreter(token):
                return tokens[index:], True, assignments
            if token == "command" and _is_command_lookup(tokens[index:]):
                # `command -v NAME` is a lookup, not a wrapper around NAME:
                # it runs nothing. Hand it on as its own program.
                return tokens[index:], False, assignments
            saw_wrapper = True
            index += 1
            continue
        if saw_wrapper and token.startswith("-"):
            return tokens[index:], True, assignments
        break
    return tokens[index:], False, assignments


def _is_command_lookup(tokens: List[str]) -> bool:
    """``command -v NAME…`` / ``command -V NAME…`` (``-p`` may ride along).

    The lookup prints where a name resolves and executes nothing, whatever
    the operands. ``command -p NAME`` without ``-v``/``-V`` runs NAME under
    the default PATH and stays a wrapper carrying a flag (ask).
    """
    if len(tokens) < 2 or tokens[0] != "command":
        return False
    flag = tokens[1]
    return bool(COMMAND_LOOKUP_FLAG_RE.match(flag)) and ("v" in flag or "V" in flag)


def _flag_value(tokens: List[str], *flags: str) -> Optional[str]:
    for index, token in enumerate(tokens):
        if token in flags and index + 1 < len(tokens):
            return tokens[index + 1]
        for flag in flags:
            if token.startswith(flag + "="):
                return token.split("=", 1)[1]
            # Attached short-flag value: `-Rowner/repo` (F-006).
            if (
                not flag.startswith("--")
                and len(flag) == 2
                and len(token) > 2
                and token.startswith(flag)
            ):
                return token[2:]
    return None


GH_URL_REPO_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?].*)?$",
    re.IGNORECASE,
)


def _gh_repo_selectors(
    tokens: List[str],
    positionals: List[str],
) -> Tuple[List[str], Optional[str]]:
    """Every explicit repository selector on a gh mutation command.

    gh lets a positional URL (`gh pr merge https://github.com/o/r/pull/7`)
    or a later `-R/--repo` occurrence retarget the mutation away from the
    first flag value and the cwd repo, so the repo gate must judge the same
    repository gh will actually mutate: every flag occurrence and every
    URL-shaped positional is collected, and the caller escalates unless
    they agree on a single repository. Returns (selectors, unresolved)
    where unresolved carries a URL-shaped positional whose repository
    could not be parsed (e.g. a non-github host).
    """
    selectors: List[str] = []
    unresolved: Optional[str] = None
    for index, token in enumerate(tokens):
        if token in ("-R", "--repo"):
            if index + 1 < len(tokens):
                selectors.append(tokens[index + 1])
            continue
        if token.startswith("--repo="):
            selectors.append(token.split("=", 1)[1])
            continue
        if token.startswith("-R") and not token.startswith("--") and len(token) > 2:
            selectors.append(token[2:])
            continue
    for word in positionals:
        url_shaped = "://" in word or GH_URL_REPO_RE.match(word)
        if not url_shaped:
            continue
        match = GH_URL_REPO_RE.match(word)
        if match:
            selectors.append(f"{match.group(1)}/{match.group(2)}")
        else:
            unresolved = word
    return selectors, unresolved


def _last_flag_value(tokens: List[str], flag: str) -> Optional[str]:
    """Last-occurrence flag value — argparse semantics.

    The completion-clear validation must judge the same effective value the
    CLI will use; first-occurrence parsing lets a duplicated flag validate
    one target and clear another.
    """
    value: Optional[str] = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == flag and index + 1 < len(tokens):
            value = tokens[index + 1]
            index += 2
            continue
        if token.startswith(flag + "="):
            value = token.split("=", 1)[1]
        index += 1
    return value


def _git_subcommand(tokens: List[str]) -> Tuple[Optional[str], List[str], Optional[str]]:
    """Return (subcommand, args-after-subcommand, -C directory)."""
    git_dir: Optional[str] = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "-C" and index + 1 < len(tokens):
            git_dir = tokens[index + 1]
            index += 2
            continue
        if token == "-c" and index + 1 < len(tokens):
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, tokens[index + 1:], git_dir
    return None, [], git_dir


def _classify_git_push(
    args: List[str],
    git_dir: str,
    constraints: Dict[str, Any],
) -> Decision:
    if not constraints.get("external_side_effects"):
        return _deny(
            "git push is an external side effect the envelope declares false"
        )
    if not constraints.get("creates_or_updates_pr"):
        return _deny(
            "git push requires creates_or_updates_pr in the envelope "
            "(branch pushes are folded into PR-artifact creation)"
        )

    bulk = sorted(set(args) & GIT_PUSH_BULK_FLAGS)
    if bulk:
        return _deny(
            f"git push {bulk[0]} can update refs beyond a single PR branch "
            "and is never inside a runtime envelope"
        )

    positional = [arg for arg in args if not arg.startswith("-")]
    remote = positional[0] if positional else "origin"
    refspecs = positional[1:]
    needs_current_branch = not refspecs
    for refspec in refspecs:
        # Wildcard refspecs can update many branches at once, including
        # protected ones (F-006: `refs/heads/*:refs/heads/*`).
        if "*" in refspec:
            return _deny(
                f"git push with wildcard refspec {refspec!r} can update "
                "multiple branches and is never inside a runtime envelope"
            )
        # `+refspec` is a per-refspec force marker — strip it before parsing
        # so `+main` is still recognized as a protected branch (F-003).
        destination = refspec.lstrip("+").split(":", 1)[-1]
        branch = destination.rsplit("/", 1)[-1]
        if branch == "HEAD":
            needs_current_branch = True
            continue
        if branch in PROTECTED_BRANCHES:
            return _deny(
                f"git push targeting protected branch {branch!r} is never "
                "inside a runtime envelope"
            )
    if needs_current_branch:
        branch = resolve_current_branch(git_dir)
        if branch is None:
            return _ask("cannot resolve the branch for a bare git push")
        if branch in PROTECTED_BRANCHES:
            return _deny(
                f"git push from protected branch {branch!r} is never inside "
                "a runtime envelope"
            )

    repo = resolve_cwd_repo(git_dir, remote)
    gate = _repo_gate(repo, constraints, "git push")
    if gate is not None:
        return gate
    return ALLOW


def _gh_positionals(tokens: List[str]) -> Tuple[List[str], bool]:
    """Extract gh positionals with flag/value pairs consumed (F-003).

    Returns (positionals, api_mutation_flags): value-taking flags skip their
    value so it can never be misread as group/action; `-f`/`--field`-style
    flags are recorded because they implicitly switch `gh api` to POST.
    """
    words: List[str] = []
    api_mutation = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in GH_API_MUTATION_FLAGS:
            api_mutation = True
            index += 2
            continue
        if token in GH_VALUE_FLAGS:
            index += 2
            continue
        if token.startswith("--"):
            if "=" in token and token.split("=", 1)[0] in GH_API_MUTATION_FLAGS:
                api_mutation = True
            index += 1
            continue
        if token.startswith("-") and len(token) > 2:
            # Attached short-flag value (`-Rowner/repo`, `-fkey=val`): the
            # whole token is flag+value — never a positional (F-006).
            if token[:2] in GH_API_MUTATION_FLAGS:
                api_mutation = True
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        words.append(token)
        index += 1
    return words, api_mutation


def _gh_env_repo_selector(
    env_assignments: List[str],
    ambient: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Repository selector carried by the effective GH_REPO / GH_HOST.

    `gh help environment`: GH_REPO selects the repository for commands
    that would otherwise use the local one, and GH_HOST retargets the
    hostname entirely — both retarget a mutation after the cwd-based
    gate approved it. The effective environment is what the Bash child
    inherits: ambient hook-process values seeded first, inline
    assignments applied over them with shell precedence. Returns
    (selector, escalate_reason); any effective GH_HOST, or a GH_REPO
    value that does not resolve to a github.com owner/repo, cannot be
    validated here and escalates.
    """
    if ambient is None:
        ambient = {
            key: os.environ[key]
            for key in ("GH_REPO", "GH_HOST")
            if key in os.environ
        }
    effective: Dict[str, str] = dict(ambient)
    origin = {key: "ambient" for key in effective}
    for assignment in env_assignments:
        name, _, value = assignment.partition("=")
        if name in ("GH_REPO", "GH_HOST"):
            effective[name] = value
            origin[name] = "inline"
    if "GH_HOST" in effective:
        return None, (
            f"an {origin['GH_HOST']} GH_HOST value "
            f"{effective['GH_HOST']!r}"
        )
    value = effective.get("GH_REPO")
    if value is None:
        return None, None
    # GH_REPO accepts [HOST/]OWNER/REPO.
    parts = value.split("/")
    if len(parts) == 2 and all(parts):
        return value, None
    if len(parts) == 3 and parts[0].lower() in ("github.com", "www.github.com"):
        return f"{parts[1]}/{parts[2]}", None
    return None, f"an {origin['GH_REPO']} GH_REPO value {value!r}"


def _classify_gh(
    tokens: List[str],
    cwd: str,
    constraints: Dict[str, Any],
    *,
    env_assignments: Optional[List[str]] = None,
    standalone: bool = False,
) -> Decision:
    words, api_mutation = _gh_positionals(tokens)
    group = words[0] if words else ""
    action = words[1] if len(words) > 1 else ""
    label = f"gh {group} {action}".strip()

    if group == "api":
        explicit = _flag_value(tokens, "-X", "--method")
        method = (explicit or ("POST" if api_mutation else "GET")).upper()
        if method == "GET":
            return ALLOW
        return _ask(f"gh api with method {method} cannot be classified")

    if group == "auth":
        # Allowlist, not blocklist: `status` is the only read-only auth verb
        # (F-006 — `gh auth switch` mutates authentication configuration).
        if action == "status":
            return ALLOW
        if not constraints.get("touches_auth_config_or_secrets"):
            return _deny(
                f"{label} mutates auth state the envelope declares false"
            )
        return ALLOW

    if group in GH_READONLY_GROUPS or action in GH_READONLY_ACTIONS:
        return ALLOW

    key = (group, action)
    constraint_classes = (
        (GH_PR_CLASS, "creates_or_updates_pr"),
        (GH_COMMENT_CLASS, "comments_on_github"),
        (GH_MERGE_CLASS, "merges_pr"),
        (GH_ISSUE_CLASS, "files_issues"),
    )
    for class_keys, required in constraint_classes:
        if key not in class_keys:
            continue
        if not constraints.get("external_side_effects"):
            return _deny(
                f"{label} is an external side effect the envelope declares false"
            )
        if not constraints.get(required):
            return _deny(f"{label} requires {required} in the envelope")
        # The gate must judge the repository gh will actually mutate: a
        # positional URL, a repeated -R/--repo, a GH_REPO/GH_HOST
        # assignment, or shell state mutated by an earlier compound
        # segment (cd, export — including builtin-prefixed spellings this
        # classifier cannot enumerate) can all retarget the command after
        # the cwd-based gate approved it. Only a standalone simple command
        # is judged, and every selector it carries must agree.
        if not standalone:
            return _ask(
                f"{label} inside a compound command cannot be validated "
                "(earlier segments may retarget it); run it as a "
                "standalone command"
            )
        env_selector, env_escalate = _gh_env_repo_selector(env_assignments or [])
        if env_escalate is not None:
            return _ask(
                f"{label} carries {env_escalate}, which retargets gh away "
                "from the validated repository; escalating for review"
            )
        selectors, unresolved = _gh_repo_selectors(tokens, words[2:])
        if env_selector is not None:
            selectors.append(env_selector)
        if unresolved is not None:
            return _ask(
                f"{label} references {unresolved!r}, whose repository "
                "cannot be resolved; escalating for review"
            )
        distinct = sorted(set(selectors))
        if len(distinct) > 1:
            return _ask(
                f"{label} carries conflicting repository selectors "
                f"({', '.join(distinct)}); escalating for review"
            )
        repo = distinct[0] if distinct else resolve_cwd_repo(cwd)
        gate = _repo_gate(repo, constraints, label)
        if gate is not None:
            return gate
        return ALLOW

    if key in GH_DENY_CLASS or group in GH_DENY_GROUPS or key in GH_WORKFLOW_MUTATIONS:
        return _deny(
            f"{label} is outside every envelope allow class "
            "(PR create/update, comments, declared merges, and declared "
            "issue filing only)"
        )

    # Fail closed: gh verbs outside the known read-only and mutation classes
    # escalate rather than pass (F-003 — e.g. `gh run delete`).
    return _ask(f"cannot classify {label} under an active envelope")


def _classify_envelope_clear(
    tokens: List[str],
    context: Optional[WorkspaceContext],
    cwd: str,
    env_assignments: Optional[List[str]] = None,
    standalone: bool = False,
) -> Decision:
    """Sanction the documented completion clear against the audit record.

    The clear is allowed exactly when the newest audit record whose CONTENT
    identity (``message_id`` + ``receiver``) matches the active envelope
    shows a terminal ``result.final_state`` (``done`` only together with its
    ``completed_at_utc`` stamp): the task lifecycle is over and the
    enforcement window may close from inside the session. ``pending`` and
    ``paused`` keep the envelope active — a paused task re-authorizes via
    the §E checkpoint, never via clear — and a stamp-less ``done`` is a
    legacy born-done admission that was never finalized, not a completion.

    The validation judges the same effective target the CLI will use: the
    clear must be a standalone simple command (compound commands escalate —
    an earlier ``export`` or ``cd`` segment could retarget it), flag values
    use last-occurrence (argparse) semantics, relative ``--oacp-dir``
    resolves against the call's cwd, and an ``OACP_HOME=`` assignment on the
    command escalates outright (it retargets the CLI's home resolution). A
    clear aimed at a different project/receiver/home than the active
    envelope, or an unreadable audit record, cannot be validated here and
    escalates. Filenames are never trusted and sender data never reaches a
    filesystem glob.
    """
    if context is None or not context.message_id:
        return _deny(
            "oacp envelope clear cannot be validated without a resolved "
            "workspace; the completion clear is sanctioned by the task's "
            "audit record"
        )
    if not standalone:
        # Earlier segments of a compound command can mutate cwd or the
        # environment (`export OACP_HOME=…;`, `cd …;`) and retarget the
        # clear after this validation runs — only a standalone simple
        # command is judged, everything else escalates.
        return _ask(
            "oacp envelope clear inside a compound command cannot be "
            "validated (earlier segments may retarget it); run the clear "
            "as a standalone command"
        )
    for assignment in env_assignments or []:
        if assignment.startswith("OACP_HOME="):
            return _ask(
                "OACP_HOME override on an envelope clear retargets the CLI's "
                "home resolution and cannot be validated in-session; "
                "escalating for review"
            )
    target_project = _last_flag_value(tokens, "--project") or context.project
    target_receiver = _last_flag_value(tokens, "--receiver") or "claude"
    if target_project != context.project or target_receiver != context.receiver:
        return _ask(
            f"oacp envelope clear targets {target_project}/{target_receiver}, "
            f"not the active envelope ({context.project}/{context.receiver}); "
            "escalating for review"
        )
    oacp_dir = _last_flag_value(tokens, "--oacp-dir")
    if oacp_dir is not None:
        declared = os.path.realpath(_normalize_file_path(oacp_dir, cwd))
        if declared != os.path.realpath(str(context.oacp_root)):
            return _ask(
                "oacp envelope clear targets a different OACP home than the "
                "active envelope; escalating for review"
            )
    audit_dir = context.audit_dir()
    candidates = sorted(audit_dir.glob("*.yaml")) if audit_dir.is_dir() else []
    matches: List[Tuple[str, Dict[str, Any]]] = []
    for candidate in candidates:
        try:
            record = load_yaml_file(candidate)
        except Exception as exc:
            # Any unreadable record could be the authoritative newest one —
            # fail closed on the whole directory rather than guess.
            return _ask(
                f"unreadable audit record {candidate.name!r} while "
                f"validating envelope clear: {exc}"
            )
        if (
            str(record.get("message_id") or "") == context.message_id
            and str(record.get("receiver") or "") == context.receiver
        ):
            record_result = record.get("result")
            state_value = (
                record_result.get("final_state")
                if isinstance(record_result, dict)
                else None
            )
            if state_value == "superseded":
                # A superseded evaluation's authority transferred to its
                # successor; it must neither sanction nor block the clear.
                continue
            matches.append((candidate.name, record))
    if not matches:
        return _deny(
            "oacp envelope clear before any audit record matches "
            f"message_id {context.message_id!r} and receiver "
            f"{context.receiver!r}; the completion clear is sanctioned once "
            "the record's result.final_state is terminal (done/error/cancelled)"
        )
    record = max(matches, key=lambda item: item[0])[1]
    result = record.get("result")
    state = result.get("final_state") if isinstance(result, dict) else None
    completed_at = result.get("completed_at_utc") if isinstance(result, dict) else None
    if state in TERMINAL_AUDIT_STATES and (state != "done" or completed_at):
        return ALLOW
    if state == "done":
        return _deny(
            f"oacp envelope clear while the audit record for "
            f"{context.message_id} shows result.final_state 'done' with no "
            "completed_at_utc — a legacy born-done admission that was never "
            "finalized, not a completed task; run the terminal finalize "
            "(oacp autonomy finalize --final-state done) first"
        )
    return _deny(
        f"oacp envelope clear while the audit record for "
        f"{context.message_id} shows result.final_state {state!r}; the "
        "envelope stays active until the task completes (done/error/cancelled) — a "
        "paused task re-authorizes via the §E checkpoint, and completion "
        "requires the audit record update first"
    )


def _classify_oacp_verify(
    tokens: List[str],
    context: Optional[WorkspaceContext] = None,
    cwd: str = "",
    env_assignments: Optional[List[str]] = None,
    standalone: bool = False,
) -> Decision:
    """Allow the verifier's read-only argv grammar and its one canonical write.

    Quarantine writes beside the input and keeps the ask. ``--attach-audit``
    writes ``result.message_auth`` into an audit record under that record's
    lock: it is the canonical writer of the receiver's own verification
    stamp, the same class as ``oacp autonomy finalize``, so it passes exactly
    when the record is the active envelope's bound admission record and the
    message being verified is that record's accepted snapshot (see
    _classify_verify_attach_audit). Any other destination keeps the ask.
    Unknown options, including argparse abbreviations, keep the existing ask
    rather than silently opening a write path.
    """
    value_flags = {"--pins", "--project", "--receiver", "--oacp-dir"}
    positionals: List[str] = []
    attach_values: List[str] = []
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if re.search(r"[$`*?\[\]{}~]", token):
            return _ask("cannot resolve shell expansion in oacp verify arguments")
        if token in ("--json", "--help", "-h"):
            index += 1
            continue
        flag, separator, value = token.partition("=")
        if flag in value_flags or flag == "--attach-audit":
            if not separator:
                index += 1
                if index >= len(tokens):
                    return _ask("missing oacp verify option value")
                value = tokens[index]
            if not value or value.startswith("-") or re.search(r"[$`*?\[\]{}~]", value):
                return _ask("cannot resolve oacp verify option value")
            if flag == "--attach-audit":
                attach_values.append(value)
        elif token.startswith("-"):
            return _ask(
                "only read-only oacp verify is exempt; audit attachment, "
                "quarantine and unknown options require review"
            )
        else:
            positionals.append(token)
            if len(positionals) > 1:
                return _ask("cannot classify oacp verify arguments")
        index += 1
    if not attach_values:
        return ALLOW
    if len(attach_values) > 1 or len(positionals) != 1:
        return _ask(
            "only read-only oacp verify is exempt; --attach-audit takes one "
            "record beside one message"
        )
    return _classify_verify_attach_audit(
        tokens, attach_values[0], positionals[0], context, cwd, env_assignments, standalone
    )


def _classify_verify_attach_audit(
    tokens: List[str],
    record_arg: str,
    message_arg: str,
    context: Optional[WorkspaceContext],
    cwd: str,
    env_assignments: Optional[List[str]],
    standalone: bool,
) -> Decision:
    """Sanction ``oacp verify --attach-audit`` against the bound record only.

    The stamp is allowed when the record is the active envelope's own
    admission record — its resolved path is a ``.yaml`` directly inside the
    canonical ``audit/autonomy_decisions/`` directory and its content
    identity (``message_id`` + ``receiver`` + ``message_sha256``) matches the
    envelope, never the filename — and the message file named on the command
    hashes to that same ``message_sha256``: the stamp binds to the record's
    accepted snapshot, never to other bytes. Like the completion clear, it is
    judged only as a standalone simple command with no ``OACP_HOME=``
    override and with project/receiver/home selectors matching the envelope,
    so an earlier segment cannot retarget it after validation. Everything
    else — another record, a record elsewhere, an unreadable one, other
    message bytes, a compound command — keeps the ask.
    """
    prefix = (
        "only read-only oacp verify is exempt; --attach-audit passes only "
        "against the active envelope's own admission record"
    )
    if context is None or not context.message_id or not context.message_sha256:
        return _ask(f"{prefix} (no resolved workspace)")
    if not standalone:
        return _ask(f"{prefix} (compound command; run it standalone)")
    for assignment in env_assignments or []:
        if assignment.startswith("OACP_HOME="):
            return _ask(f"{prefix} (OACP_HOME override)")
    target_project = _last_flag_value(tokens, "--project") or context.project
    target_receiver = _last_flag_value(tokens, "--receiver") or context.receiver
    if target_project != context.project or target_receiver != context.receiver:
        return _ask(
            f"{prefix} (targets {target_project}/{target_receiver}, not "
            f"{context.project}/{context.receiver})"
        )
    oacp_dir = _last_flag_value(tokens, "--oacp-dir")
    if oacp_dir is not None:
        declared = os.path.realpath(_normalize_file_path(oacp_dir, cwd))
        if declared != os.path.realpath(str(context.oacp_root)):
            return _ask(f"{prefix} (different OACP home)")
    resolved = os.path.realpath(_normalize_file_path(record_arg, cwd))
    audit_dir = os.path.realpath(str(context.audit_dir()))
    if (
        os.path.dirname(resolved) != audit_dir
        or not resolved.endswith(".yaml")
        or not os.path.isfile(resolved)
    ):
        return _ask(
            f"{prefix} ({record_arg!r} is not a record in the canonical "
            "audit directory)"
        )
    try:
        from _oacp_constants import locked_audit

        with locked_audit(Path(resolved)):
            record = load_yaml_file(Path(resolved))
    except Exception as exc:
        return _ask(f"{prefix} (unreadable record: {exc})")
    if (
        not isinstance(record, dict)
        or str(record.get("message_id") or "") != context.message_id
        or str(record.get("receiver") or "") != context.receiver
        or str(record.get("message_sha256") or "") != context.message_sha256
    ):
        return _ask(f"{prefix} (record identity does not match the envelope)")
    message_path = os.path.realpath(_normalize_file_path(message_arg, cwd))
    try:
        with open(message_path, "rb") as handle:
            digest = hashlib.sha256(handle.read(MESSAGE_HASH_LIMIT + 1)).hexdigest()
    except OSError as exc:
        return _ask(f"{prefix} (unreadable message: {exc})")
    if digest != context.message_sha256:
        return _ask(f"{prefix} (message bytes are not the record's accepted snapshot)")
    return ALLOW


def _oacp_finalize_is_readonly(tokens: List[str]) -> bool:
    """``oacp autonomy finalize`` forms that persist nothing.

    ``--validate`` only reads (``--sweep`` reads the directory); ``--dry-run``
    evaluates under the record lock and writes no record. Exact spellings
    only — an argparse abbreviation or any expansion syntax keeps the ask.
    """
    if any(re.search(r"[$`*?\[\]{}~]", token) for token in tokens):
        return False
    return "--validate" in tokens or "--dry-run" in tokens


def _classify_oacp(
    tokens: List[str],
    context: Optional[WorkspaceContext] = None,
    cwd: str = "",
    env_assignments: Optional[List[str]] = None,
    standalone: bool = False,
) -> Decision:
    """Scope the oacp exemption to what the protocol actually promises.

    Only `oacp send` (the §E notification pipe) plus read-only surfaces are
    exempt. `oacp envelope compile` from inside an enveloped session is
    self-modification of the active constraints (F-001); `oacp envelope
    clear` is the documented completion step and is sanctioned exactly when
    the task's audit record shows a terminal outcome; other mutating
    subcommands escalate.
    """
    words = [token for token in tokens[1:] if not token.startswith("-")]
    sub = words[0] if words else ""
    action = words[1] if len(words) > 1 else ""

    if sub in OACP_ALWAYS_ALLOWED:
        return ALLOW
    if sub == "verify":
        if len(tokens) < 2 or tokens[1] != "verify":
            return _ask("cannot classify oacp verify invocation")
        return _classify_oacp_verify(
            tokens, context, cwd, env_assignments, standalone
        )
    if sub == "envelope":
        if action == "show":
            return ALLOW
        if action == "clear":
            return _classify_envelope_clear(
                tokens, context, cwd, env_assignments, standalone
            )
        return _deny(
            f"oacp envelope {action} modifies the active envelope from inside "
            "the enveloped session; re-authorization goes through the §E "
            "checkpoint, not self-service recompilation"
        )
    if sub == "agent" and action in ("show", "list"):
        return ALLOW
    if sub == "autonomy":
        if action == "finalize" and _oacp_finalize_is_readonly(tokens):
            return ALLOW
        return _ask(
            f"cannot classify oacp autonomy {action} under an active "
            "envelope; it edits the task's own audit record — the authority "
            "the envelope overlay reads — so it is reviewed, never "
            "self-sanctioned (--validate and --dry-run are exempt)"
        )
    return _ask(
        f"cannot classify oacp {sub} under an active envelope; only send and "
        "read-only subcommands are exempt"
    )


def _classify_dependency_command(
    prog: str,
    tokens: List[str],
    constraints: Dict[str, Any],
) -> Optional[Decision]:
    if prog.startswith("python"):
        if "-m" in tokens:
            module_index = tokens.index("-m") + 1
            if module_index < len(tokens) and tokens[module_index] in ("pip", "pip3"):
                prog = "pip"
                tokens = tokens[module_index:]
            else:
                return None
        else:
            return None
    if prog not in PACKAGE_MANAGERS:
        return None
    verbs = [token for token in tokens[1:] if not token.startswith("-")]
    # `uv pip install`, `uv tool install`: scan the first two positionals.
    for verb in verbs[:2]:
        if verb in DEPENDENCY_VERBS:
            if constraints.get("touches_dependencies"):
                return ALLOW
            return _deny(
                f"{prog} {verb} changes dependencies the envelope declares false"
            )
    return None


def _mutator_operands(tokens: List[str]) -> List[str]:
    """Effective path operands of a filesystem-mutator invocation.

    Judged the way the utility parses argv, not by dash-prefix alone: `--`
    ends option processing (everything after is an operand even if it starts
    with a dash), and the GNU target-directory spellings (`-t DIR`, `-tDIR`,
    `--target-directory DIR`, `--target-directory=DIR`) contribute their
    directory value. Capturing `-t` generically across the mutator set is a
    conservative superset — a value misread from a tool that lacks the flag
    only adds a checked operand, never removes one.
    """
    operands: List[str] = []
    options_ended = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if options_ended or not token.startswith("-"):
            operands.append(token)
            index += 1
            continue
        if token == "--":
            options_ended = True
            index += 1
            continue
        if token in ("-t", "--target-directory") and index + 1 < len(tokens):
            operands.append(tokens[index + 1])
            index += 2
            continue
        if token.startswith("--target-directory="):
            operands.append(token.split("=", 1)[1])
            index += 1
            continue
        if token.startswith("-t") and not token.startswith("--") and len(token) > 2:
            operands.append(token[2:])
            index += 1
            continue
        index += 1
    return operands


def _target_directory_option(tokens: List[str]) -> Optional[str]:
    """Value of a GNU target-directory option (`-t DIR`, `-tDIR`, a short
    cluster ending in `t`, `--target-directory DIR`, `--target-directory=DIR`,
    and — because GNU getopt_long accepts unambiguous abbreviations — any
    `--t…` prefix of the long name, attached or separate) — the destination
    of every operand when present. `--` ends option processing. None when
    the invocation carries no such option."""
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return None
        if token.startswith("--"):
            name, attached, value = token[2:].partition("=")
            if name and "target-directory".startswith(name):
                if attached:
                    return value
                return tokens[index + 1] if index + 1 < len(tokens) else None
            index += 1
            continue
        if token.startswith("-"):
            match = SHORT_TARGET_DIRECTORY_RE.match(token)
            if match is not None:
                if match.group(1):
                    return match.group(1)
                return tokens[index + 1] if index + 1 < len(tokens) else None
        index += 1
    return None


def _operands(tokens: List[str]) -> List[str]:
    """Non-option operands of an argv, honouring `--` (everything after it
    is an operand even when it starts with a dash)."""
    operands: List[str] = []
    options_ended = False
    for token in tokens[1:]:
        if options_ended or token == "-" or not token.startswith("-"):
            operands.append(token)
        elif token == "--":
            options_ended = True
    return operands


def _sed_in_place_targets(tokens: List[str]) -> Optional[List[str]]:
    """File operands of an in-place sed invocation; None when sed is not
    editing in place (its output then goes to stdout and any redirect is
    gated separately).

    In-place spellings: `-i[SUFFIX]`, a short cluster containing `i`
    (`-ni`, `-Ei.bak`), `--in-place[=SUFFIX]` and its unambiguous prefixes
    (`--i…`). Script options (`-e`/`-f`, attached or separate,
    `--expression`/`--file` with prefixes) make every operand a file;
    without one the first operand is the script. `-l`/`--line-length`
    values are skipped so they never read as operands.
    """
    in_place = False
    script_option = False
    operands: List[str] = []
    options_ended = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if options_ended or token == "-" or not token.startswith("-"):
            operands.append(token)
            continue
        if token == "--":
            options_ended = True
            continue
        if token.startswith("--"):
            name, attached, _value = token[2:].partition("=")
            if not name:
                continue
            if "in-place".startswith(name):
                in_place = True
            elif "expression".startswith(name) or (
                len(name) >= 2 and "file".startswith(name)
            ):
                script_option = True
                if not attached:
                    index += 1
            elif "line-length".startswith(name) and not attached:
                index += 1
            continue
        for position, letter in enumerate(token[1:]):
            if letter == "i":
                # The rest of the cluster is the optional backup suffix. BSD
                # sed takes the suffix as a separate argument, and its empty
                # spelling (`-i ''`) is that suffix, not the script.
                in_place = True
                if (
                    position == len(token) - 2
                    and index < len(tokens)
                    and tokens[index] == ""
                ):
                    index += 1
                break
            if letter in ("e", "f", "l"):
                # Value is the rest of the cluster, or the next token.
                if letter != "l":
                    script_option = True
                if position == len(token) - 2:
                    index += 1
                break
    if not in_place:
        return None
    return operands if script_option else operands[1:]


def _shell_spans(segment: str) -> List[Tuple[int, int, str]]:
    """Share quote/escape state between redirect and argv views.

    Only a live substitution opener may hide a span from shlex. Literal
    openers in single quotes or behind a backslash must remain shell data,
    or they can swallow the command following an environment assignment.
    Substitution nesting remains bounded by _segments_of's fail-closed check.
    """
    spans: List[Tuple[int, int, str]] = []
    quote: Optional[str] = None
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "\\" and quote != "'":
            end = min(index + 2, len(segment))
            spans.append((index, end, "data"))
            index = end
            continue
        if quote != "'":
            match = SUBSTITUTION_RE.match(segment, index)
            kind = "substitution"
            if match is None and quote is None:
                match = PROCESS_SUBSTITUTION_RE.match(segment, index)
                kind = "process"
            if match is not None:
                spans.append((index, match.end(), kind))
                index = match.end()
                continue
        kind = "syntax"
        if quote is not None:
            kind = "data"
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            kind = "data"
        spans.append((index, index + 1, kind))
        index += 1
    return spans


def _shell_syntax_view(segment: str) -> str:
    """Mask shell data without losing offsets or quoted filename whitespace.

    Substitution bodies are classified separately by _segments_of; their
    operators must never become redirects of the outer command.
    """
    return "".join(
        segment[start:end] if kind == "syntax" else "x" * (end - start)
        for start, end, kind in _shell_spans(segment)
    )


def _shell_tokens(segment: str) -> List[str]:
    """Keep live substitutions opaque to shlex, then restore their spelling.

    shlex alone splits VAR=$(program arg) in the middle of the assignment,
    hiding the actual program that follows it. The spelling is restored so
    unresolved selectors, write targets and program names still fail closed.
    """
    prefix = "__oacp_substitution_"
    while prefix in segment:
        prefix += "_"
    replacements: Dict[str, str] = {}
    parts: List[str] = []
    for start, end, kind in _shell_spans(segment):
        raw = segment[start:end]
        if kind == "substitution":
            marker = f"{prefix}{len(replacements)}__"
            replacements[marker] = raw
            parts.append(marker)
        else:
            parts.append(raw)
    tokens = shlex.split("".join(parts), posix=True)
    for marker, original in replacements.items():
        tokens = [token.replace(marker, original) for token in tokens]
    return tokens


def _segment_write_targets(tokens: List[str], segment: str) -> List[str]:
    """Determinable file-write targets of one shell segment (F-004).

    Covers redirects plus common writer programs whose destinations are
    parseable from argv. Read-only or unrecognized programs contribute no
    targets — the secret/dependency/counter gate then simply does not fire.
    """
    targets = []
    for match in REDIRECT_TARGET_RE.finditer(_shell_syntax_view(segment)):
        raw_word = segment[match.start("word"):match.end("word")]
        if raw_word.startswith((">(", "<(")):
            continue
        words = _shell_tokens(raw_word)
        if not words or not words[0]:
            continue
        word = words[0]
        if match.group("dup") and (word.isdigit() or word == "-"):
            continue
        targets.append(word)
    if not tokens:
        return targets
    prog = tokens[0].rsplit("/", 1)[-1]
    positional = _operands(tokens)
    if prog in ("touch", "tee"):
        targets.extend(positional)
    elif prog in ("cp", "mv", "install"):
        target_directory = _target_directory_option(tokens)
        if target_directory is not None:
            targets.append(target_directory)
        elif len(positional) >= 2:
            targets.append(positional[-1])
    elif prog == "truncate":
        targets.extend(positional)
    elif prog == "sed":
        targets.extend(_sed_in_place_targets(tokens) or [])
    elif prog == "dd":
        targets.extend(
            token[len("of="):] for token in tokens[1:] if token.startswith("of=")
        )
    return targets


def _reauthorized_scope(
    context: Optional[WorkspaceContext],
) -> Optional[Dict[str, Any]]:
    """The durable scope a resumed §E re-authorization granted this task.

    The envelope is immutable once compiled, so a granted re-authorization
    reaches enforcement as an overlay read here rather than as a recompile
    (``oacp envelope compile --extend`` is in-session-denied by design).
    Only the audit record's *effective* ``scope`` is consulted — the
    policy-capped value the gate wrote, never the raw ``requested_scope`` a
    sender asked for. A fresh scope-less approval records no scope: it
    cleared exactly one pause and creates nothing durable, so it widens
    nothing here either.

    Record selection mirrors the completion clear: content identity only
    (``message_id`` + ``receiver``, plus the envelope's ``message_sha256`` so
    a record for different message bytes can never widen this envelope),
    filenames are never trusted, ``superseded`` evaluations are skipped, and
    the newest match governs. Every failure path returns ``None`` — an
    absent, unreadable, or malformed record leaves the compiled bound
    standing, which is the fail-closed direction for a widening overlay.
    """
    if context is None or not context.message_id or not context.message_sha256:
        return None
    audit_dir = context.audit_dir()
    if not audit_dir.is_dir():
        return None
    from _oacp_constants import locked_audit

    matches: List[Tuple[str, Dict[str, Any]]] = []
    for candidate in sorted(audit_dir.glob("*.yaml")):
        try:
            # Read under the record's own audit lock: the canonical writers
            # hold it across their read-modify-replace, so a concurrent
            # checkpoint write cannot be observed half-applied.
            with locked_audit(candidate):
                record = load_yaml_file(candidate)
        except Exception:
            # An unreadable record cannot widen anything; the compiled bound
            # stands and the write blocks with the canonical opener.
            continue
        if not isinstance(record, dict):
            continue
        if (
            str(record.get("message_id") or "") != context.message_id
            or str(record.get("receiver") or "") != context.receiver
            or str(record.get("message_sha256") or "") != context.message_sha256
        ):
            continue
        result = record.get("result")
        if not isinstance(result, dict):
            continue
        if result.get("final_state") == "superseded":
            continue
        matches.append((candidate.name, record))
    if not matches:
        return None
    governing = max(matches, key=lambda item: item[0])[1]["result"]
    checkpoint = governing.get("threshold_checkpoint")
    if not isinstance(checkpoint, dict):
        return None
    reauth = checkpoint.get("reauthorization")
    if not isinstance(reauth, dict):
        return None
    # Validate the whole re-authorization state, not just `disposition`: a
    # partial or malformed block must not widen anything. The vocabulary is
    # imported from the evaluator that writes it, never re-spelled here, so
    # the two cannot drift. `human_outcome` is deliberately NOT required —
    # the governing channel may be `sender_reply`, whose authority is the
    # sender's signed follow-up, not a receiver-side human ruling.
    from autonomy_gate import REAUTH_DECISIONS, REAUTH_GOVERNING_CHANNELS

    if (
        reauth.get("disposition") != "resumed"
        or reauth.get("presented") is not True
        or reauth.get("channel") not in REAUTH_GOVERNING_CHANNELS
        or reauth.get("decision") not in REAUTH_DECISIONS
        or reauth.get("decision") == "declined"
    ):
        return None
    scope = reauth.get("scope")
    return scope if isinstance(scope, dict) else None


def _reauthorized_file_budget(
    scope: Optional[Dict[str, Any]], declared: int
) -> int:
    """``declared``, widened to a granted bound but never narrowed by one.

    A grant can only ever open room the envelope did not already have: the
    overlay widens exactly to the recorded scope and no further, and a scope
    at or below the compiled bound is a no-op, so existing envelopes keep
    their outcomes unchanged. Only the files axis is overlaid — the grant's
    ``max_actual_minutes`` has no hook enforcement point (``estimated_minutes``
    is recorded for the §E self-check and carries no runtime semantics).
    """
    if not scope:
        return declared
    granted = scope.get("max_actual_files_touched")
    if isinstance(granted, bool) or not isinstance(granted, int):
        return declared
    return granted if granted > declared else declared


def _gate_write_paths(
    paths: List[str],
    cwd: str,
    constraints: Dict[str, Any],
    counters: Dict[str, Any],
    verb: str,
    context: Optional[WorkspaceContext] = None,
    dependency_exemption: Optional[Callable[[str], bool]] = None,
) -> Decision:
    """Shared secret/dependency/file-counter gate for file tools and Bash
    writes. Counts distinct paths cumulatively so a single call cannot jump
    the `expected_files_touched` ceiling (F-004: `touch a b c`). Bookkeeping
    surfaces pass the secret/dependency gates but never reach the counter:
    completion instrumentation must not compete with the task's declared
    file budget. The ceiling is the compiled ``expected_files_touched``,
    widened at enforcement time to a granted §E re-authorization when one
    governs the task — resolved lazily on first contact with the ceiling so
    the ordinary path never reads the audit directory."""
    touched = list(counters.get("files_touched") or [])
    declared = int(constraints.get("expected_files_touched") or 0)
    expected: Optional[int] = None
    new_files: List[str] = []
    for path in paths:
        normalized = _normalize_file_path(path, cwd)
        if normalized.startswith("/dev/"):
            continue
        if not constraints.get("touches_auth_config_or_secrets") and is_secret_path(
            normalized
        ):
            return _deny(
                f"{verb} of secret-class path {normalized!r} is outside the "
                "envelope (touches_auth_config_or_secrets: false)"
            )
        if context is not None:
            canonical = os.path.realpath(normalized)
            for label, root in context.authority_roots():
                if _within(canonical, os.path.realpath(str(root))):
                    return _deny(
                        f"{verb} of {label} {normalized!r} from inside the "
                        "enveloped session is authority self-modification; "
                        "these surfaces govern the envelope's own bound and "
                        "are never written directly (the canonical oacp "
                        "writers are classified as commands)"
                    )
            if not constraints.get("touches_auth_config_or_secrets"):
                for root in context.trust_roots():
                    if _within(canonical, os.path.realpath(str(root))):
                        return _deny(
                            f"{verb} of trust root {normalized!r} is outside "
                            "the envelope (authority-bearing auth config; "
                            "touches_auth_config_or_secrets: false)"
                        )
        if (
            not constraints.get("touches_dependencies")
            and is_dependency_path(normalized)
            and not (
                dependency_exemption is not None
                and dependency_exemption(normalized)
            )
        ):
            return _deny(
                f"{verb} of dependency manifest {normalized!r} is outside the "
                "envelope (touches_dependencies: false)"
            )
        if is_bookkeeping_path(normalized, context):
            continue
        if normalized in touched or normalized in new_files:
            continue
        if len(touched) + len(new_files) >= declared:
            if expected is None:
                expected = _reauthorized_file_budget(
                    _reauthorized_scope(context), declared
                )
            if len(touched) + len(new_files) >= expected:
                return _deny(
                    f"{BLOCKED_OPENER} — files_touched expected {expected}, "
                    f"now {len(touched) + len(new_files) + 1}"
                )
        new_files.append(normalized)
    if new_files:
        return Decision("allow", new_files=new_files)
    return ALLOW


def _reject_unsupported_shell_words(
    command: str, state: Tuple[Optional[str], bool] = (None, False)
) -> Tuple[Optional[str], bool]:
    """Fail closed before our POSIX-style scanners interpret shell words.

    ANSI-C strings use different quote/escape rules from shlex. Comments can
    contain arbitrary quotes or heredoc operators that would hide later code.
    Neither construct is modeled here; quoted/escaped data and embedded '#'
    remain ordinary word content. Substitution bodies are checked separately.
    """
    quote, word_started = state
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            if command[index + 1:index + 2] != "\n":
                word_started = True
            index += 2
            continue
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char == "$":
            following = index + 1
            while command[following:following + 2] == "\\\n":
                following += 2
            if command[following:following + 1] == "'":
                raise ValueError("ANSI-C shell quoting is not statically supported")
        if char == "#" and not word_started:
            raise ValueError("word-initial shell comments are not statically supported")
        if char in ("'", '"', "`"):
            quote = char
            word_started = True
        elif char in " \t\n;|&()<>":
            word_started = False
        else:
            word_started = True
        index += 1
    return quote, word_started


def _split_shell_segments(command: str) -> List[str]:
    """Split on shell operators outside quotes and substitutions.

    This is deliberately a small classifier scanner, not a shell parser. It
    preserves raw segment text for the existing redirect checks, joins shell
    line continuations, and rejects unterminated quoting/substitution instead
    of guessing. Separator characters inside quoted arguments stay data.
    """
    return [
        segment for segment, _operator in _split_shell_segments_with_operators(command)
    ]


def _split_shell_segments_with_operators(command: str) -> List[Tuple[str, str]]:
    """Segments paired with the operator that ends each one.

    The operator is ``&&``, ``||``, ``|``, ``|&``, ``&`` or ``;`` (a newline
    reads as ``;``), and ``""`` for the final segment. Empty segments (a
    ``;;`` case terminator, a leading operator) are dropped with their
    operator, so a recorded operator always follows real text.
    """
    _reject_unsupported_shell_words(command)
    segments: List[Tuple[str, str]] = []
    current: List[str] = []
    quote: Optional[str] = None
    in_backticks = False
    substitution_depth = 0
    index = 0

    def flush(operator: str) -> None:
        segment = "".join(current).strip()
        if segment:
            segments.append((segment, operator))
        current.clear()

    while index < len(command):
        char = command[index]
        next_char = command[index + 1] if index + 1 < len(command) else ""

        if char == "\\" and quote != "'":
            if next_char == "\n":
                index += 2
                continue
            current.append(char)
            if next_char:
                current.append(next_char)
                index += 2
            else:
                index += 1
            continue

        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            index += 1
            continue

        if in_backticks:
            current.append(char)
            if char == "`":
                in_backticks = False
            index += 1
            continue

        if char in ("'", '"'):
            quote = char
            current.append(char)
            index += 1
            continue
        if char == "`":
            in_backticks = True
            current.append(char)
            index += 1
            continue
        if char == "$" and next_char == "(":
            substitution_depth += 1
            current.extend((char, next_char))
            index += 2
            continue
        if substitution_depth:
            if char == "(":
                substitution_depth += 1
            elif char == ")":
                substitution_depth -= 1
            current.append(char)
            index += 1
            continue

        if char == "|" and current and current[-1] == ">":
            # `>|` (noclobber override) is a redirection, not a pipe; its
            # target stays visible to the redirect gate.
            current.append(char)
            index += 1
            continue
        if char in (";", "\n", "|"):
            operator = ";"
            index += 1
            if char == "|":
                operator = "|"
                if index < len(command) and command[index] in ("|", "&"):
                    operator += command[index]
                    index += 1
            flush(operator)
            continue
        if char == "&":
            # `2>&1`, `<&0`, and `&>file` are redirections, not command
            # boundaries. Their targets remain visible to the redirect gate.
            if (current and current[-1] in ("<", ">")) or next_char == ">":
                current.append(char)
                index += 1
                continue
            operator = "&"
            index += 1
            if index < len(command) and command[index] == "&":
                operator = "&&"
                index += 1
            flush(operator)
            continue

        current.append(char)
        index += 1

    if quote is not None or in_backticks or substitution_depth:
        raise ValueError("unterminated shell quote or command substitution")
    flush("")
    return segments


def _heredoc_delimiters(line: str) -> Optional[List[Tuple[str, bool, bool, int, int]]]:
    """Heredoc operators a line opens, outside quotes, in operator order.

    Each entry is ``(word, strip_leading_tabs, body_expands, start, end)``:
    the delimiter word, whether ``<<-`` tab-stripping applies, whether the
    body undergoes shell expansion (a bare, unquoted, unescaped word — a
    quoted or backslashed delimiter makes the body literal data), and the
    operator's span on the line. Returns None when the line carries a
    ``<<`` outside quotes with no parseable delimiter — the body's extent
    is then unknowable and the caller escalates.
    """
    delimiters: List[Tuple[str, bool, bool, int, int]] = []
    quote: Optional[str] = None
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        if line.startswith("<<<", index):
            index += 3
            continue
        if line.startswith("<<", index):
            match = HEREDOC_OPERATOR_RE.match(line, index)
            if match is None:
                return None
            word = match.group(2) or match.group(3) or match.group(5)
            expands = match.group(5) is not None and match.group(4) is None
            delimiters.append(
                (word, bool(match.group(1)), expands, match.start(), match.end())
            )
            index = match.end()
            continue
        index += 1
    return delimiters


def _strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc bodies, their terminator lines, and the `<<WORD` operators.

    A heredoc body is data handed to the program on stdin, not shell: the
    redirect scanner must not read `->` inside a string literal there as a
    write. The operator line itself stays, minus the operator, so a redirect
    or writer program on it (`cat > path <<EOF`, `tee path <<EOF`) is still
    a real write that counts, while the `<<EOF` word never reads as an
    operand. With a quoted or backslashed delimiter the body is literal and
    is dropped whole; with a bare delimiter the shell still expands the
    body, so a command or backtick substitution inside it can execute a
    write — that body is not dropped but escalated. Raises ValueError on an
    unterminated heredoc, an operator without a parseable delimiter, or an
    executable substitution inside an expanding body, or an apparent operator
    on a line starting inside a quote (the caller escalates rather than
    guessing).
    """
    kept: List[str] = []
    pending: List[Tuple[str, bool, bool]] = []
    shell_state: Tuple[Optional[str], bool] = (None, False)
    for line in command.split("\n"):
        if pending:
            word, strip_tabs, expands = pending[0]
            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate == word:
                pending.pop(0)
                continue
            if expands and ("$(" in line or "`" in line):
                raise ValueError(
                    "command substitution inside an expanding (unquoted) heredoc body"
                )
            continue
        # Check operator lines before stripping. Carry quote/word state over
        # newlines and continuations; literal heredoc payloads skip this check.
        starts_in_quote = shell_state[0] is not None
        shell_state = _reject_unsupported_shell_words(line + "\n", shell_state)
        delimiters = _heredoc_delimiters(line)
        if delimiters is None:
            raise ValueError("heredoc operator without a delimiter")
        if starts_in_quote and delimiters:
            # The line-local delimiter scanner cannot distinguish a quoted
            # operator from one after the inherited quote closes. Never drop
            # later program text as a guessed heredoc body in either case.
            raise ValueError("heredoc operator on a line starting inside a multi-line quote")
        for word, strip_tabs, expands, start, end in reversed(delimiters):
            line = line[:start] + line[end:]
        pending.extend(
            (word, strip_tabs, expands) for word, strip_tabs, expands, _s, _e in delimiters
        )
        kept.append(line)
    if pending:
        raise ValueError("unterminated heredoc")
    return "\n".join(kept)


def _segments_of(command: str) -> List[str]:
    """Return top-level and command-substitution segments (F-002)."""
    segments = _split_shell_segments(command)
    substitutions = list(SUBSTITUTION_RE.finditer(command))
    simple_dollar_substitutions = sum(
        match.group(1) is not None for match in substitutions
    )
    if command.count("$(") != simple_dollar_substitutions:
        # The deliberately small extraction regex cannot prove the contents
        # of nested or parenthesized substitutions. Escalate the whole call
        # instead of letting an uninspected inner mutation hide under a
        # read-only outer command.
        raise ValueError("nested or parenthesized command substitution")
    for match in substitutions:
        inner = match.group(1) or match.group(2) or ""
        segments.extend(_split_shell_segments(inner))
    process_substitutions = list(PROCESS_SUBSTITUTION_RE.finditer(command))
    if command.count("<(") + command.count(">(") != len(process_substitutions):
        raise ValueError("nested or parenthesized process substitution")
    for match in process_substitutions:
        segments.extend(_split_shell_segments(match.group(1)))
    return segments


DEGRADED_READONLY_COMMANDS = {
    "cat",
    "cmp",
    "diff",
    "grep",
    "hash",
    "head",
    "jq",
    "ls",
    "pwd",
    "rg",
    "stat",
    "tail",
    "test",
    "type",
    "wc",
    "which",
}


def _degraded_readonly_segment(segment: str) -> bool:
    """Conservatively recognize a direct read-only head after argv failure.

    No shell control, redirect, expansion, or substitution syntax is allowed
    on this fallback. Ripgrep's command-executing ``--pre`` mode is excluded.
    The quote-aware scanner has already rejected unbalanced shell syntax.
    """
    if re.search(r"[;|&<>()`$\n]", segment) or EXPANSION_SYNTAX_RE.search(segment):
        return False
    words = segment.strip().split()
    while words and ENV_ASSIGNMENT_RE.match(words[0]):
        words.pop(0)
    if words and words[0] in ("command", "builtin"):
        words.pop(0)
    if not words or words[0].startswith("-"):
        return False
    return _is_readonly_program(words)


def _is_readonly_program(words: List[str]) -> bool:
    """Whether an argv names one of the plain read-only utilities.

    One vocabulary for the degraded fallback and for nested programs
    (``xargs``, ``find -exec``): a nested invocation is a read exactly when
    its direct spelling would be. Ripgrep's command-executing ``--pre``
    mode is excluded.
    """
    if not words or not words[0]:
        return False
    program = Path(words[0]).name
    if program == "rg" and any(
        word == "--pre" or word.startswith("--pre=") for word in words[1:]
    ):
        return False
    return program in DEGRADED_READONLY_COMMANDS


def _inline_code_bodies(prog: str, tokens: List[str]) -> Optional[List[str]]:
    """The inline code operands of a node / perl / ruby argv, or None without any.

    Exact flags (``-e``, ``--eval``, ``-p``), ``--eval=CODE`` / ``--print=CODE``,
    short clusters ending in the code flag (``-ne``) and the attached spelling
    (``-e'print 1'``) all count, so no inline body reaches the plain-program
    path unread. Python argv is read by _python_inline_invocation instead.
    """
    flags = INTERPRETER_CODE_FLAGS[prog]
    cluster = INLINE_CODE_CLUSTER_RE.get(INLINE_CODE_FAMILY[prog])
    bodies: List[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            break
        if token in flags or (cluster is not None and cluster.match(token)):
            bodies.append(tokens[index + 1] if index + 1 < len(tokens) else "")
            index += 2
            continue
        if token.startswith("--eval=") or token.startswith("--print="):
            if prog in ("node", "nodejs"):
                bodies.append(token.split("=", 1)[1])
        elif (
            token.startswith("-")
            and not token.startswith("--")
            and cluster is not None
            and len(token) > 2
        ):
            for split in range(2, len(token)):
                if cluster.match(token[:split]):
                    bodies.append(token[split:])
                    break
        index += 1
    return bodies or None


def _python_inline_invocation(tokens: List[str]) -> Optional[Tuple[str, Optional[str]]]:
    """``(code, option_problem)`` for a ``python -c`` argv, or None.

    None means the argv runs no inline code: a script path, ``-m`` or ``--``
    comes before any ``-c``, and the plain-program path classifies it. Every
    option before the code flag must come from the read-only option grammar;
    the first that does not is returned as the problem. The exact flag
    (``-c``), a cluster ending in it (``-Ic``) and the attached spelling
    (``-cCODE``) all yield the code; a flag with no operand yields "".
    """
    cluster = INLINE_CODE_CLUSTER_RE["python"]
    problem: Optional[str] = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--" or not token.startswith("-") or token.startswith("-m"):
            return None
        if token == "-c" or cluster.match(token):
            return (tokens[index + 1] if index + 1 < len(tokens) else ""), problem
        if token in ("-W", "-X"):
            value = tokens[index + 1] if index + 1 < len(tokens) else ""
            if not value or value.startswith("-") or (
                token == "-X" and not PY_INLINE_X_OPTION_RE.match(value)
            ):
                problem = problem or f"{token} {value}".strip()
            index += 2
            continue
        if token.startswith("-W") and len(token) > 2:
            index += 1
            continue
        if token.startswith("-X") and len(token) > 2:
            if not PY_INLINE_X_OPTION_RE.match(token[2:]):
                problem = problem or token
            index += 1
            continue
        if PY_INLINE_OPTION_RE.match(token):
            index += 1
            continue
        if not token.startswith("--") and len(token) > 2:
            for split in range(2, len(token)):
                if cluster.match(token[:split]):
                    return token[split:], problem
        problem = problem or token
        index += 1
    return None


def _inline_code_decision(prog: str, tokens: List[str], segment: str) -> Optional[Decision]:
    """Classify an interpreter argv that may carry inline code.

    None: the argv runs no inline code (a script file, ``-m``), so the
    plain-program path applies. ALLOW: Python code inside the positive
    read-only grammar, in a static segment (no ``$`` or backtick outside
    single quotes — a value spliced into the code is code the hook never
    saw) that redirects to no file. Otherwise an ask naming the construct:
    an interpreter option outside the grammar, the first node of the code
    outside it, or a node / perl / ruby body, for which no grammar exists.
    """
    family = INLINE_CODE_FAMILY[prog]
    if family != "python":
        if _inline_code_bodies(prog, tokens) is None:
            return None
        return _ask(
            f"cannot classify inline interpreter code (no read-only grammar "
            f"for {family}; run a script file, or compute in python3 -c): "
            f"{segment!r}"
        )
    invocation = _python_inline_invocation(tokens)
    if invocation is None:
        return None
    code, option_problem = invocation
    if not code.strip():
        return _ask(
            f"cannot classify inline interpreter code (no code operand): "
            f"{segment!r}"
        )
    if not _inline_code_is_static(segment):
        return _ask(
            "cannot classify inline interpreter code (shell expansion inside "
            f"the code; pass values via argv or the environment): {segment!r}"
        )
    redirect = _segment_file_redirect(segment)
    if redirect is not None:
        return _ask(
            f"cannot classify inline interpreter code (output redirect to "
            f"{redirect!r}): {segment!r}"
        )
    if option_problem is not None:
        return _ask(
            f"cannot classify inline interpreter code (interpreter option "
            f"{option_problem!r} is outside the read-only option grammar): "
            f"{segment!r}"
        )
    problem = _python_code_problem(code)
    if problem is not None:
        return _ask(f"cannot classify inline interpreter code ({problem}): {segment!r}")
    return ALLOW


def _py_member_allowed(module: str, member: str) -> bool:
    members = PY_READONLY_MODULES.get(module)
    return members is not None and member in members


def _py_read_mode(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and PY_READ_MODE_RE.match(node.value) is not None
    )


def _py_open_call_problem(call: ast.Call, method: bool) -> Optional[str]:
    """Why a call to an open-like callee is not a literal read, or None.

    For the function form (``open(path, mode, …)``, ``codecs.open``,
    ``zipfile.ZipFile``) the mode is the second positional; for the method
    form (``Path.open(mode)``, ``ZipFile.open(name, mode)``) the receiver's
    type is unknown, so every positional must read as a mode. A mode must be
    a string literal matching the read grammar; ``*args`` / ``**kwargs``
    hide the mode and ask.
    """
    for arg in call.args:
        if isinstance(arg, ast.Starred):
            return "starred argument to a file-opening call"
    modes = list(call.args if method else call.args[1:2])
    for keyword in call.keywords:
        if keyword.arg is None:
            return "** argument to a file-opening call"
        if keyword.arg == "mode":
            modes.append(keyword.value)
    for mode in modes:
        if _py_read_mode(mode):
            continue
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
            return f"file open for writing ({mode.value!r})"
        return "file open mode is not a literal read mode"
    return None


class _PythonReadOnlyGrammar(ast.NodeVisitor):
    """Judge a parsed ``python -c`` body against the positive grammar.

    ``aliases`` maps a bound name to the module it imports; ``bound`` holds
    every name the code binds (collected flat, without scope analysis — a
    superset is safe because every capability still enters through a checked
    import or builtin). The first construct outside the grammar is recorded
    in ``problem`` and the walk stops.
    """

    def __init__(self, aliases: Dict[str, str], bound: set, modules: set) -> None:
        self.aliases = aliases
        self.bound = bound
        self.pathlib = "pathlib" in modules
        self.problem: Optional[str] = None

    def fail(self, text: str) -> None:
        if self.problem is None:
            self.problem = text

    def visit(self, node: ast.AST) -> None:
        if self.problem is None:
            super().visit(node)

    def _is_module_alias(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in self.aliases
        )

    def _is_sys_stream(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Attribute) and node.attr == "buffer":
            node = node.value
        return (
            isinstance(node, ast.Attribute)
            and node.attr in ("stdout", "stderr")
            and self._is_module_alias(node.value)
            and self.aliases[node.value.id] == "sys"
        )

    def visit_AsyncFunctionDef(self, node: ast.AST) -> None:
        self.fail("async code")

    visit_AsyncFor = visit_AsyncWith = visit_Await = visit_AsyncFunctionDef

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        if node.id in self.aliases:
            self.fail(f"module alias {node.id!r} used as a value")
        elif node.id.startswith("__") and node.id != "__name__":
            self.fail(f"dunder name {node.id!r}")
        elif node.id in PY_OPEN_LIKE:
            self.fail(f"{node.id!r} referenced without being called")
        elif node.id not in PY_READONLY_BUILTINS and node.id not in self.bound:
            self.fail(f"name {node.id!r} is outside the read-only grammar")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        attr = node.attr
        if attr.startswith("_") and attr not in PY_PRIVATE_ATTRIBUTES_ALLOWED:
            self.fail(f"private attribute {attr!r}")
            return
        if attr in PY_OPEN_LIKE:
            # Only visit_Call reaches an open-like callee, and it never visits
            # the callee node itself: any open-like attribute seen here is a
            # value (`f = codecs.open`, `Z = zipfile.ZipFile`) whose later call
            # would escape the mode check.
            self.fail(f"{attr!r} referenced without being called")
            return
        if self._is_module_alias(node.value):
            module = self.aliases[node.value.id]
            if not _py_member_allowed(module, attr):
                self.fail(
                    f"attribute {attr!r} of module {module!r} is outside the "
                    "read-only grammar"
                )
            return
        if attr == "write" and self._is_sys_stream(node.value):
            self.visit(node.value)
            return
        if attr in PY_PATHLIB_CONFLICTS:
            if self.pathlib:
                self.fail(f"attribute {attr!r} with pathlib imported")
                return
        elif attr not in PY_READONLY_ATTRIBUTES:
            self.fail(f"attribute {attr!r} is outside the read-only grammar")
            return
        self.visit(node.value)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in PY_OPEN_LIKE:
            problem = _py_open_call_problem(node, method=False)
            if problem is not None:
                self.fail(problem)
                return
        elif isinstance(func, ast.Attribute) and func.attr in PY_OPEN_LIKE:
            module_function = self._is_module_alias(func.value)
            problem = _py_open_call_problem(node, method=not module_function)
            if problem is not None:
                self.fail(problem)
                return
            if module_function:
                module = self.aliases[func.value.id]
                if not _py_member_allowed(module, func.attr):
                    self.fail(
                        f"attribute {func.attr!r} of module {module!r} is "
                        "outside the read-only grammar"
                    )
                    return
            else:
                self.visit(func.value)
        else:
            self.visit(func)
        for arg in node.args:
            self.visit(arg)
        for keyword in node.keywords:
            self.visit(keyword.value)


def _python_code_problem(code: str) -> Optional[str]:
    """The first construct of ``code`` outside the read-only grammar, or None."""
    try:
        tree = ast.parse(code, mode="exec")
    except (SyntaxError, ValueError) as exc:
        return f"code does not parse: {getattr(exc, 'msg', None) or exc}"
    aliases: Dict[str, str] = {}
    bound: set = set()
    modules: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in PY_READONLY_MODULES:
                    return f"module {alias.name!r} is outside the read-only grammar"
                modules.add(alias.name)
                name = alias.asname or alias.name
                if "." in name:
                    return (
                        f"import binds {name.split('.')[0]!r}; use "
                        f"'import {alias.name} as <alias>'"
                    )
                aliases[name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                return "relative import"
            if node.module not in PY_READONLY_MODULES:
                return f"module {node.module!r} is outside the read-only grammar"
            modules.add(node.module)
            for alias in node.names:
                if alias.name == "*":
                    return "wildcard import"
                if not _py_member_allowed(node.module, alias.name):
                    return (
                        f"attribute {alias.name!r} of module {node.module!r} is "
                        "outside the read-only grammar"
                    )
                if alias.name in PY_OPEN_LIKE and alias.asname not in (None, alias.name):
                    return f"import renames {alias.name!r}"
                bound.add(alias.asname or alias.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif type(node).__name__ in ("MatchAs", "MatchStar", "MatchMapping"):
            for field in ("name", "rest"):
                value = getattr(node, field, None)
                if isinstance(value, str):
                    bound.add(value)
    rebound = sorted(set(aliases) & bound)
    if rebound:
        return f"module alias {rebound[0]!r} is rebound"
    grammar = _PythonReadOnlyGrammar(aliases, bound, modules)
    grammar.visit(tree)
    return grammar.problem


def _inline_code_is_static(segment: str) -> bool:
    """No ``$`` or backtick outside single quotes anywhere in the segment.

    Judged on the whole segment rather than the code word alone: the shell
    expands before the interpreter runs, and a word boundary the hook infers
    from quoting can differ from the one the shell applies.
    """
    quote: Optional[str] = None
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote is not None:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        if quote != "'" and char in ("$", "`"):
            return False
        index += 1
    return True


def _segment_file_redirect(segment: str) -> Optional[str]:
    """The first output redirect target that is a real file, if any.

    Descriptor duplication (``2>&1``) and ``/dev/null`` / ``/dev/stdout`` /
    ``/dev/stderr`` are not file writes. Process substitution is a segment
    of its own and is classified there.
    """
    for match in REDIRECT_TARGET_RE.finditer(_shell_syntax_view(segment)):
        raw_word = segment[match.start("word"):match.end("word")]
        if raw_word.startswith((">(", "<(")):
            continue
        try:
            words = _shell_tokens(raw_word)
        except ValueError:
            return raw_word
        if not words or not words[0]:
            continue
        word = words[0]
        if match.group("dup") and (word.isdigit() or word == "-"):
            continue
        if word in ("/dev/null", "/dev/stdout", "/dev/stderr"):
            continue
        return word
    return None


def _quoted_spans(segment: str) -> List[Tuple[int, int]]:
    """Offsets of every complete quoted string in a segment, quotes included.

    Backslash escapes outside single quotes are honoured; an unterminated
    quote yields no span (the segment has already failed the quote-aware
    scanner by then).
    """
    spans: List[Tuple[int, int]] = []
    quote: Optional[str] = None
    start = 0
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote is None:
            if char in ("'", '"'):
                quote = char
                start = index
        elif char == quote:
            spans.append((start, index + 1))
            quote = None
        index += 1
    return spans


def _pattern_operand_programs(tokens: List[str]) -> Optional[List[List[str]]]:
    """The argv of every read-only pattern program a segment runs, or None.

    Direct (``grep …``, after wrappers), under ``xargs``, or as every
    ``find -exec`` action — any other program in the chain disqualifies the
    segment. ``rg --pre`` executes a command and is excluded. awk and sed are
    admitted here and judged for inert scripts by the caller.
    """
    tokens, needs_ask, _assignments = _strip_wrappers(tokens)
    if needs_ask or not tokens:
        return None
    prog = Path(tokens[0]).name
    if prog == "xargs":
        nested = _xargs_nested_command(tokens)
        return _pattern_operand_programs(nested) if nested else None
    if prog == "find":
        actions = _find_action_commands(tokens)
        if not actions:
            return None
        programs: List[List[str]] = []
        for action in actions:
            nested = _pattern_operand_programs(action)
            if nested is None:
                return None
            programs.extend(nested)
        return programs
    if prog not in PATTERN_OPERAND_PROGRAMS:
        return None
    if prog == "rg" and any(
        word == "--pre" or word.startswith("--pre=") for word in tokens[1:]
    ):
        return None
    return [tokens]


def _awk_program(argv: List[str]) -> Optional[str]:
    """The awk program text from argv, or None when it comes from a file.

    ``-f`` / ``-e`` read the program elsewhere, ``-i`` / ``-l`` load code,
    ``-o`` / ``-d`` / ``-p`` write files, and an unknown option is not
    proven: all return None (not inert).
    """
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return argv[index + 1] if index + 1 < len(argv) else None
        if token in AWK_VALUE_OPTIONS:
            index += 2
            continue
        if (token.startswith(("-F", "-v")) and len(token) > 2) or token.startswith(
            ("--field-separator=", "--assign=")
        ):
            index += 1
            continue
        if token in AWK_PLAIN_OPTIONS or token.startswith("--lint="):
            index += 1
            continue
        if token.startswith("-"):
            return None
        return token
    return None


def _sed_scripts(argv: List[str]) -> Optional[Tuple[bool, List[str]]]:
    """``(quiet, scripts)`` from a sed argv, or None when not provable.

    None covers a script read from a file (``-f``), in-place editing
    (``-i``), and every option outside the plain set. The first positional
    is the script unless ``-e`` / ``--expression`` supplied one.
    """
    quiet = False
    expressions = False
    scripts: List[str] = []
    index = 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            if not expressions and not scripts and index + 1 < len(argv):
                scripts.append(argv[index + 1])
            break
        if token in ("-e", "--expression"):
            if index + 1 >= len(argv):
                return None
            scripts.append(argv[index + 1])
            expressions = True
            index += 2
            continue
        if token.startswith("--expression="):
            scripts.append(token.split("=", 1)[1])
            expressions = True
            index += 1
            continue
        if token in ("-n", "--quiet", "--silent"):
            quiet = True
            index += 1
            continue
        if token in SED_PLAIN_OPTIONS:
            index += 1
            continue
        if token in ("-l", "--line-length"):
            index += 2
            continue
        if token.startswith("--line-length=") or (token.startswith("-l") and token[2:].isdigit()):
            index += 1
            continue
        if token.startswith("--"):
            return None
        if token.startswith("-") and len(token) > 1:
            letters = token[1:]
            if letters.endswith("e") and set(letters[:-1]) <= SED_CLUSTER_LETTERS:
                if index + 1 >= len(argv):
                    return None
                quiet = quiet or "n" in letters
                scripts.append(argv[index + 1])
                expressions = True
                index += 2
                continue
            if set(letters) <= SED_CLUSTER_LETTERS:
                quiet = quiet or "n" in letters
                index += 1
                continue
            return None
        if not expressions and not scripts:
            scripts.append(token)
        index += 1
    if not scripts:
        return None
    return quiet, scripts


def _sed_delimited_end(script: str, start: int, delimiter: str) -> Optional[int]:
    """Index just past the closing ``delimiter`` of a field starting at ``start``."""
    index = start
    while index < len(script):
        char = script[index]
        if char == "\\":
            index += 2
            continue
        if char == delimiter:
            return index + 1
        index += 1
    return None


def _sed_script_is_inert(script: str) -> bool:
    """Whether a sed script is built only from addresses and inert commands.

    Addresses (numbers, ``$``, ``/regex/`` with ``I``/``M`` flags, ranges,
    steps, ``!``), groups, the print-class commands in SED_SIMPLE_COMMANDS,
    ``s`` with ``g p i I m M`` and numeric flags, ``y``, and comments pass.
    ``w``/``W`` (write), ``e``/``E`` (execute), ``r``/``R`` (read a file),
    ``a``/``i``/``c`` (text), branches, labels and custom-delimiter addresses
    are outside the grammar, as is anything the scanner cannot place.
    """
    index = 0
    length = len(script)
    while index < length:
        char = script[index]
        if char in " \t\n;{}!,$~" or char.isdigit():
            index += 1
            continue
        if char == "#":
            newline = script.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue
        if char == "/":
            end = _sed_delimited_end(script, index + 1, "/")
            if end is None:
                return False
            index = end
            while index < length and script[index] in "IM":
                index += 1
            continue
        if char in SED_SIMPLE_COMMANDS:
            index += 1
            continue
        if char in "sy":
            if index + 1 >= length:
                return False
            delimiter = script[index + 1]
            if delimiter.isalnum() or delimiter in " \t\n\\":
                return False
            end = _sed_delimited_end(script, index + 2, delimiter)
            if end is None:
                return False
            end = _sed_delimited_end(script, end, delimiter)
            if end is None:
                return False
            index = end
            if char == "s":
                while index < length and script[index] in "gpiImM0123456789":
                    index += 1
            continue
        return False
    return True


def _pattern_scripts_are_inert(argv: List[str]) -> bool:
    """Whether a pattern program's script operand can run nothing and write nothing."""
    prog = Path(argv[0]).name
    if prog in ("awk", "gawk", "mawk", "nawk"):
        program = _awk_program(argv)
        return program is not None and not AWK_SCRIPT_ESCAPE_RE.search(program)
    if prog == "sed":
        parsed = _sed_scripts(argv)
        if parsed is None:
            return False
        quiet, scripts = parsed
        return quiet and all(_sed_script_is_inert(script) for script in scripts)
    return True


def _inert_pattern_spans(segment: str) -> List[Tuple[int, int]]:
    """Segment-local offsets of quoted operands that are inert pattern data.

    The segment must run a read-only pattern program (directly or nested)
    whose awk / sed script, extracted from argv, proves inert. A quoted
    operand counts when it is literal — single quotes, or double quotes with
    no live expansion or backtick (a ``$(…)`` inside the quotes executes to
    form the argument and is not data) — and is not part of a shell word
    that is an environment assignment (``X='rm'"…"`` is one word). Offsets
    are exact, so the same text elsewhere in the command earns nothing from
    this segment.
    """
    try:
        tokens = _shell_tokens(segment)
    except ValueError:
        return []
    programs = _pattern_operand_programs(tokens)
    if not programs or not all(_pattern_scripts_are_inert(argv) for argv in programs):
        return []
    # Word separators are whitespace the shell sees as syntax; whitespace inside
    # a quoted piece of the same word (`X=' '"pattern"`) separates nothing.
    separators = [
        start
        for start, end, kind in _shell_spans(segment)
        if kind == "syntax" and segment[start:end].isspace()
    ]
    spans: List[Tuple[int, int]] = []
    for start, end in _quoted_spans(segment):
        text = segment[start:end]
        if text[0] == '"' and LIVE_EXPANSION_RE.search(text):
            continue
        word_start = max([0] + [sep + 1 for sep in separators if sep < start])
        if re.match(r"^\w+=", segment[word_start:start]):
            continue
        spans.append((start, end))
    return spans


def _destructive_token_deny(label: str) -> Decision:
    return _deny(
        f"destructive command token {label!r} is outside the "
        "envelope (destructive_ops: false)"
    )


def _destructive_token_decision(command: str) -> Optional[Decision]:
    """The destructive-token deny, skipping tokens that are pattern data.

    Every match of a deny-listed token is judged exactly once, in the
    innermost shell segment that contains it directly (a match inside a
    command or process substitution belongs to that substitution's own
    segment). It passes only when it lies inside an inert quoted pattern
    operand of that segment (``grep``, ``rg``, ``awk``, ``sed -n``; directly
    or under ``xargs`` / ``find -exec``). A match the segmenter never sees —
    a heredoc body — denies through the count: every match in the raw
    command must have been judged.
    """
    hits = [label for label, pattern in DESTRUCTIVE_PATTERNS for _m in pattern.finditer(command)]
    if not hits:
        return None
    try:
        segments = _segments_of(_strip_heredoc_bodies(command))
    except ValueError:
        return _destructive_token_deny(hits[0])
    judged = 0
    for segment in segments:
        opaque = [
            (start, end)
            for start, end, kind in _shell_spans(segment)
            if kind in ("substitution", "process")
        ]
        inert = _inert_pattern_spans(segment)
        for label, pattern in DESTRUCTIVE_PATTERNS:
            for match in pattern.finditer(segment):
                if any(start <= match.start() and match.end() <= end for start, end in opaque):
                    continue
                judged += 1
                if not any(start <= match.start() and match.end() <= end for start, end in inert):
                    return _destructive_token_deny(label)
    if judged != len(hits):
        return _destructive_token_deny(hits[0])
    return None


def _xargs_nested_command(tokens: List[str]) -> Optional[List[str]]:
    """The argv xargs will run, or None when it falls back to ``echo``."""
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1:] or None
        if not token.startswith("-"):
            return tokens[index:]
        index += 2 if token in XARGS_VALUE_OPTIONS else 1
    return None


def _find_action_commands(tokens: List[str]) -> Optional[List[List[str]]]:
    """The argv of every ``-exec`` / ``-execdir`` predicate.

    None when a predicate the classifier cannot see through is present:
    ``-ok`` / ``-okdir`` (interactive), ``-delete``, or an ``-exec`` with no
    ``;`` / ``+`` terminator.
    """
    commands: List[List[str]] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in ("-ok", "-okdir", "-delete"):
            return None
        if token in ("-exec", "-execdir"):
            end = index + 1
            while end < len(tokens) and tokens[end] not in (";", "+"):
                end += 1
            if end >= len(tokens):
                return None
            commands.append(tokens[index + 1:end])
            index = end + 1
            continue
        index += 1
    return commands


def _strip_subshell_parens(tokens: List[str]) -> List[str]:
    """Drop the parentheses of a subshell group around a segment.

    ``( cmd )``, ``(cmd)`` and the halves of a group split at an operator
    run the words inside them, so those words are judged: a ``(`` that
    opens a group is dropped whether it stands alone or is glued to the
    first word, and a ``)`` that closes one likewise. ``$(``, ``<(``, the
    arithmetic ``((`` and a function name's ``()`` are not groups and keep
    their tokens.
    """
    while tokens and tokens[0] == "(":
        tokens = tokens[1:]
    if tokens and tokens[0].startswith("(") and not tokens[0].startswith("(("):
        tokens = [tokens[0][1:]] + tokens[1:]
    while tokens and tokens[-1] == ")":
        tokens = tokens[:-1]
    if (
        tokens
        and tokens[-1].endswith(")")
        and not tokens[-1].endswith("))")
        and "(" not in tokens[-1]
    ):
        tokens = tokens[:-1] + [tokens[-1][:-1]]
    return tokens


def _strip_syntax_head(tokens: List[str]) -> Tuple[List[str], Optional[str]]:
    """Drop the shell syntax at a segment's head; the words after it run.

    Shell syntax words carry no command of their own (``if cmd``, ``then
    cmd``, ``! cmd``, ``{ cmd``, ``do cmd``, a case pattern ``word)``, the
    ``case WORD in`` opener), and neither does a function header. The header
    grammar is ``[function] NAME [()] [BODY]``: NAME is any word free of
    shell metacharacters, the parentheses may be glued to NAME or spaced
    (``f()``, ``f ()``, ``f ( )``), BODY is a ``{`` or ``(`` group that may
    be glued to the parentheses (``f(){ cmd``, ``f()(cmd``) or follow later,
    and the keyword form takes a body without parentheses (``function f {
    cmd``). ``coproc`` runs its command, named or not. A ``{`` glued to a
    word that closes within the token (``{gh,pr}``) is a brace expansion,
    not a group, and stays for the program-name check. Returns the remaining
    tokens and, for a ``function`` keyword followed by anything the grammar
    cannot place, the reason to ask. The inline classifier and the script
    rule share this one grammar.
    """
    while tokens:
        head = tokens[0]
        if head == "function":
            # `function NAME`, `function NAME()`, `function NAME(){cmd`,
            # `function NAME()(cmd`: drop the keyword and the name, keep
            # whatever the name token carries after its `()`; the
            # following iterations place `(`, `)`, `{` and glued openers.
            name = tokens[1] if len(tokens) > 1 else ""
            glued = GLUED_FUNCTION_HEADER_RE.match(name)
            bare, opener, body = name.partition("(")
            if glued:
                rest = glued.group(1)
                tokens = ([rest] if rest else []) + tokens[2:]
            elif FUNCTION_NAME_RE.match(name):
                tokens = tokens[2:]
            elif opener and FUNCTION_NAME_RE.match(bare):
                # `function NAME(cmd …)`: a subshell body glued to the
                # name, which bash accepts without parentheses.
                tokens = [opener + body] + tokens[2:]
            else:
                return tokens, "cannot classify function definition"
            continue
        if head == "coproc":
            # `coproc cmd …` runs cmd; `coproc NAME { cmd; }` names the
            # coprocess and runs the group.
            if (
                len(tokens) > 2
                and FUNCTION_NAME_RE.match(tokens[1])
                and tokens[2][0] in "{("
            ):
                tokens = tokens[2:]
            else:
                tokens = tokens[1:]
            continue
        if head == "case" and len(tokens) > 2 and tokens[2] == "in":
            # `case WORD in pattern) cmd`: the opener is data, the pattern a
            # syntax word, the command after it runs.
            tokens = tokens[3:]
            continue
        glued = GLUED_FUNCTION_HEADER_RE.match(head)
        if glued:
            # `name()`, `name(){`, `name()(`, `name(){cmd`: the header
            # ends at the `()`; whatever is glued after it is judged.
            rest = glued.group(1)
            tokens = ([rest] if rest else []) + tokens[1:]
            continue
        if (
            len(tokens) > 1
            and FUNCTION_NAME_RE.match(head)
            and tokens[1].startswith("(")
            and not tokens[1].startswith("$(")
        ):
            # `name () body` and `name (){ body`: the parens belong to
            # the header, whatever they are glued to.
            tokens = [tokens[1].lstrip("()")] + tokens[2:]
            if not tokens[0]:
                tokens = tokens[1:]
            continue
        if (
            len(head) > 1
            and head[0] in "{()"
            and not head.startswith(("$(", "((", "<(", ">("))
            and not (head[0] == "{" and "}" in head)
        ):
            # `{cmd` / `(cmd` / `){`: a group delimiter glued to the
            # word after it.
            tokens = [head[1:]] + tokens[1:]
            continue
        if head in SCRIPT_SYNTAX_WORDS or (head.endswith(")") and "(" not in head):
            tokens = tokens[1:]
            continue
        break
    return tokens, None


def _classify_segment(
    tokens: List[str],
    segment: str,
    cwd: str,
    constraints: Dict[str, Any],
    write_targets: List[str],
    context: Optional[WorkspaceContext] = None,
    depth: int = 0,
    env_assignments: Optional[List[str]] = None,
    standalone: bool = False,
) -> Optional[Decision]:
    """Classify one segment's tokens; return a non-allow Decision or None.

    Write targets are collected BEFORE program dispatch so a redirect on a
    recognized program (`gh pr view > .env`) still reaches the shared write
    gate (F-005). Known runners recurse into their nested command (F-007).
    Env assignments stripped from the segment (or inherited from an outer
    runner) accumulate so target-sensitive classification can inspect them.
    A subshell group's parentheses are dropped first: ``( gh pr merge 7 )``
    runs the words inside them. Shell syntax at the head — a keyword, a
    function header, a case pattern — is dropped next and the words after
    it judged (_strip_syntax_head), in turns with the wrappers until the
    head is a program (``time ! git commit`` drops ``time``, then ``!``),
    so ``if true; then gh pr merge 7; fi`` denies as ``gh pr merge 7``
    does; a program name the shell would produce by expansion or
    substitution asks.
    """
    if depth > 3:
        return _ask(f"cannot classify deeply nested command: {segment!r}")

    tokens = _strip_subshell_parens(tokens)
    env_assignments = list(env_assignments or [])
    while True:
        # Syntax and wrappers compose — `time ! git commit`, `time if cmd;
        # …`, `! time cmd` — so the two strips take turns until neither
        # removes anything and the head is the program itself.
        tokens, head_problem = _strip_syntax_head(tokens)
        if head_problem is not None:
            return _ask(f"{head_problem}: {segment!r}")
        if not tokens or tokens[0] in SCRIPT_DATA_KEYWORDS:
            # A `for`/`select`/`case` opener iterates data (a substitution
            # in its list is judged as its own segment); the command it
            # guards is a later segment, judged there.
            return None
        if tokens[0] == "trap":
            return _ask(f"trap runs code the classifier cannot see: {segment!r}")
        before = len(tokens)
        tokens, wrapper_needs_ask, stripped = _strip_wrappers(tokens)
        env_assignments += stripped
        if wrapper_needs_ask:
            return _ask(f"cannot classify wrapper invocation: {segment!r}")
        if not tokens:
            return None
        if len(tokens) == before:
            break

    head = tokens[0]
    if (
        head not in TEST_BUILTINS
        and not head.startswith(("((", "<(", ">("))
        and re.search(r"[$`(){}*?\[\]]", head)
    ):
        # The shell produces this program name by expansion or substitution
        # after classification, so its literal spelling names no program the
        # hook can judge. A test or an arithmetic command runs nothing but
        # its own evaluation, and a process-substitution fragment (the
        # segment splitter divides `<(a | b)` at its pipe) is a filename
        # argument whose body is judged as its own segment.
        return _ask(f"cannot resolve shell program name: {head!r}")

    # Redirect targets are a property of the segment, writer-program targets
    # of the argv — both must be gated regardless of which branch handles
    # the program below (only collect segment redirects once, at depth 0).
    write_targets.extend(
        _segment_write_targets(tokens, segment if depth == 0 else "")
    )

    prog = tokens[0].rsplit("/", 1)[-1]

    if prog == "sudo":
        return _ask(f"cannot classify privileged command: {segment!r}")
    if prog == "xargs":
        # The program xargs runs is visible in argv; a plain read-only
        # utility stays a read whatever operands stdin supplies. No program
        # (the echo default) or any other program keeps the ask.
        nested = _xargs_nested_command(tokens)
        if nested is not None and _is_readonly_program(nested):
            return None
        return _ask(f"cannot classify shell indirection: {segment!r}")
    if prog in SHELL_INDIRECTION:
        if prog in SCRIPT_SHELLS:
            script_decision = _classify_script_invocation(
                tokens, cwd, constraints, env_assignments
            )
            if script_decision is not None:
                if script_decision.action == "allow":
                    return None
                return script_decision
        return _ask(f"cannot classify shell indirection: {segment!r}")
    if prog == "command":
        # Only the lookup form survives _strip_wrappers with `command` as
        # the program; it resolves a name and runs nothing.
        if _is_command_lookup(tokens):
            return None
        return _ask(f"cannot classify wrapper invocation: {segment!r}")

    if prog in INTERPRETER_CODE_FLAGS:
        inline = _inline_code_decision(prog, tokens, segment)
        if inline is ALLOW:
            return None
        if inline is not None:
            return inline

    runner_verb = RUNNER_VERBS.get(prog)
    nested: Optional[List[str]] = None
    if prog in DIRECT_RUNNERS:
        nested = tokens[1:]
    elif runner_verb is not None and len(tokens) > 1 and tokens[1] == runner_verb:
        nested = tokens[2:]
    if nested is not None:
        if not nested or nested[0].startswith("-"):
            return _ask(f"cannot classify runner invocation: {segment!r}")
        return _classify_segment(
            nested,
            segment,
            cwd,
            constraints,
            write_targets,
            context,
            depth + 1,
            env_assignments,
            standalone,
        )

    if prog == "oacp":
        decision = _classify_oacp(
            tokens, context, cwd, env_assignments, standalone
        )
        if decision.action != "allow":
            return decision
        return None
    if prog in ("cp", "mv", "install") and not constraints.get(
        "touches_auth_config_or_secrets"
    ):
        # Sources too: copying a secret out is as bad as writing one.
        for arg in tokens[1:]:
            if not arg.startswith("-") and is_secret_path(arg):
                return _deny(
                    f"command touching secret-class path {arg!r} is outside "
                    "the envelope (touches_auth_config_or_secrets: false)"
                )
    if prog in FS_MUTATORS and context is not None:
        # Operand gate, role-agnostic: deleting, relocating, copying out, or
        # aliasing an authority surface is self-modification just as much as
        # writing it, and the same for trust-root material under the auth
        # gate — the write-target gate alone sees only destinations. The
        # authority roots come from the same list the write-target gate
        # consumes, so a surface can never be protected against writes but
        # left open to `rm`/`mv`. The operands judged must be the ones the
        # utility will actually use: GNU target-directory spellings are
        # parsed, `--` ends option processing, and expansion syntax escalates
        # (the shell expands it after classification, so its literal
        # spelling proves nothing).
        authority_roots = [
            (label, os.path.realpath(str(root)))
            for label, root in context.authority_roots()
        ]
        trust_roots = [os.path.realpath(str(r)) for r in context.trust_roots()]
        for operand in _mutator_operands(tokens):
            if EXPANSION_SYNTAX_RE.search(operand) or "$" in operand:
                return _ask(
                    f"{prog} operand {operand!r} contains shell expansion or "
                    "variable syntax that cannot be statically resolved; use "
                    "explicit literal paths"
                )
            canonical = os.path.realpath(_normalize_file_path(operand, cwd))
            for label, root in authority_roots:
                if _within(canonical, root):
                    return _deny(
                        f"{prog} touching {label} {operand!r} from inside "
                        "the enveloped session is authority "
                        "self-modification"
                    )
            if not constraints.get("touches_auth_config_or_secrets"):
                for root in trust_roots:
                    if _within(canonical, root):
                        return _deny(
                            f"{prog} touching trust root {operand!r} is "
                            "outside the envelope (authority-bearing auth "
                            "config; touches_auth_config_or_secrets: false)"
                        )
    if prog == "find":
        # An action predicate runs a nested program per match: a plain
        # read-only utility stays a read; a shell, a mutator, and the
        # interactive and delete forms ask.
        actions = _find_action_commands(tokens)
        if actions is None or not all(_is_readonly_program(a) for a in actions):
            return _ask(
                f"cannot classify find with action predicates: {segment!r}"
            )
    if prog == "git":
        subcommand, args, git_dir = _git_subcommand(tokens)
        base_dir = git_dir or cwd
        if not os.path.isabs(base_dir):
            base_dir = os.path.normpath(os.path.join(cwd, base_dir))
        if subcommand == "push":
            decision = _classify_git_push(args, base_dir, constraints)
            if decision.action != "allow":
                return decision
        elif subcommand == "commit":
            if not constraints.get("commits_changes"):
                return _deny(
                    "git commit is outside the envelope "
                    "(commits_changes: false)"
                )
        return None
    if prog == "gh":
        decision = _classify_gh(
            tokens,
            cwd,
            constraints,
            env_assignments=env_assignments,
            standalone=standalone,
        )
        if decision.action != "allow":
            return decision
        return None

    dependency = _classify_dependency_command(prog, tokens, constraints)
    if dependency is not None and dependency.action != "allow":
        return dependency
    return None


def _read_script(path: str) -> Optional[bytes]:
    """The bytes of a script up to SCRIPT_READ_LIMIT; None when unreadable
    or larger than that."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(SCRIPT_READ_LIMIT + 1)
    except OSError:
        return None
    if len(data) > SCRIPT_READ_LIMIT:
        return None
    return data


def _trusted_interpreter(token: str) -> bool:
    """Whether the program ``token`` names — a shell for the script rule, a
    wrapper for _strip_wrappers — is spelled where the hook may trust it.

    A bare name (``bash``) resolves through PATH like every other program
    the hook classifies. A path must spell a system bin directory
    (INTERPRETER_BIN_DIRS, after ``..`` normalization) and resolve, through
    any symlinks, inside the system trees (INTERPRETER_REAL_PREFIXES): a
    scratch, repository or relative ``bin/bash`` is an executable that
    merely shares a shell's name, and a pin on the script says nothing
    about it.
    """
    if "/" not in token:
        return True
    if not token.startswith("/"):
        return False
    spelled = os.path.normpath(token)
    if os.path.dirname(spelled) not in INTERPRETER_BIN_DIRS:
        return False
    return os.path.realpath(spelled).startswith(INTERPRETER_REAL_PREFIXES)


def _classify_script_invocation(
    tokens: List[str],
    cwd: str,
    constraints: Dict[str, Any],
    env_assignments: Optional[List[str]],
) -> Optional[Decision]:
    """``<shell> <literal script> [args…]``: a pinned script passes, any
    other literal script asks as not registered.

    Returns None when the call is not that shape — an interpreter named by
    a path outside the system bin directories (_trusted_interpreter), a
    ``-c`` body, a shell flag, a variable or missing path — so the caller
    keeps the shell-indirection ask. The receiver's ``trusted_scripts`` pins
    (path + sha256, carried into the envelope by the compiler) pass whatever
    the script does, with any arguments, under an env-assignment prefix
    limited to the names the pin lists: the pin is the human's review of
    those exact bytes, and a changed file asks to be re-pinned. The hook
    never reads an unpinned script's text: registering the pin is the one
    review a script version gets (the receiver config is the human's to
    edit and sign), so the unpinned ask names that step rather than the
    first construct a parse could not follow.
    """
    if len(tokens) < 2 or not _trusted_interpreter(tokens[0]):
        return None
    operand = tokens[1]
    if operand.startswith("-") or "$" in operand or "`" in operand:
        return None
    if EXPANSION_SYNTAX_RE.search(operand):
        return None
    script = os.path.realpath(_normalize_file_path(operand, cwd))
    if not os.path.isfile(script):
        return None
    prefix = f"script {operand!r}"

    for entry in constraints.get("trusted_scripts") or []:
        if not isinstance(entry, dict):
            continue
        pinned = os.path.realpath(os.path.expanduser(str(entry.get("path") or "")))
        if pinned != script:
            continue
        data = _read_script(script)
        if data is None:
            return _ask(
                f"{prefix} is pinned but unreadable or over {SCRIPT_READ_LIMIT} bytes"
            )
        if hashlib.sha256(data).hexdigest() != str(entry.get("sha256") or ""):
            return _ask(
                f"{prefix} does not match its pinned sha256; review the change "
                "and re-pin it in the receiver's trusted_scripts"
            )
        allowed_env = {str(name) for name in entry.get("env") or []}
        for assignment in env_assignments or []:
            name = assignment.partition("=")[0]
            if name not in allowed_env:
                return _ask(
                    f"{prefix} is pinned without the env assignment {name!r}; "
                    "list it in the pin's env or drop the prefix"
                )
        return ALLOW

    return _ask(
        f"{prefix} is not registered under the receiver's trusted_scripts; "
        "pin it with oacp trust pin-script or run its commands directly"
    )


def _token_minting_compound(command: str) -> Optional[str]:
    """The final ``gh`` segment of an App-identity token-minting compound.

    The App-identity recipe captures a token first and chains with ``&&``:
    ``T=$(<path>/<token-helper> <app>) && GH_TOKEN="$T" gh …``. Every segment
    before the last is such a mint, every join is ``&&`` (a ``;`` would run
    gh on an empty token), and the last segment is ``gh`` behind nothing but
    ``GH_TOKEN`` set from a minted name. Nothing in that prefix can retarget
    gh — no ``cd``, no export, no ``GH_REPO``/``GH_HOST`` — so the caller
    judges the final segment as the standalone command it is. Any other
    shape returns None and keeps the compound rule.
    """
    try:
        parts = _split_shell_segments_with_operators(command)
    except ValueError:
        return None
    if len(parts) < 2 or parts[-1][1] != "":
        return None
    names = set()
    for segment, operator in parts[:-1]:
        if operator != "&&":
            return None
        match = MINT_ASSIGNMENT_RE.match(segment)
        if match is None:
            return None
        names.add(match.group(1))
    final = parts[-1][0]
    try:
        tokens = _shell_tokens(final)
    except ValueError:
        return None
    rest, needs_ask, assignments = _strip_wrappers(tokens)
    if needs_ask or not rest or rest[0].rsplit("/", 1)[-1] != "gh":
        return None
    for assignment in assignments:
        name, _, value = assignment.partition("=")
        reference = value[1:] if value.startswith("$") else ""
        if reference.startswith("{") and reference.endswith("}"):
            reference = reference[1:-1]
        if name != "GH_TOKEN" or reference not in names:
            return None
    return final


def classify_bash(
    command: str,
    cwd: str,
    constraints: Dict[str, Any],
    counters: Dict[str, Any],
    context: Optional[WorkspaceContext] = None,
    *,
    sandboxed: bool = True,
) -> Decision:
    if not constraints.get("destructive_ops"):
        decision = _destructive_token_decision(command)
        if decision is not None:
            return decision

    write_targets: List[str] = []
    try:
        stripped = _strip_heredoc_bodies(command)
        segments = _segments_of(stripped)
    except ValueError as error:
        return _ask(f"cannot segment shell command ({error}): {command!r}")
    # A command with exactly one segment has no earlier shell state (cd,
    # export) that could retarget a target-sensitive subcommand after
    # validation — the completion clear is sanctioned only in that form.
    standalone = len(segments) == 1
    # The token-minting compound is the one multi-segment shape whose earlier
    # segments provably cannot retarget the gh command they precede; its
    # final segment is judged as standalone.
    minted_final = None if standalone else _token_minting_compound(stripped)
    for segment in segments:
        try:
            tokens = _shell_tokens(segment)
        except ValueError:
            if _degraded_readonly_segment(segment):
                continue
            return _ask(f"cannot classify shell segment: {segment!r}")
        decision = _classify_segment(
            tokens,
            segment,
            cwd,
            constraints,
            write_targets,
            context,
            standalone=standalone or segment == minted_final,
        )
        if decision is not None:
            return decision

    gated_targets: List[str] = []
    tmpdir_resolvable = sandboxed and sandbox_tmpdir_is_unmodified(command, stripped)
    for target in write_targets:
        # The harness-named scratchpad is the one variable spelling the hook
        # can resolve; from here on it is a literal path and takes the
        # bookkeeping exemption like any other scratchpad write.
        resolved = resolve_sandbox_tmpdir_target(target, tmpdir_resolvable)
        if resolved is not None:
            gated_targets.append(resolved)
            continue
        # A target spelled through a shell variable or substitution takes
        # its value from shell state the classifier cannot see; escalate
        # instead of normalizing the unexpanded spelling to a cwd-relative
        # phantom path (which both miscounts the budget and can hide a
        # real write elsewhere).
        if "$" in target or "`" in target:
            return _ask(
                f"write target {target!r} references a shell variable or "
                "substitution that cannot be statically resolved; use a "
                "literal path"
            )
        # Bash-derived targets are shell-expanded after classification —
        # a pattern can reach a path its literal spelling does not. File-tool
        # paths (Edit/Write) never pass here and may contain literal brackets.
        if EXPANSION_SYNTAX_RE.search(target):
            return _ask(
                f"write target {target!r} contains shell expansion syntax "
                "that cannot be statically resolved; use explicit literal "
                "paths"
            )
        gated_targets.append(target)
    return _gate_write_paths(
        gated_targets, cwd, constraints, counters, "write", context
    )


# ── File-tool classification ──────────────────────────────────────────────────


def classify_file_write(
    file_path: str,
    cwd: str,
    constraints: Dict[str, Any],
    counters: Dict[str, Any],
    context: Optional[WorkspaceContext] = None,
    tool_name: str = "",
    tool_input: Optional[Dict[str, Any]] = None,
) -> Decision:
    dependency_exemption: Optional[Callable[[str], bool]] = None
    if tool_input is not None and tool_name in ("Edit", "Write"):
        def version_exemption(normalized: str) -> bool:
            return _is_version_only_pyproject_edit(
                tool_name, tool_input, normalized
            )

        dependency_exemption = version_exemption
    return _gate_write_paths(
        [file_path],
        cwd,
        constraints,
        counters,
        "edit",
        context,
        dependency_exemption,
    )


# ── Dispatch + I/O ────────────────────────────────────────────────────────────


def classify(
    tool_name: str,
    tool_input: Dict[str, Any],
    cwd: str,
    envelope: Dict[str, Any],
    context: Optional[WorkspaceContext] = None,
) -> Decision:
    constraints = envelope.get("constraints") or {}
    counters = envelope.get("counters") or {}

    if tool_name == "Bash":
        return classify_bash(
            str(tool_input.get("command") or ""),
            cwd,
            constraints,
            counters,
            context,
            # The harness's per-call sandbox opt-out is the only sandbox
            # signal a hook receives; absent, the call runs sandboxed.
            sandboxed=tool_input.get("dangerouslyDisableSandbox") is not True,
        )
    if tool_name in ("Edit", "Write"):
        file_path = str(tool_input.get("file_path") or "")
        if not file_path:
            return ALLOW
        return classify_file_write(
            file_path,
            cwd,
            constraints,
            counters,
            context,
            tool_name,
            tool_input,
        )
    if tool_name == "NotebookEdit":
        notebook = str(tool_input.get("notebook_path") or "")
        if not notebook:
            return ALLOW
        return classify_file_write(notebook, cwd, constraints, counters, context)
    return ALLOW


def emit(decision: Decision) -> None:
    if decision.action == "allow":
        # Silence keeps the harness's own permission flow authoritative; the
        # envelope must never widen it.
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision.action,
                    "permissionDecisionReason": decision.reason,
                }
            }
        )
    )


def _compile_message_name(payload: Dict[str, Any]) -> Optional[str]:
    """Return the message filename when this tool call runs an envelope compile.

    Recognizes ``oacp envelope compile <message> ...`` (and the script-path
    spelling ``... envelope_compiler.py compile <message> ...``) anywhere in a
    Bash command. Best-effort by design: a miss degrades to an unbound
    envelope, never to a wrong binding — the compiler still checks the
    claimed filename against the message it actually compiles.
    """
    if str(payload.get("tool_name") or "") != "Bash":
        return None
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if "compile" not in command:
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    # Compile options that consume a value: their value token must never be
    # mistaken for the positional message (mirrors the compiler's argparse
    # surface — keep in sync with envelope_compiler.py's compile subparser).
    value_options = {"--receiver", "--project", "--oacp-dir", "--config", "--audit"}
    for index, token in enumerate(tokens[:-1]):
        # Front ends for the same compile operation: the `oacp` executable
        # (any path spelling), the module CLI (`python3 -m oacp.cli`), and
        # the compiler script invoked directly.
        is_cli = (
            token == "envelope"
            and index > 0
            and tokens[index - 1].endswith(("oacp", "oacp.cli"))
        )
        is_script = token.endswith("envelope_compiler.py")
        if not (is_cli or is_script):
            continue
        rest = tokens[index + 1 :]
        if not rest or rest[0] != "compile":
            continue
        skip_next = False
        for operand in rest[1:]:
            if skip_next:
                skip_next = False
                continue
            if operand.startswith("-"):
                if operand in value_options:
                    skip_next = True
                continue
            return Path(operand).name
    return None


def _shared_state_affinity(
    payload: Dict[str, Any], state_root: str, cwd: str
) -> str:
    """Classify a foreign-session tool call's relation to shared envelope state.

    Returns ``"affine"`` when the call names the shared envelope state that
    protects the bound session (an ``oacp envelope`` invocation, a protected
    state filename anywhere in the command, or an operand resolving into the
    receiver's ``state/`` directory — through any symlinked spelling),
    ``"uncertain"`` when the target cannot be determined statically
    (unresolved shell expansion, substitution, unparseable input), and
    ``"clear"`` for ordinary foreign work. Callers deny mutation-capable
    affine calls outright — the classifier itself allows some mutation
    spellings (e.g. plain ``rm`` with a ``$VAR`` operand), so falling
    through is not a safe backstop — but keep pre-envelope behavior for
    provably read-only inspection (``_readonly_state_inspection``), keep
    uncertain calls enforced without budget accounting, and bypass only
    clear ones.
    """
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    # Compare against the canonical spelling of the state root: OACP homes
    # reached through a symlink otherwise never match a canonicalized
    # candidate, and the guard would wave the real path through.
    state_root = os.path.realpath(state_root)
    if tool_name in ("Write", "Edit", "NotebookEdit"):
        file_path = str(
            tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        )
        if not file_path:
            return "uncertain"
        canonical = os.path.realpath(_normalize_file_path(file_path, cwd))
        return "affine" if _within(canonical, state_root) else "clear"
    if tool_name != "Bash":
        return "clear"
    command = str(tool_input.get("command") or "")
    lowered = command.lower()
    if "envelope" in lowered and (
        "oacp" in lowered or "envelope_compiler" in lowered or "oacp-envelope" in lowered
    ):
        return "affine"
    # Protected state filenames anywhere in the raw command — catches bare
    # names after a `cd`, operands inside substitutions, and mv/rm targets.
    if ENVELOPE_FILENAME in command or SESSION_CLAIM_STEM in command:
        return "affine"
    if state_root in command:
        return "affine"
    parse_failed = False
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = []
        parse_failed = True
    for token in tokens:
        # An env-assignment token can smuggle the path in its value part.
        value = token.split("=", 1)[1] if ENV_ASSIGNMENT_RE.match(token) else token
        candidate = os.path.realpath(_normalize_file_path(value, cwd))
        if _within(candidate, state_root):
            return "affine"
    if (
        parse_failed
        or "$" in command
        or "`" in command
        or EXPANSION_SYNTAX_RE.search(command)
    ):
        return "uncertain"
    return "clear"


# Commands that cannot mutate or execute anything when output redirection,
# process substitution, and shell expansion are all absent (each is ruled
# out before this set is consulted). Membership requires the tool to have
# no write mode AND no command-execution flag in any common implementation:
# `sed` (-i), `sort` (-o), `find` (-delete/-exec), `tee`, `rg` (--pre),
# `less`/`more` (shell escapes, `less -o`), and `file` (-C writes a magic
# cache) all stay out.
READONLY_INSPECTION_COMMANDS = {
    "cat",
    "cd",
    "cmp",
    "diff",
    "echo",
    "grep",
    "head",
    "jq",
    "ls",
    "pwd",
    "stat",
    "tail",
    "test",
    "wc",
}


def _readonly_state_inspection(payload: Dict[str, Any]) -> bool:
    """True when an affine Bash call is provably read-only inspection.

    State relation is not the same as mutation capability: a foreign
    session reading shared state (``oacp envelope show``, ``cat``/``stat``
    on state files) cannot disturb the bound session, so it keeps
    pre-envelope behavior. Conservative by construction — any parse
    failure, unresolved expansion, redirection, process substitution, or
    command outside a small inspection allowlist means the affine deny
    stands: the exemption must prove the complete command cannot spawn,
    embed, or execute a mutation.
    """
    if str(payload.get("tool_name") or "") != "Bash":
        return False
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if not command.strip():
        return False
    if "$" in command or "`" in command or ">" in command:
        return False
    # Process substitution executes its body: `cat <(rm state)` mutates
    # under a read-only head. (`>(...)` is already caught by the `>` check.)
    if "<(" in command:
        return False
    if EXPANSION_SYNTAX_RE.search(command):
        return False
    try:
        segments = _segments_of(command)
    except ValueError:
        return False
    for segment in segments:
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            return False
        while tokens and ENV_ASSIGNMENT_RE.match(tokens[0]):
            tokens.pop(0)
        if not tokens:
            return False
        if Path(tokens[0]).name in READONLY_INSPECTION_COMMANDS:
            continue
        if _is_envelope_show_invocation(tokens):
            continue
        return False
    return True


# `envelope show` takes exactly these value-carrying options (keep in sync
# with envelope_compiler.py's common parser). Anything else denies, so a
# grammar change fails safe.
SHOW_VALUE_OPTIONS = {"--receiver", "--project", "--oacp-dir"}
SHOW_FLAG_OPTIONS = {"-h", "--help"}


def _is_envelope_show_invocation(tokens: List[str]) -> bool:
    """Anchored recognition of the read-only ``envelope show`` form.

    The front end must be the command head — a subsequence scan would let
    trailing spoof tokens bless a different leading command (`rm state
    oacp envelope show`) — and the remaining argv must fit show's option
    grammar exactly.
    """
    head = Path(tokens[0]).name
    if head == "oacp":
        if tokens[1:3] != ["envelope", "show"]:
            return False
        rest = tokens[3:]
    elif head.startswith("python"):
        if len(tokens) >= 3 and tokens[1] == "-m":
            # Module names are exact — an endswith match would bless
            # look-alike modules.
            if tokens[2] not in ("oacp.cli", "oacp"):
                return False
            if tokens[3:5] != ["envelope", "show"]:
                return False
            rest = tokens[5:]
        elif len(tokens) >= 2 and tokens[1].endswith("envelope_compiler.py"):
            if tokens[2:3] != ["show"]:
                return False
            rest = tokens[3:]
        else:
            return False
    elif tokens[0].endswith("envelope_compiler.py"):
        if tokens[1:2] != ["show"]:
            return False
        rest = tokens[2:]
    else:
        return False
    expect_value = False
    for token in rest:
        if expect_value:
            expect_value = False
            continue
        if token in SHOW_FLAG_OPTIONS:
            continue
        option, separator, _value = token.partition("=")
        matches = [
            candidate
            for candidate in SHOW_VALUE_OPTIONS
            if candidate.startswith(option)
        ]
        if len(matches) != 1:
            return False
        if not separator:
            expect_value = True
    return not expect_value


def process(payload: Dict[str, Any], receiver: str = "claude") -> Decision:
    from _oacp_env import resolve_oacp_home

    cwd = str(payload.get("cwd") or os.getcwd())
    session_id = str(payload.get("session_id") or "")
    project = find_project(Path(cwd))
    if project is None:
        return ALLOW
    oacp_root = resolve_oacp_home(cwd=Path(cwd))
    target = envelope_path(oacp_root, project, receiver)

    from envelope_compiler import envelope_lock

    if not target.is_file():
        # No active envelope: if this very call is about to compile one,
        # record which session it belongs to. Only the hook sees the harness
        # session id (it is absent from the Bash environment), so this
        # observation is the binding's sole source. Best-effort: a failed
        # claim write must never affect the call's outcome.
        if session_id:
            message_name = _compile_message_name(payload)
            if message_name:
                try:
                    with envelope_lock(target):
                        if not target.is_file():
                            write_session_claim(target, session_id, message_name)
                except OSError:
                    pass
        return ALLOW

    with envelope_lock(target):
        envelope = load_envelope(target)
        if envelope is None:
            return ALLOW
        bound_session = str(envelope.get("session_id") or "")
        foreign_session = bool(
            bound_session and session_id and bound_session != session_id
        )
        context = WorkspaceContext(
            oacp_root=oacp_root,
            project=str(envelope.get("project") or project),
            receiver=str(envelope.get("receiver") or receiver),
            message_id=str(envelope.get("message_id") or ""),
            message_sha256=str(envelope.get("message_sha256") or ""),
        )
        if foreign_session:
            # Session-bound envelope, different session: pre-envelope
            # behavior — no classification, no files_touched accounting.
            # An unbound envelope (legacy compile, or a caller the harness
            # gave no session id) keeps the historical (project, agent)
            # scope: enforcement never silently narrows.
            affinity = _shared_state_affinity(payload, str(target.parent), cwd)
            if affinity == "affine":
                # Pure inspection of shared state cannot disturb the bound
                # session — only mutation capability loses the bypass.
                if _readonly_state_inspection(payload):
                    return ALLOW
                # A foreign session has no legitimate mutation of the state
                # protecting the bound session — deny outright rather than
                # trusting classification, which allows some mutation
                # spellings (e.g. plain rm with a $VAR operand).
                decision = _with_message_id(
                    _deny(
                        "this call can modify envelope state bound to another "
                        "session; a concurrent session must not compile over, "
                        "clear, or edit the active envelope (read-only "
                        "inspection is exempt)"
                    ),
                    context.message_id,
                )
                _record_decision(context, decision, payload, session_id)
                return decision
            if affinity == "clear":
                return ALLOW
            # Uncertain target (unresolved expansion / unparseable): stay
            # enforced through full classification, but never charge the
            # bound session's files_touched budget for foreign work.
        decision = classify(
            str(payload.get("tool_name") or ""),
            payload.get("tool_input") or {},
            cwd,
            envelope,
            context,
        )
        if decision.action == "allow" and decision.new_files and not foreign_session:
            counters = envelope.setdefault("counters", {})
            touched = list(counters.get("files_touched") or [])
            counters["files_touched"] = sorted(set(touched) | set(decision.new_files))
            write_envelope(target, envelope)
        decision = _with_message_id(decision, context.message_id)
        _record_decision(context, decision, payload, session_id)
    return decision


def main(argv: Optional[Sequence[str]] = None) -> int:
    receiver = os.environ.get("OACP_ENVELOPE_RECEIVER", "claude")
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be a JSON object")
        decision = process(payload, receiver=receiver)
    except Exception as exc:  # fail closed in the interior: escalate, never allow
        decision = _ask(f"envelope hook could not evaluate this call: {exc}")
    emit(decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
