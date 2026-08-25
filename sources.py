"""Discovery of the Claude Code data roots that feed metrics.db.

A *Claude user directory* is any directory shaped like `~/.claude`: it holds a
`projects/` subtree of JSONL transcripts, plus (usually) settings/state files.
This module finds them in four places:

1. **Primary** - `~/.claude`, or `$CLAUDE_CONFIG_DIR`. Carries no label, so
   existing project names are unchanged.
2. **Siblings** - other `.claude*` directories sitting next to the primary one
   (e.g. `.claude-work` from a second profile).
3. **Extra locations** - user-supplied paths, searched several levels deep for
   anything that looks like a Claude user directory. A location may point well
   *above* the real directory (a backup drive, a synced folder), so we look at
   every level rather than assuming the path is the root itself.
4. **Remote machines** - transcripts pulled over SSH into a local cache, then
   searched exactly like a local location. Hosts come from `~/.ssh/config` or
   from an explicit list.

Every root outside #1 carries a `label`. The ingester prepends it to that
root's project names (`build-server/gem-trip`, `.claude-work/api`) so a row's
origin is always visible in the dashboard.

Configuration lives in `sources.json` next to this file (see SourceConfig);
the CLI flags on `jsonl_ingest.py` override it.
"""
import glob as _glob
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import namedtuple

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "sources.json")
REMOTE_CACHE = os.path.join(BASE, "remote-cache")

DEFAULT_DEPTH = 4          # levels searched below an extra location
DEFAULT_REMOTE_DEPTH = 3   # levels searched below $HOME on a remote machine
DEFAULT_SSH_TIMEOUT = 300  # seconds per host, including the transfer
DEFAULT_CONNECT_TIMEOUT = 8   # seconds to establish the SSH connection
DEFAULT_REMOTE_BUDGET = 600   # seconds spent on remote fetching per pass

# Failure backoff. A host that cannot be reached is parked rather than retried
# on every pass; without this, one machine with a missing key would mean an
# ssh attempt every hour, forever, for a user who may not even remember
# configuring it. An explicit CLI run always ignores the backoff.
RETRY_BASE_S = 900              # 15 min after the first failure...
RETRY_MAX_S = 12 * 3600         # ...doubling, capped at 12h
AUTH_RETRY_S = 6 * 3600         # a missing/refused key will not fix itself
NO_CLAUDE_RETRY_S = 12 * 3600   # that machine simply does not run Claude Code

# Substrings OpenSSH puts in its diagnostics, mapped to what they mean for us.
# Order matters: the first match wins.
_ERROR_KINDS = (
    ("auth", ("permission denied", "publickey", "host key verification",
              "no matching host key", "too many authentication failures",
              "unable to negotiate", "not a valid identity", "bad permissions",
              "no such identity", "authentication failed")),
    ("unreachable", ("could not resolve", "connection refused", "no route to",
                     "network is unreachable", "connection timed out",
                     "connection closed", "operation timed out",
                     "timed out after", "broken pipe", "connection reset")),
)
# mtime is compared against the *remote* clock, so back the incremental cutoff
# off by an hour to stay correct under modest clock skew. Re-sending an extra
# hour of transcripts is cheap; ingest_state skips re-parsing unchanged files.
REMOTE_SKEW_S = 3600

# A root is a directory to ingest. `label` is prepended to its project names
# ('' for the primary directory); `origin` is for reporting only.
Root = namedtuple("Root", "path label origin")

# Files/dirs that mark a directory as Claude's, beyond the required projects/.
MARKERS = ("settings.json", "settings.local.json", ".credentials.json",
           "history.jsonl", "statsig", "todos", "shell-snapshots", "ide",
           "plugins", "commands", "agents")

# Never descend into these while searching. "projects" is on the list because
# a Claude directory's own transcript tree is huge and can never contain
# another Claude directory worth reporting separately.
PRUNE = {"projects", ".git", "node_modules", "__pycache__", ".venv", "venv",
         ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", ".next",
         ".gradle", ".terraform", "vendor", "site-packages"}


def _warn(msg):
    # ASCII only: this may land on a cp1252 console (Windows default).
    print(f"claude-lens: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Recognising a Claude user directory
# ---------------------------------------------------------------------------

def _projects_has_transcripts(projects, max_dirs=200):
    """True if projects/ holds at least one *.jsonl, scanning at most max_dirs."""
    try:
        with os.scandir(projects) as it:
            for n, entry in enumerate(it):
                if n >= max_dirs:
                    break
                if not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    with os.scandir(entry.path) as inner:
                        for f in inner:
                            if f.name.endswith(".jsonl"):
                                return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


def looks_like_claude_dir(path):
    """Does `path` have the shape of a Claude user directory?

    Required: a `projects/` subdirectory. Sufficient beyond that: a `.claude*`
    name, a known settings/state file, or transcripts actually sitting under
    projects/. That last case is what lets us recognise a renamed or copied
    directory - a backup folder named after the machine it came from.
    """
    projects = os.path.join(path, "projects")
    if not os.path.isdir(projects):
        return False
    if os.path.basename(os.path.normpath(path)).startswith(".claude"):
        return True
    if any(os.path.exists(os.path.join(path, m)) for m in MARKERS):
        return True
    return _projects_has_transcripts(projects)


def find_claude_dirs(location, max_depth=DEFAULT_DEPTH):
    """Every Claude user directory at or below `location`, breadth-first.

    A match is not descended into, so a directory never yields its own nested
    copies. Symlink loops are broken by tracking resolved paths.
    """
    location = os.path.abspath(os.path.expanduser(location))
    if not os.path.isdir(location):
        return []
    found, seen, queue = [], set(), [(location, 0)]
    while queue:
        path, depth = queue.pop(0)
        try:
            key = os.path.normcase(os.path.realpath(path))
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        if looks_like_claude_dir(path):
            found.append(path)
            continue  # a Claude dir is a leaf for this search
        if depth >= max_depth:
            continue
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.name in PRUNE:
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            queue.append((entry.path, depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue
    return found


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

def _label_for(path, host=None):
    """The prefix shown on this root's project names.

    Local: the Claude folder's own name (`.claude-work`), except that a plain
    `.claude` found somewhere other than the primary location is named after
    its parent instead - that parent is what distinguishes it (a per-machine
    backup folder, say).
    Remote: the host name, with the folder name appended when the machine has
    more than the standard directory.
    """
    name = os.path.basename(os.path.normpath(path))
    if host:
        return host if name == ".claude" else f"{host}/{name}"
    if name == ".claude":
        parent = os.path.basename(os.path.dirname(os.path.normpath(path)))
        return parent or name
    return name


def _add_root(roots, taken, path, origin, host=None):
    """Append `path` as a root, skipping duplicates and de-colliding labels."""
    key = os.path.normcase(os.path.realpath(path))
    if key in taken:
        return
    label = _label_for(path, host)
    used = {r.label for r in roots}
    if not label or label in used:
        base, n = label or "source", 2
        while not label or label in used:
            label = f"{base}-{n}"
            n += 1
    taken.add(key)
    roots.append(Root(path, label, origin))


# ---------------------------------------------------------------------------
# Local discovery
# ---------------------------------------------------------------------------

def primary_dir():
    """Claude Code's own config directory: $CLAUDE_CONFIG_DIR or ~/.claude."""
    return os.path.abspath(os.path.expanduser(
        os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join("~", ".claude")))


def discover_local(extra_locations=(), scan_siblings=True,
                   depth=DEFAULT_DEPTH, primary=None):
    """Primary dir first (unlabeled), then siblings, then extra locations."""
    primary = os.path.abspath(os.path.expanduser(primary or primary_dir()))
    roots, taken = [Root(primary, "", "primary")], set()
    try:
        taken.add(os.path.normcase(os.path.realpath(primary)))
    except OSError:
        pass

    if scan_siblings:
        try:
            entries = sorted(os.scandir(os.path.dirname(primary)),
                             key=lambda e: e.name)
        except OSError:
            entries = []
        for entry in entries:
            if not entry.name.startswith(".claude"):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if looks_like_claude_dir(entry.path):
                _add_root(roots, taken, entry.path, "sibling")

    for location in extra_locations:
        hits = find_claude_dirs(location, depth)
        if not hits:
            _warn(f"no Claude directory found under {location}")
        for path in hits:
            _add_root(roots, taken, path, "local")
    return roots


# ---------------------------------------------------------------------------
# SSH config
# ---------------------------------------------------------------------------

def ssh_config_hosts(path=None, _seen=None):
    """Concrete host aliases from an OpenSSH config, following Include.

    Wildcard/negated patterns (`Host *`, `Host !bad`) configure other entries
    rather than naming a machine, so they are skipped.
    """
    path = os.path.expanduser(path or os.path.join("~", ".ssh", "config"))
    _seen = _seen if _seen is not None else set()
    real = os.path.normcase(os.path.abspath(path))
    if real in _seen or not os.path.isfile(path):
        return []
    _seen.add(real)
    hosts = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = re.split(r"[\s=]+", line)
        key = parts[0].lower()
        if key == "include":
            for pattern in parts[1:]:
                pattern = os.path.expanduser(pattern.strip('"'))
                if not os.path.isabs(pattern):
                    pattern = os.path.join(os.path.dirname(path), pattern)
                for inc in sorted(_glob.glob(pattern)):
                    hosts += ssh_config_hosts(inc, _seen)
        elif key == "host":
            for pattern in parts[1:]:
                pattern = pattern.strip('"')
                if not pattern or any(c in pattern for c in "*?!"):
                    continue
                hosts.append(pattern)
    out, dedupe = [], set()
    for h in hosts:
        if h not in dedupe:
            dedupe.add(h)
            out.append(h)
    return out


# ---------------------------------------------------------------------------
# Remote collection
# ---------------------------------------------------------------------------

# Runs on the remote under /bin/sh. Args: $1 = search depth, $2 = mtime cutoff
# (epoch seconds; 0 = everything). Locates every `.claude*` directory holding a
# projects/ subtree under $HOME and streams their transcripts out as a gzipped
# tar on stdout, with paths relative to $HOME. Exit 3 = nothing Claude-shaped
# on this machine, 4 = no usable home directory. Needs only sh, find and tar,
# so it works on Linux and macOS remotes alike; -newermt support is probed at
# run time and the incremental filter is simply skipped where it is missing.
REMOTE_SH = r"""
set -u
DEPTH="${1:-3}"
SINCE="${2:-0}"
H="${HOME:-}"
[ -n "$H" ] || H=$(cd && pwd) || exit 4
cd "$H" || exit 4
roots() { find . -maxdepth "$DEPTH" -type d -name '.claude*' -prune -print 2>/dev/null; }
roots | grep -q . || exit 3
NEWER=""
if [ "$SINCE" -gt 0 ] 2>/dev/null; then
  if find . -maxdepth 0 -newermt "@$SINCE" >/dev/null 2>&1; then NEWER=1; fi
fi
list() {
  roots | while IFS= read -r r; do
    [ -d "$r/projects" ] || continue
    if [ -n "$NEWER" ]; then
      find "$r/projects" -type f -name '*.jsonl' -newermt "@$SINCE" -print0 2>/dev/null
    else
      find "$r/projects" -type f -name '*.jsonl' -print0 2>/dev/null
    fi
  done
}
list | tar -czf - --null -T -
"""


# Python 3.12+ (and 3.9.17+/3.11.4+) can apply tarfile's own hardening on top
# of the checks below; older runtimes simply don't get it.
_EXTRACT_KW = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}


def _safe_extract(tar, dest):
    """Extract regular files only, refusing any member that escapes `dest`.

    Members keep their relative layout inside the host's own cache directory;
    anything that would resolve outside it (a `..` component, a symlink, a
    device node) is dropped rather than sanitised, since a well-behaved remote
    never sends one.
    """
    dest = os.path.abspath(dest)
    count = 0
    for member in tar:
        if not member.isfile():
            continue
        name = member.name.replace("\\", "/")
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if not parts or ".." in parts or name.startswith("/"):
            continue
        member.name = "/".join(parts)
        target = os.path.abspath(os.path.join(dest, *parts))
        if target != dest and not target.startswith(dest + os.sep):
            continue
        try:
            tar.extract(member, dest, **_EXTRACT_KW)
            count += 1
        except (OSError, tarfile.TarError):
            continue
    return count


def host_cache_dir(host):
    return os.path.join(REMOTE_CACHE, re.sub(r"[^A-Za-z0-9._-]", "_", host))


def classify_error(message):
    """What kind of failure this is, which decides how long to back off."""
    text = (message or "").lower()
    for kind, needles in _ERROR_KINDS:
        if any(n in text for n in needles):
            return kind
    return "other"


def retry_delay(kind, fail_count):
    """Seconds to wait before trying this host again.

    Auth and no-Claude-directory failures are *states*, not blips - a key that
    is missing now will still be missing in fifteen minutes - so they park the
    host for hours straight away. Everything else backs off exponentially,
    which recovers quickly from a reboot but stops hammering a dead machine.
    """
    if kind == "auth":
        return AUTH_RETRY_S
    if kind == "no_claude":
        return NO_CLAUDE_RETRY_S
    return min(RETRY_BASE_S * (2 ** max(fail_count - 1, 0)), RETRY_MAX_S)


def fmt_duration(seconds):
    """Compact '4m' / '2h 30m' for status output."""
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    h, m = divmod(seconds // 60, 60)
    return f"{h}h" if not m else f"{h}h {m}m"


def fetch_remote(host, since=0, depth=DEFAULT_REMOTE_DEPTH,
                 timeout=DEFAULT_SSH_TIMEOUT,
                 connect_timeout=DEFAULT_CONNECT_TIMEOUT, ssh_opts=()):
    """Pull a host's transcripts into its local cache directory.

    Returns {"host", "files", "cache", "error", "kind", "elapsed"}; `error` is
    None on success and `kind` says what went wrong ("auth", "unreachable",
    "no_claude", "other"). This never raises: a host-level problem must not
    take the whole report - or the background receiver - down with it.

    Three separate limits keep a bad host cheap rather than annoying:
      * BatchMode + NumberOfPasswordPrompts=0 - a missing key fails in well
        under a second instead of blocking on a password prompt.
      * ConnectTimeout - bounds a machine that is off or firewalled.
      * ServerAlive probes - a connection that goes dead mid-transfer is torn
        down in ~45s rather than sitting until the overall timeout. A slow but
        genuinely progressing transfer is unaffected.
    """
    started = time.monotonic()
    result = {"host": host, "files": 0, "cache": host_cache_dir(host),
              "error": None, "kind": None, "elapsed": 0.0}

    def done(error=None, kind=None):
        result["elapsed"] = round(time.monotonic() - started, 1)
        if error:
            result["error"] = error
            result["kind"] = kind or classify_error(error)
        return result

    if not shutil.which("ssh"):
        return done("ssh not found on PATH", "other")
    cmd = ["ssh",
           "-o", "BatchMode=yes",
           "-o", "NumberOfPasswordPrompts=0",
           "-o", f"ConnectTimeout={int(connect_timeout)}",
           "-o", "ServerAliveInterval=15",
           "-o", "ServerAliveCountMax=3",
           "-o", "StrictHostKeyChecking=accept-new",
           *ssh_opts,
           host, "sh", "-s", "--", str(depth), str(int(since))]
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="claude-lens-", suffix=".tar.gz")
        with os.fdopen(fd, "wb") as out:
            proc = subprocess.run(cmd, input=REMOTE_SH.encode(), stdout=out,
                                  stderr=subprocess.PIPE, timeout=timeout)
        if proc.returncode == 3:
            return done("no Claude directory found on this machine", "no_claude")
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            lines = [ln for ln in err.splitlines() if ln.strip()]
            return done((lines[-1] if lines
                         else f"ssh exited {proc.returncode}")[:200])
        os.makedirs(result["cache"], exist_ok=True)
        with tarfile.open(tmp, mode="r|gz") as tar:
            result["files"] = _safe_extract(tar, result["cache"])
    except subprocess.TimeoutExpired:
        return done(f"timed out after {timeout}s", "unreachable")
    except Exception as exc:  # noqa: BLE001 - nothing here may reach the caller
        return done(f"{type(exc).__name__}: {exc}"[:200])
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return done()


def dedupe_labels(roots):
    """Make every label unique across roots that were discovered separately.

    Local discovery and each host's discovery each de-collide internally, but
    they can't see each other - a backup folder named `oldlaptop` and an SSH
    host named `oldlaptop` would otherwise merge two machines' projects under
    one name. Later roots get a `-2` suffix; the unlabeled primary is exempt
    because there is only ever one of it.
    """
    seen, out = set(), []
    for root in roots:
        label = root.label
        if label and label in seen:
            n = 2
            while f"{label}-{n}" in seen:
                n += 1
            label = f"{label}-{n}"
            _warn(f"duplicate source label for {root.path}; using '{label}'")
        seen.add(label)
        out.append(Root(root.path, label, root.origin))
    return out


def remote_roots(host):
    """The Claude directories inside a host's cache, labeled with the host."""
    roots, taken = [], set()
    for path in find_claude_dirs(host_cache_dir(host), DEFAULT_REMOTE_DEPTH + 1):
        _add_root(roots, taken, path, "remote", host=host)
    return roots


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class SourceConfig:
    """Which places to look. Loaded from sources.json, overridden by CLI flags.

    sources.json (all keys optional):
        {
          "extra_locations": ["D:/backups/claude"],
          "scan_sibling_claude_dirs": true,
          "depth": 4,
          "remotes": ["build-server", "mac-mini"],
          "use_ssh_config": false,
          "ssh_timeout": 300,
          "ssh_options": ["-i", "~/.ssh/id_claude"]
        }
    """

    def __init__(self, extra_locations=(), scan_siblings=True,
                 depth=DEFAULT_DEPTH, remotes=(), use_ssh_config=False,
                 ssh_timeout=DEFAULT_SSH_TIMEOUT, ssh_options=(),
                 remote_full=False,
                 ssh_connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                 remote_budget=DEFAULT_REMOTE_BUDGET):
        self.extra_locations = list(extra_locations)
        self.scan_siblings = scan_siblings
        self.depth = depth
        self.remotes = list(remotes)
        self.use_ssh_config = use_ssh_config
        self.ssh_timeout = ssh_timeout
        self.ssh_connect_timeout = ssh_connect_timeout
        self.remote_budget = remote_budget
        self.ssh_options = [os.path.expanduser(o) for o in ssh_options]
        self.remote_full = remote_full

    @classmethod
    def load(cls, path=CONFIG_PATH):
        if not os.path.isfile(path):
            return cls()
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as exc:
            _warn(f"ignoring {os.path.basename(path)}: {exc}")
            return cls()
        return cls(
            extra_locations=raw.get("extra_locations") or [],
            scan_siblings=raw.get("scan_sibling_claude_dirs", True),
            depth=int(raw.get("depth", DEFAULT_DEPTH)),
            remotes=raw.get("remotes") or [],
            use_ssh_config=bool(raw.get("use_ssh_config", False)),
            ssh_timeout=int(raw.get("ssh_timeout", DEFAULT_SSH_TIMEOUT)),
            ssh_connect_timeout=int(raw.get("ssh_connect_timeout",
                                            DEFAULT_CONNECT_TIMEOUT)),
            remote_budget=int(raw.get("remote_budget", DEFAULT_REMOTE_BUDGET)),
            ssh_options=raw.get("ssh_options") or [],
        )

    def hosts(self):
        """Explicit remotes plus, when asked, every host in ~/.ssh/config."""
        out, seen = [], set()
        for h in list(self.remotes) + (ssh_config_hosts()
                                       if self.use_ssh_config else []):
            if h not in seen:
                seen.add(h)
                out.append(h)
        return out
