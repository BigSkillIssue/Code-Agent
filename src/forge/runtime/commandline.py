"""Find every simple command inside a shell command line, so that permission rules see each one.

A best effort over shell syntax: chains, pipes, `$(...)`, `<(...)`, keywords such as `if` and
`do`, wrappers such as `sudo -u root` or `timeout 5`, and scripts handed to another shell
(`bash -lc '...'`, `pwsh -EncodedCommand ...`, `eval`, `find -exec`, `echo ... | sh`). Variables
and aliases are not expanded; the OS sandbox is the boundary, these rules are a guard rail.
"""

import base64
import binascii
import re
import shlex
from dataclasses import dataclass

MAX_DEPTH = 5
SEPARATORS = re.compile(r"\|\||&&|;;|\|&|[;|&\n]")
SUBSHELLS = re.compile(r"[$<>]\(([^()]*)\)|`([^`]*)`")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Shell keywords that start a command without being one; `(` and `{` group commands.
KEYWORDS = frozenset(
    {"{", "}", "(", ")", "!", "if", "then", "else", "elif", "do", "while", "until", "fi", "done"}
)
# Programs that run their arguments as a command: options that take a value, and how many
# plain words come before the command (`timeout 5 cmd`, `taskset 0x3 cmd`).
WRAPPERS: dict[str, tuple[frozenset[str], int]] = {
    name: (frozenset(valued.split()), positional)
    for name, valued, positional in [
        ("sudo", "-u -g -h -p -C -D -r -t -U -T -R --user --group --host --prompt "
                 "--close-from --chdir --role --type --other-user --command-timeout --chroot", 0),
        ("doas", "-u -C", 0),
        ("pkexec", "--user", 0),
        ("nice", "-n --adjustment", 0),
        ("env", "-u --unset -C --chdir", 0),
        ("xargs", "-a -d -E -I -L -n -P -s --arg-file --delimiter --eof --replace --max-lines "
                  "--max-args --max-procs --max-chars --process-slot-var", 0),
        ("time", "-f -o --format --output", 0),
        ("exec", "-a", 0),
        ("timeout", "-s -k --signal --kill-after", 1),
        ("ionice", "-c -n -p -P -u --class --classdata --pid --pgid --uid", 0),
        ("stdbuf", "-i -o -e --input --output --error", 0),
        ("chrt", "", 1),
        ("taskset", "", 1),
        ("watch", "-n --interval", 0),
        ("nohup", "", 0),
        ("setsid", "", 0),
        ("command", "", 0),
        ("builtin", "", 0),
        ("unbuffer", "", 0),
        ("caffeinate", "", 0),
    ]
}  # fmt: skip
PRIVILEGED = frozenset({"sudo", "doas", "pkexec", "su", "runuser", "runas"})
POSIX_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "su", "runuser", "flock"})
# Options that take a value; su and runuser take a user, flock a lock file before the command.
SHELL_VALUED = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
LEADING: dict[str, frozenset[str]] = {
    "su": frozenset({"-g", "-G", "-s", "--group", "--supp-group", "--shell"}),
    "runuser": frozenset({"-u", "-g", "-G", "-s", "--user", "--group", "--supp-group", "--shell"}),
    "flock": frozenset({"-w", "-E", "--timeout", "--conflict-exit-code"}),
}
POWERSHELLS = frozenset({"pwsh", "powershell"})
POWERSHELL_VALUED = frozenset(
    {"-executionpolicy", "-ep", "-ex", "-workingdirectory", "-wd", "-outputformat", "-of", "-o",
     "-inputformat", "-if", "-configurationname", "-config", "-custompipename", "-settingsfile",
     "-windowstyle", "-w", "-version", "-v", "-psconsolefile"}
)  # fmt: skip
FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
ECHOES = frozenset({"echo", "printf"})
REDIRECT = re.compile(r"[0-9&]?>{1,2}\|?\s*(\S*)")


@dataclass(frozen=True)
class Found:
    """One simple command as rules see it (wrappers removed) and as written, with the marks an
    allow rule must name to cover it: a program that changes privileges (`sudo`) or `>`."""

    text: str
    written: str
    marks: tuple[str, ...] = ()


def simple_commands(command: str) -> list[str]:
    """Every simple command inside a command line: chained, piped, in $(...), or wrapped
    in `sh -c`, `sudo`, `env`, `xargs` ... (rules must see each one)."""
    return [found.text for found in commands_in(command)]


def commands_in(command: str, depth: int = 0) -> list[Found]:
    """Every simple command inside a command line, as written and as rules see it."""
    if depth > MAX_DEPTH:
        return []
    found: list[Found] = []
    for inner in SUBSHELLS.findall(command):
        found += commands_in(inner[0] or inner[1], depth + 1)
    segments = split_segments(SUBSHELLS.sub(" ", command))
    for index, segment in enumerate(segments):
        words, privileged = command_words(segment)
        if not words:
            continue
        written = " ".join(split_words(segment.strip()))
        marks = (*privileged, *((">",) if writes_to_file(segment) else ()))
        scripts = nested_scripts(words, segments[index - 1] if index else "")
        if not scripts or program_name(words[0]) == "find":
            found.append(Found(" ".join(words), written, marks))
        for script in scripts or []:
            found += [
                Found(inner.text, written if marks else inner.written, (*marks, *inner.marks))
                for inner in commands_in(script, depth + 1)
            ]
    return found


def command_words(segment: str) -> tuple[list[str], list[str]]:
    """The words of one simple command without leading keywords, assignments and wrappers;
    also the wrappers that run it with other privileges (`sudo`, `doas` ...)."""
    words = split_words(segment.strip().strip("()").strip())
    privileged: list[str] = []
    while words:
        name = program_name(words[0])
        if name in PRIVILEGED:
            privileged.append(name)
        if words[0] in KEYWORDS or ASSIGNMENT.match(words[0]):
            words = words[1:]
        elif name in WRAPPERS:
            words = after_wrapper(name, words[1:])
        else:
            break
    return words, privileged


def after_wrapper(name: str, rest: list[str]) -> list[str]:
    """The command a wrapper runs: its options and their values and its leading words skipped
    (`env -S 'a b'` runs `a b`)."""
    valued, positional = WRAPPERS[name]
    rest = list(rest)
    while rest and rest[0].startswith("-") and rest[0] != "-":
        word = rest.pop(0)
        if word == "--":
            break
        if name == "env" and (word == "-S" or word.startswith("--split-string")):
            value = word.partition("=")[2] or (rest.pop(0) if rest else "")
            return split_words(value) + rest
        if word in valued and rest and not rest[0].startswith("-"):
            rest.pop(0)  # the option's value
    return rest[positional:]


def nested_scripts(words: list[str], piped_in: str) -> list[str] | None:
    """Command lines this command hands to a shell (None when it runs no other command)."""
    name = program_name(words[0])
    if name in POSIX_SHELLS:
        script = posix_script(name, words[1:])
        if script is None and len(words) == 1:
            return echoed(piped_in)  # `echo 'cmd' | sh`
        return None if script is None else [script]
    if name in POWERSHELLS:
        return powershell_script(name, words[1:])
    if name == "cmd":
        lowered = [word.lower() for word in words]
        starts = [i for i, word in enumerate(lowered) if word in ("/c", "/k", "/r")]
        return [" ".join(words[starts[0] + 1 :])] if starts else None
    if name == "eval":
        return [" ".join(words[1:])]
    if name == "find":
        return find_scripts(words[1:])
    return None


def posix_script(name: str, args: list[str]) -> str | None:
    """The script of `sh -c '...'`, `bash -lc '...'`, `bash <<< '...'`, `su user -c '...'` or
    `flock file cmd`."""
    rest = list(args)
    valued = SHELL_VALUED | LEADING.get(name, frozenset())
    leading = 1 if name in LEADING else 0
    while rest:
        word = rest.pop(0)
        if word.startswith("<<<"):
            return word[3:] or (rest[0] if rest else "")
        if word in ("-c", "--command") or (
            word.startswith("-") and not word.startswith("--") and "c" in word[1:]
        ):
            return rest[0] if rest else ""
        if word in valued and rest and not rest[0].startswith("-"):
            rest.pop(0)
        elif not word.startswith(("-", "+")):
            if leading == 0:
                # flock runs the rest as a command; a shell runs a script file we cannot see
                return " ".join([word, *rest]) if name == "flock" else None
            leading -= 1
    return None


def powershell_script(name: str, args: list[str]) -> list[str] | None:
    """The command of `pwsh -Command ...` (also abbreviated) or `-EncodedCommand <base64>`."""
    rest = list(args)
    while rest:
        word = rest.pop(0)
        lowered = word.lower()
        if lowered in ("-e", "-ec") or (len(lowered) > 2 and "-encodedcommand".startswith(lowered)):
            return [decode_powershell(rest[0])] if rest else None
        if lowered.startswith("-c") and "-command".startswith(lowered):
            return [" ".join(rest)]
        if lowered in POWERSHELL_VALUED and rest and not rest[0].startswith("-"):
            rest.pop(0)
        elif not word.startswith("-"):
            # Windows PowerShell 5.1 runs plain arguments as a command, pwsh as a script file
            return [" ".join([word, *rest])] if name == "powershell" else None
    return None


def decode_powershell(encoded: str) -> str:
    """The text of a base64 UTF-16LE script, or the input when it is not one."""
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-16-le")
    except (binascii.Error, UnicodeDecodeError):
        return encoded


def find_scripts(args: list[str]) -> list[str]:
    """The commands `find -exec ... ;` (also -execdir, -ok, -okdir) runs."""
    scripts: list[str] = []
    rest = list(args)
    while rest:
        if rest.pop(0) not in FIND_EXEC:
            continue
        words: list[str] = []
        while rest and rest[0] not in (";", "\\;", "+", "\\"):
            words.append(rest.pop(0))
        scripts.append(" ".join(words))
    return scripts


def echoed(segment: str) -> list[str]:
    """What `echo` or `printf` in the segment before a pipe prints (a script for the shell)."""
    words, _ = command_words(segment)
    if words and program_name(words[0]) in ECHOES:
        return [" ".join(word for word in words[1:] if not word.startswith("-"))]
    return []


def program_name(word: str) -> str:
    """`/usr/bin/sudo` and `SUDO.EXE` are both `sudo`."""
    return word.lower().replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".exe")


def split_segments(command: str) -> list[str]:
    """Split at ; | & && || and line breaks, but not inside quotes, after a backslash or in a
    redirection such as `2>&1` or `&>file`."""
    segments, current, quote = [], "", ""
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            current += command[index : index + 2]
            index += 2
            continue
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif not in_redirection(command, index) and (match := SEPARATORS.match(command, index)):
            segments.append(current)
            current, index = "", match.end()
            continue
        current += char
        index += 1
    return [*segments, current]


def in_redirection(command: str, index: int) -> bool:
    """True for the `&` of `2>&1`, `<&0` or `&>file`."""
    if command[index] != "&":
        return False
    return (index > 0 and command[index - 1] in "<>") or command.startswith(">", index + 1)


def split_words(text: str) -> list[str]:
    """Shell-like words (quotes removed); falls back to whitespace for unbalanced quotes."""
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


def writes_to_file(segment: str) -> bool:
    """True if the command redirects output to a file (`>`, `>>`, `&>`; not `2>&1` or
    `> /dev/null`)."""
    plain = without_quotes(segment)
    for found in REDIRECT.finditer(plain):
        target = found.group(1)
        if not (target.startswith("&") or target == "/dev/null"):
            return True
    return False


def without_quotes(text: str) -> str:
    """The text with every quoted part (and escaped character) replaced by `_`."""
    out, quote, index = "", "", 0
    while index < len(text):
        char = text[index]
        if char == "\\" and quote != "'":
            out += "__"
            index += 2
            continue
        if quote:
            quote = "" if char == quote else quote
            out += "_"
        elif char in "'\"":
            quote = char
            out += "_"
        else:
            out += char
        index += 1
    return out
