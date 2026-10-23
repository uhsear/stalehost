#!/usr/bin/env python
r"""Find every ArcGIS file on a workstation that still names the host you are retiring, and never call a tree clean that you could not finish reading.

Point it at a machine and it walks the local fixed drives for .aprx, .lyrx,
.sde and a short list of plain text types -- .py, .bat, .cmd, .ps1, .sql and
.ini -- decides whether each one still names the old host as a whole token,
and prints one line an operator can read in a fleet grid. Every way the scan can
fall short -- a directory it could not enumerate, a project it could not open,
a clock that ran out -- has its own counter and its own verdict. A tree that was
only half read is never reported as clean.

The obvious tool is a recursive text search: `findstr /s`, `grep -r`, or ripgrep.
ripgrep is faster than this will ever be, searches any file type, and decodes
UTF-16 when the file carries a BOM. The gap is the three ArcGIS formats. An
.aprx is a ZIP, so the host name exists only inside the deflated CIM JSON and a
scan of the container bytes finds nothing. An .sde stores its properties as
UTF-16LE with no BOM, in the middle of a binary file, at whatever byte offset
the property set lands on. And a substring match cannot tell OLDHOST-1 from
OLDHOST-10, which is the neighbouring server that must not be touched. The last
gap is the verdict: `grep -r` exits 0 or 1 whether or not it could read the
tree, so a permission-denied profile looks exactly like a clean one. On the
text bucket alone ripgrep wins and this tool does not pretend otherwise. What
it has is one verdict across a ZIP, a UTF-16LE property set and text together,
with an exit code built for a detect-and-remediate loop.

    python stalehost.py --self-test
    python stalehost.py OLDHOST-1
    python stalehost.py OLDHOST-1 --path D:\GIS
    python stalehost.py OLDHOST-1 --json
    python stalehost.py OLDHOST-1 --fail-on-sde
    python stalehost.py OLDHOST-1 --text-ext .txt,.config --fail-on-text
    python stalehost.py OLDHOST-1 --to NEWHOST
    python stalehost.py OLDHOST-1 --to NEWHOST --apply
    python stalehost.py OLDHOST-1 --to NEWHOST --apply --passwords-stdin < logins.json

Exit codes: 0 nothing to remediate, 1 stale content found or the scan could not
be trusted, 64 usage error. Read EXIT CODE POLARITY below before changing them.

EXIT CODE POLARITY -- the tool is built for a detect-and-remediate loop
(Microsoft Intune Remediations, or any fleet tool with the same shape):

    exit 1  = ISSUE FOUND. The remediation script runs.
    exit 0  = HEALTHY.     The remediation script does not run.

That is the opposite of a Win32 app detection rule, where exit 0 plus non-empty
output means "found". Do not carry that habit here.

    exit 1 -- the device has remediable work, or cannot be proven not to:
      1. a stale .aprx or .lyrx was found                     (STALE)
      2. the scan hit --max-seconds before finishing          (INCOMPLETE)
      3. a scan root could not be enumerated, or a .aprx, a
         .lyrx or a text file could not be read, so the
         device is unproven                                   (UNVERIFIED)
      4. the tool itself threw                                (ERROR)

    exit 0 -- nothing for the remediation to do:
      5. no stale content at all                              (CLEAN)
      6. stale text only. See WHY TEXT IS INVENTORY below     (TEXTONLY)
      7. stale .sde only. See WHY .sde IS INVENTORY below     (SDEONLY)

Cases 2 to 4 fail TOWARD remediation on purpose. A remediation that is
idempotent changes nothing on a clean device, whereas a missed device means
broken connections for a user the moment the database moves. The trade-off is
that a device which can never finish, or never read a file, reports "issue
found" every cycle. That is a visible signal rather than a silent miss. Known
causes: ArcGIS Pro holding a project open during the scan, or a cloud-storage
placeholder the scanning account cannot hydrate. Both show as unverified=N in
the summary line, so both are diagnosable from the grid.

WHY .sde IS INVENTORY AND NOT A VERDICT. A stale .sde is counted, sampled and
reported, and by default it does not set exit 1, because the usual remediation
cannot fix the .sde a real fleet has. A DBMS-authenticated .sde stores a
database password, so repointing one needs a human to supply it: on the dry
run that produced this tool, every one of the thousand-odd .sde files found was
DBMS-auth and none was repointed. Without ArcGIS Pro on the device, arcpy cannot
rewrite one at all. Letting .sde set exit 1 makes the loop re-run a remediation
forever on a device it can never clean. The sde= count is the manual worklist
instead. Pass --fail-on-sde only when your remediation really can fix them,
which is the case after --to and --apply with Pro's Python and the passwords on
stdin.

WHAT --apply DOES, AND WHAT IT REFUSES. With --to NEWHOST the tool prints a plan
and writes nothing. Adding --apply is the only way anything is written. An
.aprx or .lyrx is rewritten in process: the new bytes are built beside the old
file, read back with the matcher the scan uses, compared against the stat taken
before the work began, and only then swapped in with os.replace, after a
byte-checked backup. An .sde cannot be edited as bytes -- it is a length-prefixed
property set -- so ArcGIS Pro's own Python rebuilds it with
CreateDatabaseConnection in a child process. Passwords reach that child on its
stdin and nowhere else: there is no flag for one and the environment is never
read. Text files are not rewritten. After --apply the tree is scanned again and
the exit code is the verdict for the tree as it now stands, so a file that could
not be repointed still says STALE.

WHY TEXT IS INVENTORY TOO, AND WHY IT IS SCANNED AT ALL. The first version of
this tool read three ArcGIS formats and nothing else. Pointed at a legacy
automation share of several hundred files where about a fifth named the retiring
host, one of them running nightly under Task Scheduler, it printed CLEAN over
scanned=0. The PowerShell one-liner this tool exists to replace would have found
every one of them. A detector that cannot see what its own remediation fixes is worse than the thing
it replaced, so the text bucket is not optional.

It does not set exit 1 by default, for a different reason than .sde. The
extension list is a guess about somebody else's fleet, and a host name in a
.txt is often prose rather than a connection, so promoting text to work would
change the exit code under remediations that are already deployed. --fail-on-
text promotes it, --text-ext widens it, and both counts are on the line either
way. An unread text file is UNVERIFIED regardless: only .sde has an excuse.
"""

from __future__ import print_function

import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# Extensions a remediation can rewrite unattended, with no ArcGIS licence.
# These drive the verdict.
FIXABLE_EXTENSIONS = (".aprx", ".lyrx")

# Extensions reported as inventory only. See WHY .sde IS INVENTORY above.
INVENTORY_EXTENSIONS = (".sde",)

# Plain text that names a host in a connection. A scheduled .py is the common
# one: on a legacy automation share of several hundred files, about a fifth
# named the retiring host, one of them ran nightly under Task Scheduler, and this
# tool read none of them and printed CLEAN over scanned=0. The list is short on purpose -- these six
# are script and configuration types, where a host name is a connection rather
# than prose. .txt is deliberately absent: a readme that mentions a server is
# not a broken job. Add it, or anything else your fleet uses, with --text-ext.
TEXT_EXTENSIONS = (".py", ".bat", ".cmd", ".ps1", ".sql", ".ini")

# The two buckets whose membership is fixed. The text bucket is composed per
# run, because --text-ext extends it.
SCANNED_EXTENSIONS = FIXABLE_EXTENSIONS + INVENTORY_EXTENSIONS

# Sample paths printed. Fixable hits are sampled separately from .sde and from
# text so that a connection cache full of .sde, or a share full of scripts,
# cannot crowd the actionable .aprx and .lyrx paths out of the report. A fleet
# console usually captures about 2048 characters of output; 5 + 3 + 3 lines of
# ~115 characters leaves room to spare.
SAMPLE_CAP = 5
SDE_SAMPLE_CAP = 3
TEXT_SAMPLE_CAP = 3

# Width a sample path is truncated to, from the left. The meaningful part of a
# GIS project path is its end.
SAMPLE_WIDTH = 110

# Directory names skipped at any depth. The first seven hold no user content.
# node_modules and .git are here because a measured recon found zero .aprx,
# .lyrx or .sde in either, and on a developer workstation they dominate the walk.
EXCLUDE_NAMES = frozenset([
    "windows", "program files", "program files (x86)", "programdata",
    "recycle.bin", "$recycle.bin", "system volume information",
    "node_modules", ".git",
])

# ArcGIS Pro drops ephemeral GUID-named .sde connection files straight into
# AppData\Local\Temp and never cleans them up: thousands of them on the
# workstation this was measured on, the single biggest cost in a full walk. They are
# per-session scratch, regenerated on the next launch, so repointing them is
# pointless. --scan-temp includes them anyway. Slashes are normalised before
# the comparison so the same rule can be tested on a non-Windows host.
TEMP_FRAGMENT = "/appdata/local/temp"

# Safety cap on total scan time. A pruned full-C: walk on a production
# workstation measured 58 s, so 600 is roughly 10x headroom for a slower disk or
# a larger user profile. It is not derived from any fleet tool's deadline.
DEFAULT_MAX_SECONDS = 600

# Read caps. A .sde is a few kilobytes and a CIM part is rarely over a megabyte.
# Both caps exist because this runs as a privileged service account over every
# local drive, reading whatever a user dropped there. A file over the cap raises
# and is counted unreadable, which fails toward remediation rather than
# pretending the file was clean.
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_ENTRY_BYTES = 64 * 1024 * 1024

# Default path of the Python that ships with ArcGIS Pro, and the registry key
# read when Pro is installed somewhere else.
PRO_PYTHON = r"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
PRO_REGISTRY_KEY = r"SOFTWARE\ESRI\ArcGISPro"

# Remediation. Every file --apply replaces keeps its original beside it, under
# this name plus a UTC time stamp. The tool never deletes a backup.
BACKUP_MARK = ".stalehost-bak-"
TEMP_MARK = ".stalehost-tmp"

# The time one Pro Python process may spend rebuilding every .sde connection.
# Each CreateDatabaseConnection call connects to the database, so a dead server
# is a hang and not an error. The cap turns it into a counted failure.
SDE_WORKER_SECONDS = 1800

# The most that is read from stdin. A password list is a few kilobytes.
STDIN_CAP = 1024 * 1024

# The file the .sde worker runs from, in the child process. It is this file. The
# self-test points it at a copy that sits beside a stand-in arcpy, because Pro's
# own Python puts its real ArcPy ahead of anything on PYTHONPATH.
WORKER_SCRIPT = os.path.abspath(__file__)

# The prefix of each line the .sde worker prints, so the noise arcpy and Python
# write to the same stream is never parsed as a result.
RESULT_MARK = "STALEHOST-RESULT "

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

# A zip local file header begins with these four bytes.
ZIP_MAGIC = b"PK\x03\x04"

# stat.FILE_ATTRIBUTE_REPARSE_POINT, written out so the constant does not depend
# on which platform the stat module was built for.
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# Bytes that are part of a host token, so not a boundary: 0-9 A-Z a-z and the
# hyphen. Held as a set of ints because indexing a bytes object yields ints.
WORD_BYTES = frozenset(
    b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-")

# Bytes a replacement host may hold: the token bytes plus '.' and '_'.
HOST_BYTES = WORD_BYTES | frozenset(b"._")

# Properties read off a connection with arcpy.Describe(...).connectionProperties.
# Names are the documented ones, dbClient with its capital C included.
SDE_PROPS = ("dbClient", "instance", "server", "database",
             "authentication_mode", "user", "version", "branch",
             "historical_name", "historical_timestamp")

# Verdict states. The word leads the summary line because a fleet grid truncates
# the output column from the right, and every row otherwise opened with the same
# dozen identical characters, so the one word that mattered was the one cut off.
STALE = "STALE"
INCOMPLETE = "INCOMPLETE"
UNVERIFIED = "UNVERIFIED"
SDEONLY = "SDEONLY"
TEXTONLY = "TEXTONLY"
CLEAN = "CLEAN"
ERROR = "ERROR"


# ----------------------------------------------------------------- pure core

def _is_word_byte(value):
    """True when the byte continues a host token, so it is not a boundary."""
    return value in WORD_BYTES


def host_needles(host):
    """Return the (ascii, utf-16-le) lower-case byte needles for a host name.

    Raises ValueError on a host this tool cannot search for, because an empty
    or non-ASCII needle would match everything or nothing silently.
    """
    if host is None or not host.strip():
        raise ValueError("host name is empty")
    lowered = host.strip().lower()
    try:
        ascii_needle = lowered.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("host name must be ASCII: %s" % host)
    return ascii_needle, lowered.encode("utf-16-le")


def token_spans(haystack, needle, wide):
    """Yield the start index of every whole-token match of needle in haystack.

    Both arguments are already lower-cased bytes. The boundary is symmetric: the
    character on either side must not be a letter, a digit or a hyphen, so
    OLDHOST-1 does not match inside OLDHOST-10 or inside MYOLDHOST-1. The end of
    the buffer is a boundary.

    wide=False  single-byte text. Inspect the neighbouring byte.
    wide=True   utf-16-le text. Inspect the neighbouring two-byte unit. A unit
                whose high byte is not zero is a non-ASCII character and is
                therefore already a boundary. The search runs at every byte
                offset, not only even ones, so the needle is found whatever
                alignment the surrounding binary file put it on.
    """
    if not needle or len(haystack) < len(needle):
        return
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return
        start = index + 1
        after = index + len(needle)
        if wide:
            if (index >= 2 and haystack[index - 1] == 0
                    and _is_word_byte(haystack[index - 2])):
                continue
            if (after + 1 < len(haystack) and haystack[after + 1] == 0
                    and _is_word_byte(haystack[after])):
                continue
        else:
            if index > 0 and _is_word_byte(haystack[index - 1]):
                continue
            if after < len(haystack) and _is_word_byte(haystack[after]):
                continue
        yield index


def find_token(haystack, needle, wide):
    """True when needle appears in haystack as a whole token. See token_spans."""
    for _ in token_spans(haystack, needle, wide):
        return True
    return False


def bytes_contain_host(raw, host):
    """True when these bytes name the host as a whole token, in either encoding.

    bytes.lower() folds only ASCII A-Z and leaves every other byte alone, so the
    one lowered buffer is valid for the ASCII search and the UTF-16LE search
    alike. That is the whole reason this is a byte scan and not a decode: a
    decode has to pick an encoding and a starting offset before it can search,
    and a .sde carries both forms in one file.
    """
    lowered = raw.lower()
    ascii_needle, wide_needle = host_needles(host)
    if find_token(lowered, ascii_needle, False):
        return True
    return find_token(lowered, wide_needle, True)


def looks_like_zip(head):
    """True when these leading bytes are a zip local file header."""
    return head[:len(ZIP_MAGIC)] == ZIP_MAGIC


def text_extensions(extra=None, defaults=TEXT_EXTENSIONS):
    """The text bucket for this run: the defaults plus whatever --text-ext adds.

    Each entry is lower-cased and given its leading dot, so "BAT", ".BAT" and
    ".bat" are one extension and not three. An extension another bucket already
    owns is refused rather than accepted and quietly ignored: an .aprx is a ZIP
    and an .sde is a binary property set, and reading either as plain text
    reports a file that plainly needs the fix as already repointed.
    """
    bucket = list(defaults)
    for item in extra or []:
        for part in item.split(","):
            name = part.strip().lower()
            if not name:
                continue
            if not name.startswith("."):
                name = "." + name
            if name in SCANNED_EXTENSIONS:
                raise ValueError(
                    "%s is not plain text and is already scanned" % name)
            if name not in bucket:
                bucket.append(name)
    return tuple(bucket)


def normalise(path):
    """Lower-case a path and make its separators comparable across platforms."""
    return path.replace("\\", "/").lower()


def skip_directory(name, path, include_temp=False):
    """True when the walk must not descend into this directory."""
    if name.lower() in EXCLUDE_NAMES:
        return True
    if not include_temp and TEMP_FRAGMENT in normalise(path):
        return True
    return False


def failure_is_root(path, root_keys):
    """True when a directory that would not enumerate was a scan root.

    A root that cannot be enumerated means the answer for this device is
    unknown, not clean. A subdirectory that cannot be enumerated is routine on
    Windows -- every protected profile does it -- and must not flip the verdict,
    or every device in the fleet reports an issue forever.
    """
    return normalise(path) in root_keys


def has_reparse_attribute(attributes):
    """True when a Windows attribute bitmask marks a reparse point.

    A junction is a reparse point but is not a symbolic link, so islink() says
    nothing about it. Descending into one lets the walk cycle forever, and
    C:\\Users\\<name>\\Documents and My Documents are junctions on every profile.
    """
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def verdict(walk, sde_is_work=False, text_is_work=False):
    """Return (state, exit code) for a finished walk. The whole decision.

    Kept in one function with no I/O so the exit code a fleet tool acts on can
    be asserted directly rather than inferred from a scan.
    """
    fixable = walk.aprx + walk.lyrx
    if fixable or (sde_is_work and walk.sde) or (text_is_work and walk.text):
        return STALE, 1
    if walk.timed_out:
        return INCOMPLETE, 1
    if walk.root_failed:
        return UNVERIFIED, 1
    if walk.unreadable_fix:
        return UNVERIFIED, 1
    # An unread text file leaves the device unproven whether or not text counts
    # as work, because it is the ordinary contract: a tree that could not be
    # finished is never reported clean. The .sde carve-out below is the one
    # exception, and it is bought by a measurement -- over a thousand
    # DBMS-authenticated files on one workstation, none of them repointable. Nothing measured says
    # a locked .py is that common, so it does not get the same excuse.
    if walk.unreadable_text:
        return UNVERIFIED, 1
    if sde_is_work and walk.unreadable_sde:
        return UNVERIFIED, 1
    # Text outranks the .sde worklist in the report: a stale .py can be
    # rewritten unattended, and a DBMS-authenticated .sde needs a person.
    if walk.text:
        return TEXTONLY, 0
    if walk.sde:
        return SDEONLY, 0
    return CLEAN, 0


# ------------------------------------------------- remediation core (pure)

def check_new_host(new, old):
    """Return the replacement host, stripped, or raise ValueError.

    Three refusals. The name must be a plain host name, because it is written
    into JSON strings and binary property sets where a quote, a backslash or a
    comma would corrupt the file. It must be ASCII for the same reason the old
    host must. And it must not contain the old host as a whole token: if
    OLDHOST-1 became OLDHOST-1.example.org, the next run would find the old host
    again and rewrite it to OLDHOST-1.example.org.example.org.
    """
    if new is None or not new.strip():
        raise ValueError("replacement host is empty")
    name = new.strip()
    try:
        raw = name.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("replacement host must be ASCII: %s" % new)
    if any(byte not in HOST_BYTES for byte in raw):
        raise ValueError("replacement host may hold only letters, digits, "
                         "'.', '-' and '_': %s" % name)
    if find_token(raw.lower(), host_needles(old)[0], False):
        raise ValueError("replacement host still names the old host as a "
                         "token, so a second run would rewrite it again")
    return name


def rewrite_host(raw, host, new):
    """Return (bytes with every whole-token host replaced, replacement count).

    The old host is found with the same rule the scan uses, in ASCII and in
    UTF-16LE. The replacement is written in the encoding it replaced, so a
    UTF-16LE .lyrx stays UTF-16LE. Two matches that overlap, as "a.a" does
    inside "a.a.a", are not both replaced: the first wins and the verification
    that follows every rewrite catches whatever is left.
    """
    lowered = raw.lower()
    ascii_needle, wide_needle = host_needles(host)
    new_ascii = new.encode("ascii")
    new_wide = new.encode("utf-16-le")
    edits = [(index, len(ascii_needle), new_ascii)
             for index in token_spans(lowered, ascii_needle, False)]
    edits += [(index, len(wide_needle), new_wide)
              for index in token_spans(lowered, wide_needle, True)]
    edits.sort()
    pieces = []
    cursor = 0
    count = 0
    for start, length, replacement in edits:
        if start < cursor:
            continue
        pieces.append(raw[cursor:start])
        pieces.append(replacement)
        cursor = start + length
        count += 1
    pieces.append(raw[cursor:])
    return b"".join(pieces), count


def backup_path(path, now, exists=os.path.exists):
    """A backup name that does not exist yet: stamped, and numbered on a clash.

    A second run in the same second must not overwrite the first backup, which
    is the only copy of the original.
    """
    base = "%s%s%s" % (path, BACKUP_MARK,
                       time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)))
    candidate = base
    number = 0
    while exists(candidate):
        number += 1
        candidate = "%s.%d" % (base, number)
    return candidate


def sde_temp_path(path):
    """Where the new connection is built, beside the old one.

    Beside it so the final rename never crosses a volume. An .sde name must end
    in .sde, so the marker goes before the extension.
    """
    stem = os.path.splitext(path)[0]
    return stem + "-stalehost-tmp.sde"


def scrub(text, secrets):
    """Replace every secret in text with ***. Longest first, so a password that
    holds another one is removed whole."""
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def parse_passwords(data):
    """Parse the JSON object on stdin into {lower-case login: password}.

    The error never quotes the input, because the input is the secret.
    """
    try:
        loaded = json.loads(data.decode("utf-8-sig"))
    except ValueError:
        loaded = None
    if not isinstance(loaded, dict) or not all(
            isinstance(value, str) and value for value in loaded.values()):
        raise ValueError("stdin must be one JSON object mapping each login to "
                         "a non-empty password string")
    # Logins match without regard to case, so two keys that differ only in case
    # name one login. Two different passwords for it cannot both be right, and
    # last-one-wins would pick one without saying so.
    result = {}
    for key, value in loaded.items():
        if result.get(key.lower(), value) != value:
            raise ValueError("stdin gives one login two different passwords "
                             "under names that differ only in case")
        result[key.lower()] = value
    return result


def sde_decision(props, host, new, passwords):
    """Decide what to do with one .sde connection. Returns (action, detail, kwargs).

    action is create, declined or needs-password. props are the strings read
    from arcpy.Describe(...).connectionProperties, so this runs with no arcpy.
    Nothing here is guessed: a connection that is not SQL Server, that is a
    branch or historical connection, that does not name the old host in its
    instance, or whose authentication mode is unknown is declined and left
    exactly as it was.
    """
    if props["dbClient"].lower() != "sqlserver":
        return "declined", "not a SQL Server connection", None
    if props["branch"] or props["historical_name"] or props["historical_timestamp"]:
        return "declined", "branch and historical connections are not repointed", None
    current = props["instance"] or props["server"]
    updated, count = rewrite_host(current.encode("utf-8"), host, new)
    if not count:
        return "declined", "the connection does not name the old host", None
    kwargs = {"database_platform": "SQL_SERVER",
              "instance": updated.decode("utf-8")}
    if props["database"]:
        kwargs["database"] = props["database"]
    if props["version"]:
        kwargs["version_type"] = "TRANSACTIONAL"
        kwargs["version"] = props["version"]
    mode = props["authentication_mode"].upper()
    if mode == "OSA":
        kwargs["account_authentication"] = "OPERATING_SYSTEM_AUTH"
        return "create", "", kwargs
    if mode != "DBMS":
        return "declined", "unknown authentication mode", None
    password = passwords.get(props["user"].lower())
    if password is None:
        return "needs-password", "a database login needs a password", None
    kwargs["account_authentication"] = "DATABASE_AUTH"
    kwargs["username"] = props["user"]
    kwargs["password"] = password
    kwargs["save_user_pass"] = "SAVE_USERNAME"
    return "create", "", kwargs


def format_sample(path, width=SAMPLE_WIDTH):
    """Truncate a sample path from the left, keeping the meaningful end."""
    if len(path) <= width:
        return path
    return "..." + path[-(width - 3):]


def summary_line(state, host, walk, pro, text_exts=TEXT_EXTENSIONS):
    """The one line a fleet console captures. Paths and counts only."""
    line = ("%s STALEHOST old=%s fix=%d (aprx=%d lyrx=%d) sde=%d text=%d "
            "scanned=%d unreadable=%d elapsed=%ds pro=%s"
            % (state, host, walk.aprx + walk.lyrx, walk.aprx, walk.lyrx,
               walk.sde, walk.text, walk.scanned,
               walk.unreadable_fix + walk.unreadable_sde + walk.unreadable_text,
               walk.elapsed, pro))
    # Printed only when they changed the verdict, so the common case stays short.
    if walk.unreadable_fix or walk.unreadable_text:
        line += " unverified=%d" % (walk.unreadable_fix + walk.unreadable_text)
    if walk.root_failed:
        line += " rootfail=yes"
    if walk.timed_out:
        line += " timeout=yes"
    # Always last, and always printed. A CLEAN that read no text at all is a
    # different claim from a CLEAN that read the scripts too, and the line has
    # to say which one it is. The grid truncates from the right, so this sits
    # behind the counts that decide the verdict.
    line += " textext=%s" % ",".join(e.lstrip(".") for e in text_exts)
    return line


class Walk(object):
    """Counts from one scan. Built with keywords so a test can state one case."""

    def __init__(self, aprx=0, lyrx=0, sde=0, unreadable_fix=0,
                 unreadable_sde=0, root_failed=False, timed_out=False,
                 scanned=0, elapsed=0, samples=None, sde_samples=None,
                 text=0, unreadable_text=0, text_samples=None):
        self.aprx = aprx
        self.lyrx = lyrx
        self.sde = sde
        self.text = text                        # stale .py, .bat, .ps1, ...
        self.unreadable_fix = unreadable_fix    # unread .aprx or .lyrx
        self.unreadable_sde = unreadable_sde    # unread .sde
        self.unreadable_text = unreadable_text  # unread text file
        self.root_failed = root_failed
        self.timed_out = timed_out
        self.scanned = scanned
        self.elapsed = elapsed
        self.samples = [] if samples is None else samples
        self.sde_samples = [] if sde_samples is None else sde_samples
        self.text_samples = [] if text_samples is None else text_samples

    def to_dict(self, state, code, host, pro, text_exts=TEXT_EXTENSIONS):
        return {
            "state": state,
            "exitCode": code,
            "oldServer": host,
            "fixable": self.aprx + self.lyrx,
            "aprx": self.aprx,
            "lyrx": self.lyrx,
            "sde": self.sde,
            "text": self.text,
            "textExtensions": list(text_exts),
            "scanned": self.scanned,
            "unreadableFixable": self.unreadable_fix,
            "unreadableSde": self.unreadable_sde,
            "unreadableText": self.unreadable_text,
            "rootFailed": self.root_failed,
            "timedOut": self.timed_out,
            "elapsedSeconds": self.elapsed,
            "proInstalled": pro == "yes",
            "samples": list(self.samples),
            "sdeSamples": list(self.sde_samples),
            "textSamples": list(self.text_samples),
        }


# ------------------------------------------------------------- files on disk

def read_bytes(path, max_bytes=MAX_FILE_BYTES):
    """Read a whole file, refusing one that is over the cap. See MAX_FILE_BYTES."""
    handle = open(path, "rb")
    try:
        raw = handle.read(max_bytes + 1)
    finally:
        handle.close()
    if len(raw) > max_bytes:
        raise ValueError("file is over the %d byte read cap" % max_bytes)
    return raw


def read_head(path, count):
    """Read the first count bytes, for a format check."""
    handle = open(path, "rb")
    try:
        return handle.read(count)
    finally:
        handle.close()


def zip_contains_host(path, host, max_entry=MAX_ENTRY_BYTES):
    """True when any entry inside this archive names the host.

    Every entry is decompressed and searched. Searching the container's own
    bytes would never match, because the host name exists only inside the
    deflated CIM JSON. That is asserted in the self-test, and it is the reason
    this tool exists instead of a recursive text search.
    """
    archive = zipfile.ZipFile(path)
    try:
        for info in archive.infolist():
            if info.file_size == 0:
                continue
            if info.file_size > max_entry:
                raise ValueError("entry %s is over the %d byte read cap"
                                 % (info.filename, max_entry))
            if bytes_contain_host(archive.read(info), host):
                return True
        return False
    finally:
        archive.close()


def file_contains_host(path, extension, host, text_exts=TEXT_EXTENSIONS):
    """Dispatch by extension. Anything else, .mxd included, is not in scope."""
    if extension == ".aprx":
        # An .aprx is always a zip of CIM parts.
        return zip_contains_host(path, host)
    if extension == ".lyrx":
        # A .lyrx is usually CIM JSON on disk and is sometimes a zip. Route on
        # the header rather than on a guess, because a zipped .lyrx read as text
        # matches nothing and reports the layer as already repointed.
        if looks_like_zip(read_head(path, len(ZIP_MAGIC))):
            return zip_contains_host(path, host)
        return bytes_contain_host(read_bytes(path), host)
    if extension == ".sde":
        # Length-prefixed binary property sets. No text layer to decode.
        return bytes_contain_host(read_bytes(path), host)
    if extension in text_exts:
        # Plain text, read as bytes and never decoded, so the file's encoding
        # is a non-problem: bytes_contain_host matches an ASCII host and a
        # UTF-16LE one alike, which is what a .ps1 saved by the ISE needs.
        return bytes_contain_host(read_bytes(path), host)
    return False


def is_reparse(entry):
    """True when this directory entry is a link the walk must not follow."""
    try:
        if entry.is_symlink():
            return True
        attributes = getattr(entry.stat(follow_symlinks=False),
                             "st_file_attributes", 0)
    except OSError:
        return True
    return has_reparse_attribute(attributes)


def scan_tree(roots, host, max_seconds=DEFAULT_MAX_SECONDS, include_temp=False,
              matcher=None, lister=None, clock=time.time,
              text_exts=TEXT_EXTENSIONS, hits=None):
    """Walk the roots and count what names the host. Never raises for one file.

    An explicit stack, not os.walk and not a recursive helper. The equivalent
    one-liner on the shell this was deployed from aborts the entire enumeration
    on the first access-denied directory and returns nothing, which a fleet tool
    reads as a healthy device. A per-directory try/except degrades instead of
    lying: the directory is skipped, the counter that says so is raised, and the
    verdict reflects it.

    hits, when a list is given, receives (path, extension) for every stale file,
    not only the capped samples. The remediation needs all of them.
    """
    if matcher is None:
        # Wrapped rather than passed straight through, so the seam a test
        # replaces keeps its three-argument shape whatever the text bucket is.
        def matcher(path, extension, target):
            return file_contains_host(path, extension, target, text_exts)
    if lister is None:
        # Replaced only by the self-test, which cannot create an access-denied
        # directory or a junction on every host it has to be green on. clock is
        # a seam for the same reason: the deadline boundary is only assertable
        # against a clock that does not move between two reads of it.
        lister = os.scandir
    walk = Walk()
    scanned_extensions = SCANNED_EXTENSIONS + tuple(text_exts)
    root_keys = set(normalise(root) for root in roots)
    started = clock()
    stack = list(roots)

    # Roots are pushed without a name check on purpose: a caller may point
    # --path at a folder that lives inside an excluded tree, which is what the
    # self-test does with its fixtures under the system temp directory.
    while stack:
        if clock() - started >= max_seconds:
            walk.timed_out = True
            break
        current = stack.pop()

        # Materialised inside the try on purpose. scandir hands back a lazy
        # iterator, so without list() the access-denied error surfaces in the
        # loop below, outside the except, and kills the whole run.
        try:
            entries = list(lister(current))
        except OSError:
            entries = []
            if failure_is_root(current, root_keys):
                walk.root_failed = True

        subdirectories = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    subdirectories.append(entry)
                    continue
            except OSError:
                continue
            extension = os.path.splitext(entry.name)[1].lower()
            if extension not in scanned_extensions:
                continue
            walk.scanned += 1
            try:
                hit = matcher(entry.path, extension, host)
            except Exception:
                # Corrupt, locked, mid-write, or a cloud placeholder that will
                # not hydrate. This is not evidence of cleanliness. An unread
                # .aprx or .lyrx leaves the device unproven and must fail toward
                # remediation. An unread .sde cannot, because the remediation
                # would not have fixed it anyway.
                if extension in INVENTORY_EXTENSIONS:
                    walk.unreadable_sde += 1
                elif extension in FIXABLE_EXTENSIONS:
                    walk.unreadable_fix += 1
                else:
                    walk.unreadable_text += 1
                continue
            if not hit:
                continue
            if hits is not None:
                hits.append((entry.path, extension))
            if extension in INVENTORY_EXTENSIONS:
                walk.sde += 1
                if len(walk.sde_samples) < SDE_SAMPLE_CAP:
                    walk.sde_samples.append(entry.path)
            elif extension in FIXABLE_EXTENSIONS:
                if extension == ".aprx":
                    walk.aprx += 1
                else:
                    walk.lyrx += 1
                if len(walk.samples) < SAMPLE_CAP:
                    walk.samples.append(entry.path)
            else:
                walk.text += 1
                if len(walk.text_samples) < TEXT_SAMPLE_CAP:
                    walk.text_samples.append(entry.path)

        for entry in subdirectories:
            if skip_directory(entry.name, entry.path, include_temp):
                continue
            if is_reparse(entry):
                continue
            stack.append(entry.path)

    walk.elapsed = int(clock() - started)
    return walk


def _import_winreg():
    import winreg  # imported here so the tool runs on a non-Windows host
    return winreg


def _pro_in_registry(subkey=PRO_REGISTRY_KEY, importer=_import_winreg):
    """True when ArcGIS Pro is registered, whatever path it was installed to."""
    try:
        winreg = importer()
    except ImportError:
        return False
    try:
        # HKLM\SOFTWARE\ESRI is a 64-bit key. Reading it from a 32-bit host
        # process, which is what a fleet agent often gives you, is redirected to
        # WOW6432Node where no ESRI key exists, so the check reported no Pro for
        # every Pro installed outside the default path. KEY_WOW64_64KEY reads
        # the real hive from either host.
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey, 0,
                             winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
    except OSError:
        return False
    key.Close()
    return True


def pro_installed(known_path=PRO_PYTHON, registry=None):
    """True when ArcGIS Pro is on this device. Reported, never a verdict input.

    Pro's absence does not change what is stale. It changes what a remediation
    could fix: .aprx and .lyrx always, .sde only with arcpy.
    """
    if os.path.isfile(known_path):
        return True
    return (registry or _pro_in_registry)()


def _import_ctypes():
    import ctypes  # imported here so the tool runs on a non-Windows host
    return ctypes


def _windows_drive_type(root, importer=_import_ctypes):
    """GetDriveTypeW for a drive root. 0, meaning unknown, off Windows."""
    try:
        return int(importer().windll.kernel32.GetDriveTypeW(root))
    except (ImportError, AttributeError, OSError):
        return 0


def fixed_drive_roots(drive_type=None, letters="ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    """Local fixed drives only.

    Removable and network drives are excluded deliberately. A mapped drive is
    somebody else's tree, and a scan that follows one reports another machine's
    projects against this device, then blocks for the full network timeout when
    the share is gone.
    """
    if drive_type is None:
        drive_type = _windows_drive_type
    roots = []
    for letter in letters:
        root = "%s:\\" % letter
        if drive_type(root) == 3:       # DRIVE_FIXED
            roots.append(root)
    return roots


def scan_roots(path, roots_source=None):
    """The roots to walk. Raises ValueError when there is nothing to scan."""
    if path:
        # A mistyped root used to produce a confident CLEAN over zero files.
        if not os.path.exists(path):
            raise ValueError("scan root does not exist: %s" % path)
        return [path]
    roots = (roots_source or fixed_drive_roots)()
    if not roots:
        raise ValueError("no local fixed drives found; pass --path")
    return roots


# ------------------------------------------------------- remediation (files)

def describe_error(exc):
    """One short line for a failure. The exception type first, then its text."""
    return ("%s: %s" % (type(exc).__name__, exc))[:200]


def result_row(path, status, detail="", backup=""):
    return {"path": path, "status": status, "detail": detail, "backup": backup}


def rewrite_zip(source_path, target_path, host, new, max_entry=MAX_ENTRY_BYTES):
    """Copy an archive entry by entry with the host replaced. Return the count.

    Names, order, time stamps, compression and attributes are kept, so the only
    difference is the bytes of the parts that named the host. Every entry is
    read through zipfile, which checks its CRC, so a damaged archive raises here
    and is never half copied.
    """
    total = 0
    source = zipfile.ZipFile(source_path)
    try:
        target = zipfile.ZipFile(target_path, "w")
        try:
            target.comment = source.comment
            for info in source.infolist():
                if info.file_size > max_entry:
                    raise ValueError("entry %s is over the %d byte read cap"
                                     % (info.filename, max_entry))
                data, count = rewrite_host(source.read(info), host, new)
                total += count
                clone = zipfile.ZipInfo(info.filename, info.date_time)
                clone.compress_type = info.compress_type
                clone.external_attr = info.external_attr
                clone.create_system = info.create_system
                clone.comment = info.comment
                target.writestr(clone, data)
        finally:
            target.close()
    finally:
        source.close()
    return total


def rewrite_into(path, extension, temp, host, new):
    """Write the rewritten copy of one project file to temp. Return the count."""
    if extension == ".aprx" or looks_like_zip(read_head(path, len(ZIP_MAGIC))):
        return rewrite_zip(path, temp, host, new)
    data, count = rewrite_host(read_bytes(path), host, new)
    _write(temp, data)
    return count


def verify_rewrite(temp, extension, host, new):
    """Refuse a rewritten file that still names the old host or lacks the new.

    This reads the finished file with the same matcher the scan uses, so the
    check is the one the next scan will make.
    """
    if file_contains_host(temp, extension, host):
        raise ValueError("the rewritten file still names the old host")
    if not file_contains_host(temp, extension, new):
        raise ValueError("the rewritten file does not name the new host")


# Win32 constants for the access list copy. SE_FILE_OBJECT names the kind of
# object, the others pick the part of the security descriptor and say whether the
# list blocks inheritance from the folder.
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x4
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
SE_DACL_PROTECTED = 0x1000


def copy_acl(source, target, importer=_import_ctypes):
    """Give target the access list that source has. Return True when it was copied.

    A new file takes the access list its folder hands down. A project or an .sde
    that someone locked to one account would come back readable by every account
    the folder trusts, and an .sde holds a stored password. So the rewritten file
    and its backup get the original's list, including whether it blocks
    inheritance. Off Windows the mode bits are the whole story, and
    shutil.copymode has already copied them, so this does nothing there. A volume
    with no access list, such as FAT, has nothing to copy. A failure raises, and
    the caller leaves the original as it was.
    """
    try:
        ctypes = importer()
        advapi = ctypes.windll.advapi32
        kernel = ctypes.windll.kernel32
    except (ImportError, AttributeError):
        return False
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    code = advapi.GetNamedSecurityInfoW(
        source, SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None,
        ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if code:
        raise OSError("cannot read the access list of the file (error %d)"
                      % code)
    try:
        if not dacl.value:
            return False
        control = ctypes.c_ushort()
        revision = ctypes.c_ulong()
        if not advapi.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise OSError("cannot read the access list flags of the file")
        if control.value & SE_DACL_PROTECTED:
            flag = PROTECTED_DACL_SECURITY_INFORMATION
        else:
            flag = UNPROTECTED_DACL_SECURITY_INFORMATION
        code = advapi.SetNamedSecurityInfoW(
            target, SE_FILE_OBJECT,
            ctypes.c_ulong(DACL_SECURITY_INFORMATION | flag),
            None, None, dacl, None)
        if code:
            raise OSError("cannot set the access list of the new file "
                          "(error %d)" % code)
    finally:
        kernel.LocalFree(descriptor)
    return True


def swap_in(path, staged, before, now, replace=os.replace, copy=shutil.copy2,
            before_swap=None, acl=copy_acl):
    """Back up path, then put staged in its place. Return the backup path.

    The order is the safety. The original is compared with the stat taken before
    the work began, so a file someone saved in the meantime is refused and not
    overwritten. The backup is compared byte for byte before anything is
    replaced. Both the backup and the new file take the original's mode and
    access list. If any step after the backup fails, as the rename does when
    another process holds the file open, the backup is removed again because
    nothing changed.
    """
    if before_swap is not None:
        before_swap()
    after = os.stat(path)
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise ValueError("the file changed while it was being rewritten")
    backup = backup_path(path, now)
    copy(path, backup)
    try:
        if not filecmp.cmp(path, backup, shallow=False):
            raise ValueError("the backup does not match the original")
        shutil.copymode(path, staged)
        acl(path, backup)
        acl(path, staged)
        replace(staged, path)
    except Exception:
        os.remove(backup)
        raise
    return backup


def repoint_file(path, extension, host, new, now, replace=os.replace,
                 copy=shutil.copy2, before_swap=None, acl=copy_acl):
    """Rewrite one .aprx or .lyrx in place. Return (count, backup path).

    The new content is built beside the original and checked before anything
    is replaced, so any failure leaves the original untouched.
    """
    before = os.stat(path)
    temp = path + TEMP_MARK
    try:
        count = rewrite_into(path, extension, temp, host, new)
        if not count:
            raise ValueError("no whole-token match to rewrite")
        verify_rewrite(temp, extension, host, new)
        backup = swap_in(path, temp, before, now, replace, copy, before_swap,
                          acl)
    finally:
        if os.path.isfile(temp):
            os.remove(temp)
    return count, backup


# --------------------------------------------------------- remediation (.sde)

def _load_arcpy():
    import arcpy  # only the worker under ArcGIS Pro's Python ever gets here
    return arcpy


def _sde_one(arcpy, path, job):
    """Describe one connection, decide, and build the new one beside it."""
    try:
        described = arcpy.Describe(path).connectionProperties
    except AttributeError:
        # Seen on ArcGIS Pro 3.6: when the server in the file does not answer,
        # Describe returns a generic object with no connectionProperties.
        return "failed", ("arcpy cannot read the connection properties. "
                          "Describe connects to the old host, which must "
                          "still answer.")
    props = dict((name, str(getattr(described, name, "") or ""))
                 for name in SDE_PROPS)
    action, detail, kwargs = sde_decision(props, job["old"], job["new"],
                                          job["passwords"])
    if action != "create":
        return action, detail
    temp = sde_temp_path(path)
    # The tool page does not say whether an existing output is overwritten, so
    # none is ever left to be.
    if os.path.exists(temp):
        os.remove(temp)
    kwargs["out_folder_path"] = os.path.dirname(temp)
    kwargs["out_name"] = os.path.basename(temp)
    arcpy.management.CreateDatabaseConnection(**kwargs)
    return "created", ""


def sde_worker(job, loader, out):
    """The child half of an .sde repoint. Runs under ArcGIS Pro's Python.

    It reads nothing but its job and writes nothing but result lines, plus the
    new connection files beside the old ones. Every message passes through
    scrub, because an error from a database driver may quote the password it
    was given.
    """
    arcpy = loader()
    secrets = list(job["passwords"].values())
    for path in job["files"]:
        try:
            status, detail = _sde_one(arcpy, path, job)
        except Exception as exc:
            status, detail = "failed", describe_error(exc)
        out.write(RESULT_MARK + json.dumps(
            {"path": path, "status": status,
             "detail": scrub(detail, secrets)}) + "\n")
    out.flush()
    return 0


def run_sde_worker(python, script, job, timeout=SDE_WORKER_SECONDS):
    """Run the worker in a child process. Return ({path: (status, detail)}, problem).

    The job, passwords included, goes down the child's stdin. It is never an
    argument and never an environment variable, either of which another account
    on the machine can read from the process list.
    """
    secrets = list(job["passwords"].values())
    try:
        child = subprocess.Popen([python, script, "--sde-worker", "--apply"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
    except OSError as exc:
        return {}, scrub(describe_error(exc), secrets)
    try:
        out, err = child.communicate(json.dumps(job).encode("utf-8"),
                                     timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        return {}, "the ArcGIS Pro Python did not finish in %d seconds" % timeout
    results = {}
    for line in out.decode("utf-8", "replace").splitlines():
        if line.startswith(RESULT_MARK):
            try:
                item = json.loads(line[len(RESULT_MARK):])
            except ValueError:
                continue
            results[item["path"]] = (item["status"], item["detail"])
    problem = ""
    if child.returncode != 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-1:]
        problem = scrub("the ArcGIS Pro Python exited %d %s"
                        % (child.returncode, "".join(tail)[:120]), secrets)
    return results, problem


def repoint_sde(paths, host, new, apply, passwords, python, now, script,
                timeout=SDE_WORKER_SECONDS):
    """Rewrite or plan every stale .sde. Returns a list of result rows."""
    if not os.path.isfile(python):
        return [result_row(path, "left", "no ArcGIS Pro Python at the "
                           "--pro-python path") for path in paths]
    if not apply:
        return [result_row(path, "would", "recreate with ArcGIS Pro Python")
                for path in paths]
    secrets = list(passwords.values())
    befores = dict((path, os.stat(path)) for path in paths)
    absolute = dict((os.path.abspath(path), path) for path in paths)
    job = {"old": host, "new": new, "files": sorted(absolute),
           "passwords": passwords}
    reported, problem = run_sde_worker(python, script, job, timeout)
    rows = []
    for full in sorted(absolute):
        path = absolute[full]
        temp = sde_temp_path(path)
        try:
            status, detail = reported.get(
                full, ("failed", problem or "the worker returned no result"))
            if status == "created":
                verify_rewrite(temp, ".sde", host, new)
                backup = swap_in(path, temp, befores[path], now)
                rows.append(result_row(path, "fixed", "recreated", backup))
            else:
                rows.append(result_row(path, status, scrub(detail, secrets)))
        except Exception as exc:
            rows.append(result_row(path, "failed",
                                   scrub(describe_error(exc), secrets)))
        finally:
            if os.path.isfile(temp):
                os.remove(temp)
    return rows


def repoint_all(hits, host, new, apply, passwords, python, now, script):
    """Plan or perform the repoint for every hit. Returns a list of result rows.

    Text files are left alone on purpose: a host name in a script is often a
    decision and not a connection. The .aprx and .lyrx are rewritten in
    process, with no ArcGIS licence. The .sde need ArcGIS Pro's Python.
    """
    rows = []
    sde = []
    for path, extension in sorted(hits):
        if extension == ".sde":
            sde.append(path)
        elif extension not in FIXABLE_EXTENSIONS:
            continue
        elif not apply:
            rows.append(result_row(path, "would", "rewrite in place"))
        else:
            try:
                count, backup = repoint_file(path, extension, host, new, now)
                rows.append(result_row(path, "fixed",
                                       "%d replacement(s)" % count, backup))
            except Exception as exc:
                rows.append(result_row(path, "failed", describe_error(exc)))
    if sde:
        rows += repoint_sde(sde, host, new, apply, passwords, python, now,
                            script)
    return rows


def read_stdin(stdin):
    """Read the password JSON from a pipe. A terminal is refused, never prompted."""
    # A process started with no stdin at all has sys.stdin set to None.
    if stdin is None or stdin.isatty():
        raise ValueError("--passwords-stdin reads a pipe, not a terminal and "
                         "not a closed stdin")
    data = getattr(stdin, "buffer", stdin).read(STDIN_CAP + 1)
    if len(data) > STDIN_CAP:
        raise ValueError("stdin is over the %d byte cap" % STDIN_CAP)
    return data


# ------------------------------------------------------------------ self-test

def _write(path, data):
    handle = open(path, "wb")
    try:
        handle.write(data)
    finally:
        handle.close()


def _zip_fixture(path, text, entry="CIMPATH/map.json"):
    """An .aprx or zipped .lyrx: CIM JSON deflated inside a zip, as Pro saves it.

    The empty entry is not decoration. Pro writes folder entries and empty parts
    into an .aprx, and a reader that hands one to the matcher wastes the read.
    """
    archive = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    try:
        archive.writestr("CIMPATH/", b"")
        archive.writestr(entry, text)
    finally:
        archive.close()


def _sde_fixture(path, host, encoding, pad=b"\x02\x00\x00\x00\x18\x00",
                 tail=b"\x00\x00\x0a\x00"):
    """Binary noise around the host string, the way a property set carries it."""
    _write(path, pad + host.encode(encoding) + tail)


# The stand-in for arcpy that the self-test puts on the path of a child process.
# It reads a connection file written as key=value lines and writes a new one the
# same way, so the whole parent and child protocol runs with no ArcGIS install.
# A behaviour key in the fixture makes it raise, hang, write nothing, or write a
# connection that still names the old host.
_ARCPY_STUB = r'''
import os
import time


class _Props(object):
    pass


_state = {}


def Describe(path):
    props = _Props()
    handle = open(path)
    text = handle.read()
    handle.close()
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            setattr(props, key, value)
    _state["behaviour"] = getattr(props, "behaviour", "")
    if _state["behaviour"] == "describe":
        raise RuntimeError("cannot open this connection file")
    if _state["behaviour"] == "noprops":
        return _Props()
    holder = _Props()
    holder.connectionProperties = props
    return holder


class management(object):
    @staticmethod
    def CreateDatabaseConnection(**kw):
        behaviour = _state["behaviour"]
        if behaviour == "raise":
            raise RuntimeError("login failed for password "
                               + kw.get("password", ""))
        if behaviour == "hang":
            time.sleep(30)
        if behaviour == "noout":
            return
        instance = "OLDHOST-1" if behaviour == "stale" else kw["instance"]
        handle = open(os.path.join(kw["out_folder_path"], kw["out_name"]), "w")
        handle.write("instance=%s\ndatabase=%s\nauth=%s\nuser=%s\n"
                     "password_len=%d\n"
                     % (instance, kw.get("database", ""),
                        kw["account_authentication"], kw.get("username", ""),
                        len(kw.get("password", ""))))
        handle.close()
'''


def _harness(out=None):
    """Return (check, raises, finish) over one pass and one fail counter.

    Built by a function and not inline so the self-test can run a second copy
    against cases that are meant to fail, and so see the failure paths work.
    """
    passed = [0]
    failed = []

    def say(text):
        print(text, file=out or sys.stdout)

    def check(cond, label):
        if cond:
            passed[0] += 1
            say("PASS  %s" % label)
        else:
            failed.append(label)
            say("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def finish():
        say("-" * 70)
        total = passed[0] + len(failed)
        if failed:
            say("%d assertions, %d failed" % (total, len(failed)))
            for label in failed:
                say("  FAILED: %s" % label)
            return 1
        say("%d assertions, 0 failed" % total)
        return 0

    return check, raises, finish


def self_test():
    """Assertions over the matchers, the walk and the verdict. No network."""
    import io
    import tempfile
    import types

    global WORKER_SCRIPT
    check, raises, finish = _harness()
    saved_script = WORKER_SCRIPT

    def run(argv, stdin=None, loader=None):
        """main() with both streams captured, returning (exit code, output)."""
        buffer = io.StringIO()
        saved = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = buffer, buffer
        try:
            code = main(argv, stdin, loader)
        finally:
            sys.stdout, sys.stderr = saved
        return code, buffer.getvalue()

    host = "OLDHOST-1"
    decoy = "OLDHOST-10"

    print("stalehost self-test: stand-in arcpy, no network, no registry write")
    print("-" * 70)

    workdir = tempfile.mkdtemp(prefix="stalehost-test-")
    try:
        # ---- the needle, and what is not a needle
        raises(lambda: host_needles(""), "an empty host name is refused")
        raises(lambda: host_needles("   "), "a blank host name is refused")
        raises(lambda: host_needles(u"OLDH\u00d6ST"),
               "a non-ASCII host name is refused rather than never matching")
        check(host_needles(host)[0] == b"oldhost-1",
              "the ASCII needle is the lower-cased host")
        check(host_needles(host)[1] == u"oldhost-1".encode("utf-16-le"),
              "the wide needle is the same name as utf-16-le")

        # ---- token boundaries. This is the rule the whole tool turns on.
        check(bytes_contain_host(b"SERVER=OLDHOST-1;DATABASE=EXAMPLEDB", host),
              "a host between separators is a hit")
        check(not bytes_contain_host(b"SERVER=OLDHOST-10;DATABASE=EXAMPLEDB", host),
              "the neighbouring server OLDHOST-10 is not a hit  <-- pinned defect")
        check(not bytes_contain_host(b"SERVER=MYOLDHOST-1;", host),
              "a longer name ending in the host is not a hit  <-- pinned defect")
        check(bytes_contain_host(b"\\\\OLDHOST-1\\share", host),
              "a UNC path names the host as a whole token")
        # A hyphen is part of a host token, not a separator. Server names come
        # in hyphenated families, so a host retired as OLDHOST must not match
        # OLDHOST-10, which is the next machine in the same rack.
        check(not bytes_contain_host(b"SERVER=OLDHOST-10;", "OLDHOST"),
              "a hyphen continues the token, so OLDHOST does not match OLDHOST-10  <-- pinned defect")
        check(not bytes_contain_host(b"SERVER=MCP-OLDHOST;", "OLDHOST"),
              "and a hyphenated prefix does not make OLDHOST a whole token  <-- pinned defect")
        check(bytes_contain_host(b"SERVER=OLDHOST-1", host),
              "the end of the buffer is a boundary")
        check(bytes_contain_host(b"server=oldhost-1,1433;", host),
              "a host followed by a port is a hit")
        check(bytes_contain_host(b"SERVER=oldhost-1\\SQLEXPRESS;", host),
              "a host followed by an instance is a hit")
        check(bytes_contain_host(b"SERVER=OldHost-1;", host),
              "the search is case-insensitive")
        check(not bytes_contain_host(b"SERVER=NEWHOST;DATABASE=EXAMPLEDB", host),
              "a connection already on the new alias is not a hit")
        check(not bytes_contain_host(b"", host),
              "an empty file is not a hit")

        # ---- utf-16le, at whatever offset the binary put it on
        wide = u"SERVER=OLDHOST-1;".encode("utf-16-le")
        check(bytes_contain_host(wide, host),
              "a utf-16-le connection string is a hit with no BOM")
        check(bytes_contain_host(b"\x00" + wide, host),
              "a utf-16-le host at an odd byte offset is still a hit  <-- pinned defect")
        check(not bytes_contain_host(u"SERVER=OLDHOST-10;".encode("utf-16-le"), host),
              "the OLDHOST-10 decoy in utf-16-le is not a hit  <-- pinned defect")
        check(bytes_contain_host(u"OLDHOST-1".encode("utf-16-le"), host),
              "a utf-16-le host at the end of the buffer is a hit")
        check(not bytes_contain_host(u"MYOLDHOST-1".encode("utf-16-le"), host),
              "a longer utf-16-le name ending in the host is not a hit")

        # ---- format routing
        check(looks_like_zip(b"PK\x03\x04rest"), "a PK header is a zip")
        check(not looks_like_zip(b'{"la'), "CIM JSON text is not a zip")
        check(not looks_like_zip(b"PK"), "a two byte file is not a zip")

        # ---- the text bucket. The gap that printed CLEAN over stale scripts.
        check(".py" in text_extensions(),
              "a .py is in the text bucket by default  <-- pinned defect")
        check(".txt" not in text_extensions(),
              "a .txt is not, because a readme naming a server is not a broken job")
        check(text_extensions(["XML"])[-1] == ".xml",
              "an added extension is lower-cased and given its leading dot")
        check(text_extensions([".py"]) == TEXT_EXTENSIONS,
              "an extension already in the bucket is not added twice")
        check(text_extensions([".txt,.config"])[-2:] == (".txt", ".config"),
              "one --text-ext value can carry a comma separated list")
        check(text_extensions([" , "]) == TEXT_EXTENSIONS,
              "an empty item is ignored rather than becoming an extension of '.'")
        raises(lambda: text_extensions([".aprx"]),
               "an extension another bucket owns is refused, not silently ignored  <-- pinned defect")
        raises(lambda: text_extensions([".sde"]),
               "and .sde too, because a property set read as text reports clean")

        # ---- real files, the three formats
        stale_lyrx = os.path.join(workdir, "stale_text.lyrx")
        decoy_lyrx = os.path.join(workdir, "decoy_text.lyrx")
        clean_lyrx = os.path.join(workdir, "clean_text.lyrx")
        wide_lyrx = os.path.join(workdir, "stale_utf16.lyrx")
        zipped_lyrx = os.path.join(workdir, "stale_zipped.lyrx")
        stale_aprx = os.path.join(workdir, "stale_zip.aprx")
        clean_aprx = os.path.join(workdir, "clean_zip.aprx")
        broken_aprx = os.path.join(workdir, "broken.aprx")
        stale_sde = os.path.join(workdir, "stale_utf16.sde")
        ascii_sde = os.path.join(workdir, "stale_ascii.sde")
        decoy_sde = os.path.join(workdir, "decoy_utf16.sde")

        connection = '{"workspaceConnectionString":"SERVER=%s;DATABASE=EXAMPLEDB"}'
        _write(stale_lyrx, (connection % host).encode("utf-8"))
        _write(decoy_lyrx, (connection % decoy).encode("utf-8"))
        _write(clean_lyrx, (connection % "NEWHOST").encode("utf-8"))
        _write(wide_lyrx, (connection % host).encode("utf-16-le"))
        _zip_fixture(zipped_lyrx, connection % host)
        _zip_fixture(stale_aprx, connection % host.lower())
        _zip_fixture(clean_aprx, connection % "NEWHOST")
        _write(broken_aprx, b"this is not a zip archive")
        _sde_fixture(stale_sde, host, "utf-16-le")
        _sde_fixture(ascii_sde, host, "ascii")
        _sde_fixture(decoy_sde, decoy, "utf-16-le")

        check(file_contains_host(stale_lyrx, ".lyrx", host),
              "a text .lyrx naming the old host is a hit")
        check(not file_contains_host(decoy_lyrx, ".lyrx", host),
              "a text .lyrx naming OLDHOST-10 is not a hit")
        check(not file_contains_host(clean_lyrx, ".lyrx", host),
              "a text .lyrx already on the new alias is not a hit")
        check(file_contains_host(wide_lyrx, ".lyrx", host),
              "a .lyrx saved as utf-16-le is still a hit  <-- pinned defect")
        check(file_contains_host(zipped_lyrx, ".lyrx", host),
              "a zipped .lyrx is routed by its PK header, not its extension  <-- pinned defect")
        check(file_contains_host(stale_aprx, ".aprx", host),
              "an .aprx whose zipped CIM json names the old host is a hit")
        check(not file_contains_host(clean_aprx, ".aprx", host),
              "an .aprx already on the new alias is not a hit")
        check(file_contains_host(stale_sde, ".sde", host),
              "an .sde carrying the host as utf-16-le is a hit  <-- pinned defect")
        check(file_contains_host(ascii_sde, ".sde", host),
              "an .sde carrying the host as ascii is a hit")
        check(not file_contains_host(decoy_sde, ".sde", host),
              "an .sde naming OLDHOST-10 in utf-16-le is not a hit  <-- pinned defect")
        check(not file_contains_host(stale_lyrx, ".mxd", host),
              "an extension outside the three in scope is never a hit")

        # The justification for the zip reader, stated as an assertion: the raw
        # container bytes do not carry the host name, so a text search over the
        # file finds nothing on a project that plainly needs the fix.
        check(not bytes_contain_host(read_bytes(stale_aprx), host),
              "the raw .aprx bytes do not contain the host name  <-- pinned defect")

        # ---- the text bucket on real files
        stale_py = os.path.join(workdir, "nightly.py")
        decoy_py = os.path.join(workdir, "neighbour.py")
        wide_ps1 = os.path.join(workdir, "sync.ps1")
        _write(stale_py, ('conn = "SERVER=%s;DATABASE=EXAMPLEDB"'
                          % host).encode("utf-8"))
        _write(decoy_py, ('conn = "SERVER=%s;DATABASE=EXAMPLEDB"'
                          % decoy).encode("utf-8"))
        _write(wide_ps1, ('$server = "%s"' % host).encode("utf-16-le"))

        check(file_contains_host(stale_py, ".py", host),
              "a .py naming the old host is a hit  <-- pinned defect")
        check(not file_contains_host(decoy_py, ".py", host),
              "the whole-token rule holds in the text bucket too")
        check(file_contains_host(wide_ps1, ".ps1", host),
              "a .ps1 saved as utf-16-le is a hit, because text is read as bytes")
        check(not file_contains_host(stale_py, ".py", host, text_exts=(".bat",)),
              "an extension this run did not include is not read  <-- pinned defect")

        # ---- read caps
        raises(lambda: read_bytes(stale_sde, max_bytes=4),
               "a file over the read cap is refused, not truncated")
        raises(lambda: zip_contains_host(stale_aprx, host, max_entry=8),
               "a zip entry over the read cap is refused, not skipped")

        # ---- the walk over a real tree
        tree = os.path.join(workdir, "tree")
        os.makedirs(os.path.join(tree, "projects"))
        os.makedirs(os.path.join(tree, ".git"))
        os.makedirs(os.path.join(tree, "node_modules"))
        os.makedirs(os.path.join(tree, "AppData", "Local", "Temp"))
        os.makedirs(os.path.join(tree, "conn.sde"))
        _write(os.path.join(tree, "readme.txt"),
               ("SERVER=%s" % host).encode("utf-8"))
        shutil.copy(stale_aprx, os.path.join(tree, "projects", "Zoning.aprx"))
        shutil.copy(stale_lyrx, os.path.join(tree, "projects", "Parcels.lyrx"))
        shutil.copy(clean_lyrx, os.path.join(tree, "projects", "Roads.lyrx"))
        shutil.copy(stale_sde, os.path.join(tree, "projects", "prod.sde"))
        shutil.copy(ascii_sde, os.path.join(tree, "legacy.sde"))
        shutil.copy(decoy_sde, os.path.join(tree, "neighbour.sde"))
        shutil.copy(stale_aprx, os.path.join(tree, ".git", "hidden.aprx"))
        shutil.copy(stale_aprx, os.path.join(tree, "node_modules", "dep.aprx"))
        shutil.copy(stale_sde, os.path.join(tree, "AppData", "Local", "Temp",
                                            "scratch.sde"))

        walk = scan_tree([tree], host)
        check(walk.scanned == 6,
              "the walk scanned the six documents and skipped the pruned trees")
        check(walk.aprx + walk.lyrx + walk.sde == 4,
              "a .txt naming the old host is out of scope and is not counted")
        check((walk.aprx, walk.lyrx, walk.sde) == (1, 1, 2),
              "the walk counted 1 aprx, 1 lyrx and 2 sde")
        check(walk.unreadable_fix == 0 and walk.unreadable_sde == 0,
              "nothing in a readable tree is counted unreadable")
        check(verdict(walk) == (STALE, 1),
              "a tree holding a stale project is STALE exit 1")
        check(all(".git" not in normalise(p) for p in walk.samples),
              "a .git directory is never walked")
        check(all("node_modules" not in normalise(p) for p in walk.samples),
              "a node_modules directory is never walked")
        check(all(TEMP_FRAGMENT not in normalise(p) for p in walk.sde_samples),
              "the AppData Local Temp scratch is pruned by default  <-- pinned defect")
        check(all(not p.endswith("conn.sde") for p in walk.sde_samples),
              "a directory named conn.sde is not counted as a file")

        temp_walk = scan_tree([tree], host, include_temp=True)
        check(temp_walk.scanned == 7 and temp_walk.sde == 3,
              "--scan-temp reaches the scratch connection the default prunes")

        # ---- the walk over a tree of scripts. Kept apart from the tree above
        # so those counts still state the three-format case on their own.
        text_tree = os.path.join(workdir, "text_tree")
        os.makedirs(text_tree)
        shutil.copy(stale_py, os.path.join(text_tree, "nightly.py"))
        shutil.copy(decoy_py, os.path.join(text_tree, "neighbour.py"))
        shutil.copy(wide_ps1, os.path.join(text_tree, "sync.ps1"))
        _write(os.path.join(text_tree, "notes.txt"),
               ("SERVER=%s" % host).encode("utf-8"))

        text_walk = scan_tree([text_tree], host)
        check(text_walk.scanned == 3 and text_walk.text == 2,
              "the walk reads the two .py and the .ps1, leaves the .txt out, and clears the decoy  <-- pinned defect")
        check(verdict(text_walk) == (TEXTONLY, 0),
              "a tree whose only stale files are scripts is TEXTONLY, never CLEAN  <-- pinned defect")
        check(verdict(text_walk, text_is_work=True) == (STALE, 1),
              "--fail-on-text makes that same tree remediable work exit 1")
        widened = scan_tree([text_tree], host,
                            text_exts=text_extensions(["txt"]))
        check(widened.scanned == 4 and widened.text == 3,
              "--text-ext reaches the .txt the default bucket leaves out")
        check(len(text_walk.text_samples) == 2 and text_walk.samples == []
              and text_walk.sde_samples == [],
              "text is sampled in its own list, so it cannot crowd out a project")

        text_many = os.path.join(workdir, "text_many")
        os.makedirs(text_many)
        for number in range(5):
            shutil.copy(stale_py, os.path.join(text_many, "job%d.py" % number))
        text_many_walk = scan_tree([text_many], host)
        # 3 is written out rather than read back from TEXT_SAMPLE_CAP, which
        # would pass for whatever that constant was changed to.
        check(text_many_walk.text == 5 and len(text_many_walk.text_samples) == 3,
              "every stale script is counted but only three are sampled")

        def refusing_text_matcher(path, extension, target):
            if extension == ".py":
                raise IOError("locked by another process")
            return file_contains_host(path, extension, target)

        unread_text = scan_tree([text_tree], host, matcher=refusing_text_matcher)
        check(unread_text.unreadable_text == 2 and unread_text.unreadable_fix == 0
              and unread_text.unreadable_sde == 0,
              "an unread script is counted in its own counter  <-- pinned defect")
        check(verdict(unread_text) == (UNVERIFIED, 1),
              "and it leaves the device unproven with no flag needed  <-- pinned defect")

        # A corrupt project is the case that must not read as clean.
        broken_tree = os.path.join(workdir, "broken_tree")
        os.makedirs(broken_tree)
        shutil.copy(broken_aprx, os.path.join(broken_tree, "Truncated.aprx"))
        broken_walk = scan_tree([broken_tree], host)
        check(broken_walk.scanned == 1 and broken_walk.unreadable_fix == 1,
              "an .aprx that will not open is counted unreadable, not clean  <-- pinned defect")
        check(verdict(broken_walk) == (UNVERIFIED, 1),
              "and the device is UNVERIFIED exit 1 rather than CLEAN")

        # An unreadable .sde is counted apart from an unreadable project,
        # because the two lead to different verdicts.
        def refusing_matcher(path, extension, target):
            if extension == ".sde":
                raise IOError("locked by another process")
            return file_contains_host(path, extension, target)

        locked_walk = scan_tree([tree], host, matcher=refusing_matcher)
        check(locked_walk.unreadable_sde == 3 and locked_walk.unreadable_fix == 0,
              "an unread .sde is counted apart from an unread project  <-- pinned defect")
        check(verdict(Walk(unreadable_sde=3)) == (CLEAN, 0),
              "an unread .sde alone stays CLEAN exit 0, so the loop ends  <-- pinned defect")

        # A root that is a file, not a directory, cannot be enumerated.
        file_root_walk = scan_tree([stale_lyrx], host)
        check(file_root_walk.root_failed and file_root_walk.scanned == 0,
              "a scan root that is not a directory is a root failure")
        check(verdict(file_root_walk) == (UNVERIFIED, 1),
              "and that device is UNVERIFIED exit 1, never CLEAN  <-- pinned defect")
        check(not failure_is_root(os.path.join(tree, "projects"),
                                  set([normalise(tree)])),
              "a subdirectory that will not enumerate is not a root failure  <-- pinned defect")
        check(failure_is_root(tree.upper(), set([normalise(tree)])),
              "a root is recognised whatever case it was typed in")

        # A directory that opens and then fails part way through being read.
        # This is the shape the shell one-liner this tool replaces gets wrong:
        # the listing is lazy, so the error surfaces in the loop that consumes
        # it rather than where it was opened. Simulated through the lister seam
        # because no portable fixture can deny the running account a directory.
        def half_read_lister(path):
            def entries():
                for entry in os.scandir(path):
                    yield entry
                raise OSError(13, "access denied part way through the listing")
            return entries()

        half = scan_tree([tree], host, lister=half_read_lister)
        check(half.root_failed is True and half.scanned == 0,
              "a listing that fails part way through degrades, it does not escape  <-- pinned defect")
        check(verdict(half) == (UNVERIFIED, 1),
              "and that device is UNVERIFIED, not a device that scanned nothing and passed")

        def denied_subdirectory_lister(path):
            if path.endswith("projects"):
                raise OSError(13, "access is denied")
            return os.scandir(path)

        denied = scan_tree([tree], host, lister=denied_subdirectory_lister)
        check(denied.root_failed is False and denied.scanned == 2,
              "one unreadable subdirectory does not flag the device, the rest is still read  <-- pinned defect")

        # A junction cannot be created on every host this has to be green on, so
        # the entry is simulated. is_reparse and the walk are the real ones.
        class FakeEntry(object):
            """The four members of an os.scandir entry that the walk uses."""

            def __init__(self, name, path, symlink=False, attributes=0,
                         stat_raises=False, dir_raises=False):
                self.name = name
                self.path = path
                self._symlink = symlink
                self._attributes = attributes
                self._stat_raises = stat_raises
                self._dir_raises = dir_raises

            def is_dir(self, follow_symlinks=True):
                if self._dir_raises:
                    raise OSError(5, "cannot tell what this entry is")
                return True

            def is_symlink(self):
                return self._symlink

            def stat(self, follow_symlinks=True):
                if self._stat_raises:
                    raise OSError(5, "cannot stat this entry")
                # os.DirEntry.stat() follows the link by default and then
                # reports the TARGET, whose attributes carry no reparse bit.
                # The fake behaves the same way, so asking for the wrong one
                # here loses the junction exactly as it would on Windows.
                attributes = 0 if follow_symlinks else self._attributes
                return type("S", (), {"st_file_attributes": attributes})()

        projects = os.path.join(tree, "projects")

        def entry_lister(entry):
            def lister(path):
                if path == tree:
                    return [entry]
                return os.scandir(path)
            return lister

        plain = FakeEntry("projects", projects)
        junction = FakeEntry("Documents", projects,
                             attributes=FILE_ATTRIBUTE_REPARSE_POINT)
        link = FakeEntry("Shortcut", projects, symlink=True)
        unstattable = FakeEntry("Odd", projects, stat_raises=True)

        check(scan_tree([tree], host, lister=entry_lister(plain)).scanned == 4,
              "an ordinary subdirectory is descended into and its four documents read")
        check(scan_tree([tree], host, lister=entry_lister(junction)).scanned == 0,
              "a junction is not descended into, so the walk cannot cycle  <-- pinned defect")
        check(scan_tree([tree], host, lister=entry_lister(link)).scanned == 0,
              "a symbolic link is not followed either")
        check(scan_tree([tree], host,
                        lister=entry_lister(unstattable)).scanned == 0,
              "an entry that will not stat is refused rather than followed blind")
        check(is_reparse(junction) is True and is_reparse(plain) is False,
              "and that decision is the reparse attribute, read off the entry")

        # A clock that does not move. The boundary is >=, not >, so a budget
        # already spent stops the walk before the first directory rather than
        # after it. Against the real clock this case only holds while the
        # system timer has not ticked, which is why the clock is a seam.
        stopped = scan_tree([tree], host, max_seconds=0, clock=lambda: 100.0)
        check(stopped.timed_out and stopped.scanned == 0,
              "a budget already spent stops the walk before the first directory  <-- pinned defect")
        timed_out = scan_tree([tree], host, max_seconds=0)
        check(timed_out.timed_out and timed_out.scanned == 0,
              "a time cap already spent stops the walk before it reads anything")
        check(verdict(timed_out) == (INCOMPLETE, 1),
              "and an unfinished scan is INCOMPLETE exit 1")
        # A clock that starts at 100 and then reads 142, so the reported elapsed
        # has to be 42. Asserting elapsed == 0 would pass for a tool that never
        # reads the clock at all.
        readings = [100.0, 100.0]

        def measured_clock():
            return readings.pop(0) if readings else 142.0

        untimed = scan_tree([tree], host, max_seconds=1000,
                            clock=measured_clock)
        check(untimed.timed_out is False and untimed.scanned == 6
              and untimed.elapsed == 42,
              "a budget still open reads the whole tree, and elapsed is the clock  <-- pinned defect")

        # Sample caps. Eight stale projects, five sampled.
        many = os.path.join(workdir, "many")
        os.makedirs(many)
        for number in range(8):
            shutil.copy(stale_lyrx, os.path.join(many, "L%d.lyrx" % number))
            shutil.copy(stale_sde, os.path.join(many, "S%d.sde" % number))
        many_walk = scan_tree([many], host)
        # The caps are written out, not read back from the constants. An
        # assertion that compares a value against the constant that produced it
        # passes for every value the constant could be given.
        check(many_walk.lyrx == 8 and len(many_walk.samples) == 5,
              "every stale project is counted but only five are sampled")
        check(many_walk.sde == 8 and len(many_walk.sde_samples) == 3,
              "stale .sde are sampled separately so they cannot crowd out projects")

        # ---- directory pruning rules, stated directly
        check(skip_directory("Windows", r"C:\Windows"),
              "the Windows directory is skipped")
        check(skip_directory(".git", "/home/gis/repo/.git"),
              "a .git directory is skipped at any depth")
        check(not skip_directory("Projects", r"D:\GIS\Projects"),
              "an ordinary project directory is walked")
        check(skip_directory("Temp", r"C:\Users\a\AppData\Local\Temp"),
              "the ArcGIS scratch connection directory is skipped by default")
        check(not skip_directory("Temp", r"C:\Users\a\AppData\Local\Temp",
                                 include_temp=True),
              "--scan-temp turns that prune off")
        check(not skip_directory("Temp", r"D:\Build\Temp"),
              "another directory called Temp is still walked")
        # 0x400 is written out rather than taken from the constant. An assertion
        # that reads the constant back passes whatever the constant was changed
        # to, and this one number is the whole junction defence.
        check(has_reparse_attribute(0x400 | 0x10),
              "a junction is refused by its reparse attribute  <-- pinned defect")
        check(not has_reparse_attribute(0x10),
              "an ordinary directory has no reparse attribute")
        check(FILE_ATTRIBUTE_REPARSE_POINT == 0x400,
              "the reparse bit is the Windows value, not whatever stat was built with")

        # ---- the verdict table. These are the exit codes a fleet tool acts on.
        check(verdict(Walk(aprx=1)) == (STALE, 1),
              "a stale .aprx alone is STALE exit 1")
        check(verdict(Walk(lyrx=1)) == (STALE, 1),
              "a stale .lyrx alone is STALE exit 1")
        check(verdict(Walk(sde=9)) == (SDEONLY, 0),
              "stale .sde alone is SDEONLY exit 0, so no remediation loop  <-- pinned defect")
        check(verdict(Walk(aprx=1, sde=9)) == (STALE, 1),
              "a pile of stale .sde does not mask one stale .aprx  <-- pinned defect")
        check(verdict(Walk(unreadable_fix=1)) == (UNVERIFIED, 1),
              "an unread project is UNVERIFIED exit 1, not CLEAN  <-- pinned defect")
        check(verdict(Walk(root_failed=True)) == (UNVERIFIED, 1),
              "a root that would not enumerate is UNVERIFIED exit 1")
        check(verdict(Walk(timed_out=True)) == (INCOMPLETE, 1),
              "a scan that ran out of time is INCOMPLETE exit 1")
        check(verdict(Walk()) == (CLEAN, 0),
              "nothing found at all is CLEAN exit 0")
        check(verdict(Walk(aprx=1, timed_out=True, root_failed=True)) == (STALE, 1),
              "found work outranks both ways a scan can fall short")
        check(verdict(Walk(sde=9), sde_is_work=True) == (STALE, 1),
              "--fail-on-sde turns the .sde worklist into remediable work")
        check(verdict(Walk(unreadable_sde=1), sde_is_work=True) == (UNVERIFIED, 1),
              "and an unread .sde then leaves the device unproven")
        check(verdict(Walk(unreadable_sde=1)) == (CLEAN, 0),
              "without the flag that same device is CLEAN")
        check(verdict(Walk(text=9)) == (TEXTONLY, 0),
              "stale text alone is TEXTONLY exit 0 until a remediation can rewrite it")
        check(verdict(Walk(text=9), text_is_work=True) == (STALE, 1),
              "--fail-on-text turns that worklist into remediable work")
        check(verdict(Walk(aprx=1, text=9)) == (STALE, 1),
              "a pile of stale scripts does not mask one stale .aprx")
        check(verdict(Walk(text=1, sde=9)) == (TEXTONLY, 0),
              "text outranks the .sde worklist, because a .py can be rewritten unattended  <-- pinned defect")
        check(verdict(Walk(unreadable_text=1)) == (UNVERIFIED, 1),
              "an unread script is UNVERIFIED with no flag, unlike an unread .sde  <-- pinned defect")
        check(TEXTONLY == "TEXTONLY",
              "the seventh state word is the one the fleet rule greps for")

        # ---- the reported line
        line = summary_line(STALE, host, Walk(aprx=1, lyrx=2, sde=3, scanned=9,
                                              elapsed=12), "yes")
        check(line.startswith(STALE),
              "the verdict word leads the line, because the grid truncates from the right  <-- pinned defect")
        check("fix=3 (aprx=1 lyrx=2)" in line and "sde=3" in line,
              "the counts an operator triages on are all on that line")
        check("unverified=" not in line and "rootfail=" not in line
              and "timeout=" not in line,
              "the three trouble markers are absent when nothing went wrong")
        trouble = summary_line(UNVERIFIED, host,
                               Walk(unreadable_fix=2, root_failed=True,
                                    timed_out=True), "no")
        check("unverified=2" in trouble and "rootfail=yes" in trouble
              and "timeout=yes" in trouble,
              "each marker appears only when it changed the verdict")
        text_line = summary_line(TEXTONLY, host, Walk(text=2, scanned=9), "yes")
        check("text=2" in text_line, "the text count is on the line")
        # The default bucket is written out, not joined from the constant. An
        # assertion built from the constant passes for any bucket it holds.
        check(text_line.endswith(" textext=py,bat,cmd,ps1,sql,ini"),
              "and the line names the extensions that were read, so a CLEAN says what it covered  <-- pinned defect")
        check(summary_line(CLEAN, host, Walk(), "yes",
                           (".py",)).endswith(" textext=py"),
              "a narrowed bucket is reported as narrowed")
        unread_line = summary_line(UNVERIFIED, host, Walk(unreadable_text=2),
                                   "yes")
        check("unreadable=2" in unread_line and "unverified=2" in unread_line,
              "an unread script is in the reported total, never lost  <-- pinned defect")
        check(format_sample("C:\\GIS\\Zoning.aprx") == "C:\\GIS\\Zoning.aprx",
              "a short sample path is printed whole")
        long_path = "C:\\GIS\\" + ("deep\\" * 40) + "Zoning.aprx"
        # 110 is written out. Reading SAMPLE_WIDTH back here would let the width
        # be changed to anything without an assertion noticing.
        check(len(format_sample(long_path)) == 110
              and format_sample(long_path).endswith("Zoning.aprx"),
              "a long one is truncated from the left, keeping the file name")
        # The boundary itself: a path of exactly the width is not truncated, and
        # one character more is. An off-by-one here silently mangles a path.
        exact = "C:\\" + "d" * 107
        check(len(exact) == 110 and format_sample(exact) == exact,
              "a path of exactly the width is printed whole, not truncated")
        check(format_sample(exact + "x").startswith("...")
              and len(format_sample(exact + "x")) == 110,
              "one character more is truncated  <-- pinned defect")

        # ---- environment reporting
        # registry=lambda: False on purpose. This machine has Pro registered, so
        # without it the assertion passes even when the known path is ignored.
        check(pro_installed(known_path=stale_lyrx, registry=lambda: False)
              is True,
              "ArcGIS Pro is reported present when its python is on disk")
        check(_pro_in_registry(subkey="SOFTWARE\\ESRI\\NoSuchKey-stalehost")
              is False,
              "a registry key that is not there reports no Pro, and does not raise")
        # A key that is certainly present, so the success path is executed too.
        # Off Windows there is no registry at all and the same call answers
        # False, which is the other half of the same contract.
        check(_pro_in_registry(subkey="SOFTWARE\\Microsoft\\Windows")
              is (os.name == "nt"),
              "a key that is there reports Pro on Windows, and no registry elsewhere")
        missing_python = os.path.join(workdir, "no-such-python.exe")
        check(pro_installed(known_path=missing_python,
                            registry=lambda: True) is True,
              "Pro installed somewhere else is still found, through the registry")
        check(pro_installed(known_path=missing_python,
                            registry=lambda: False) is False,
              "and a device with no Pro at all reports none")
        check(scan_roots(None, roots_source=lambda: ["C:\\", "D:\\"])
              == ["C:\\", "D:\\"],
              "with no --path every local fixed drive is a scan root")
        raises(lambda: scan_roots(None, roots_source=list),
               "a device that reports no fixed drive is refused, not called clean")
        check(scan_roots(tree, roots_source=list) == [tree],
              "--path overrides the drive list")
        check(fixed_drive_roots(drive_type=lambda r: 3 if r < "E" else 4,
                                letters="CDE") == ["C:\\", "D:\\"],
              "only fixed drives are scanned, never a mapped or removable one")
        check(all(len(r) == 3 and r.endswith(":\\") for r in fixed_drive_roots()),
              "every drive root handed to the walk is a drive root")

        # ---- end to end
        # The state words are written out below, not compared against the
        # constants that produce them. A fleet rule greps for the literal word,
        # so renaming a constant has to fail here.
        check((STALE, INCOMPLETE, UNVERIFIED, SDEONLY, CLEAN, ERROR)
              == ("STALE", "INCOMPLETE", "UNVERIFIED", "SDEONLY", "CLEAN",
                  "ERROR"),
              "the six state words are the ones the fleet rule greps for  <-- pinned defect")
        code, out = run([host, "--path", tree])
        check(code == 1 and out.startswith("STALE "),
              "a real scan of a stale tree exits 1 and says STALE")
        check("Zoning.aprx" in out and "prod.sde" in out,
              "and it prints the actionable paths, fixable ones first")
        check("EXAMPLEDB" not in out and "workspaceConnectionString" not in out,
              "it reports paths and counts, never file contents  <-- pinned defect")
        code, out = run(["  %s  " % host, "--path", tree])
        check(code == 1 and "old=%s " % host in out,
              "a host name typed with stray spaces is reported without them")

        # A sample path longer than the width, printed by main rather than by
        # format_sample on its own. The deep directory exists to make one.
        deep = os.path.join(workdir, "d" * 40, "d" * 40, "d" * 40)
        os.makedirs(deep)
        shutil.copy(stale_lyrx, os.path.join(deep, "Deep.lyrx"))
        code, out = run([host, "--path", os.path.join(workdir, "d" * 40)])
        hit_line = [l for l in out.splitlines() if l.startswith("  hit ")][0]
        check(len(hit_line[6:]) == 110 and hit_line.startswith("  hit ...")
              and hit_line.endswith("Deep.lyrx"),
              "a long path is truncated in the report, not only by format_sample  <-- pinned defect")

        clean_tree = os.path.join(workdir, "clean_tree")
        os.makedirs(clean_tree)
        shutil.copy(clean_lyrx, os.path.join(clean_tree, "Roads.lyrx"))
        code, out = run([host, "--path", clean_tree])
        check(code == 0 and out.startswith("CLEAN "),
              "a tree already on the new alias exits 0 and says CLEAN")

        sde_tree = os.path.join(workdir, "sde_tree")
        os.makedirs(sde_tree)
        shutil.copy(stale_sde, os.path.join(sde_tree, "prod.sde"))
        code, out = run([host, "--path", sde_tree])
        check(code == 0 and out.startswith("SDEONLY "),
              "a device holding only stale .sde exits 0, so the loop ends")
        code, out = run([host, "--path", sde_tree, "--fail-on-sde"])
        check(code == 1 and out.startswith("STALE "),
              "--fail-on-sde makes that same device exit 1  <-- pinned defect")

        code, out = run([host, "--path", sde_tree, "--json"])
        report = json.loads(out)
        check(report["state"] == SDEONLY and report["sde"] == 1
              and report["exitCode"] == 0,
              "--json carries the same verdict as the text line")
        check(report["samples"] == [] and len(report["sdeSamples"]) == 1,
              "and it separates the actionable samples from the worklist")

        # The defect this bucket was built for: a tree of scheduled scripts that
        # name the retiring host used to read scanned=0 and CLEAN.
        code, out = run([host, "--path", text_tree])
        check(code == 0 and out.startswith("TEXTONLY "),
              "a real scan of a stale script tree says TEXTONLY, never CLEAN  <-- pinned defect")
        check("  txt " in out and "nightly.py" in out and "notes.txt" not in out,
              "it prints the stale script paths and leaves the .txt out")
        check("EXAMPLEDB" not in out,
              "and it reports the path, never the connection string")
        code, out = run([host, "--path", text_tree, "--fail-on-text"])
        check(code == 1 and out.startswith("STALE "),
              "--fail-on-text makes that same device exit 1  <-- pinned defect")
        code, out = run([host, "--path", text_tree, "--text-ext", "txt",
                         "--json"])
        report = json.loads(out)
        check(report["text"] == 3 and len(report["textSamples"]) == 3
              and report["textExtensions"][-1] == ".txt",
              "--json carries the text counts, the samples and the bucket read")
        code, out = run([host, "--path", text_tree, "--text-ext", ".aprx"])
        check(code == 64,
              "a --text-ext that collides with another bucket is a usage error, exit 64")

        code, out = run([host, "--path", os.path.join(workdir, "nope")])
        check(code == 1 and out.startswith("ERROR "),
              "a mistyped scan root is an error exit 1, never a confident CLEAN  <-- pinned defect")
        code, out = run([])
        check(code == 64, "no host name is a usage error, exit 64")
        code, out = run(["   "])
        check(code == 64, "a blank host name is a usage error, exit 64")

        # ====================================================================
        # Remediation. Everything below writes only inside workdir.
        # ====================================================================
        new = "NEWHOST"

        def outcome(fn):
            """The exception fn raises, or None."""
            try:
                fn()
            except Exception as exc:
                return exc
            return None

        def snapshot(root):
            """Every file under root with its bytes, to prove nothing moved."""
            state = {}
            for base, _dirs, names in os.walk(root):
                for name in names:
                    full = os.path.join(base, name)
                    state[full] = read_bytes(full)
            return state

        def stale_copy(source, name):
            target = os.path.join(workdir, name)
            shutil.copy(source, target)
            return target

        def backups_of(name):
            return [n for n in os.listdir(workdir)
                    if n.startswith(name + BACKUP_MARK)]

        # ---- the replacement host
        check(check_new_host("  NewHost-2.example.org ", host)
              == "NewHost-2.example.org",
              "a replacement host is stripped and kept as typed")
        check(check_new_host("OLDHOST-10", host) == "OLDHOST-10",
              "a neighbouring name is a legal replacement, because the token rule clears it")
        raises(lambda: check_new_host("", host),
               "an empty replacement host is refused")
        raises(lambda: check_new_host(None, host),
               "a missing replacement host is refused")
        raises(lambda: check_new_host(u"NEW\u00d6", host),
               "a non-ASCII replacement host is refused")
        raises(lambda: check_new_host("NEW\\INST", host),
               "a backslash in the replacement is refused, because it would corrupt JSON  <-- pinned defect")
        raises(lambda: check_new_host('NEW"HOST', host),
               "a quote in the replacement is refused")
        raises(lambda: check_new_host("a,b", host),
               "a comma in the replacement is refused")
        raises(lambda: check_new_host("OLDHOST-1.example.org", host),
               "a replacement that still names the old host is refused, or a rerun would grow it  <-- pinned defect")
        raises(lambda: check_new_host("oldhost-1", host),
               "the old host itself, in any case, is refused")

        # ---- rewriting bytes
        check(rewrite_host(b"SERVER=OLDHOST-1;DB=X", host, new)
              == (b"SERVER=NEWHOST;DB=X", 1),
              "a whole-token host is replaced")
        check(rewrite_host(b"SERVER=OLDHOST-10;", host, new)
              == (b"SERVER=OLDHOST-10;", 0),
              "the neighbouring server is left byte for byte  <-- pinned defect")
        check(rewrite_host(b"server=oldhost-1", host, "NewHost")[0]
              == b"server=NewHost",
              "a match in any case is replaced by the name as typed")
        wide_in = u"SERVER=OLDHOST-1;".encode("utf-16-le")
        check(rewrite_host(wide_in, host, new)
              == (u"SERVER=NEWHOST;".encode("utf-16-le"), 1),
              "a utf-16-le host is replaced and stays utf-16-le  <-- pinned defect")
        check(rewrite_host(b"\x00" + wide_in, host, new)
              == (b"\x00" + u"SERVER=NEWHOST;".encode("utf-16-le"), 1),
              "a utf-16-le host at an odd offset is replaced too")
        both = b"a=OLDHOST-1;" + wide_in + b";z=OLDHOST-1"
        both_out, both_count = rewrite_host(both, host, new)
        check(both_count == 3 and not bytes_contain_host(both_out, host)
              and bytes_contain_host(both_out, new),
              "ASCII and utf-16-le copies in one buffer are all replaced")
        check(rewrite_host(rewrite_host(b"x=OLDHOST-1", host, new)[0],
                           host, new)[1] == 0,
              "a second pass finds nothing, so a rerun is idempotent")
        check(rewrite_host(b"a.a.a", "a.a", "N") == (b"N.a", 1),
              "two overlapping matches are not both cut, which would shred the bytes  <-- pinned defect")

        # ---- names, scrubbing and passwords
        check(backup_path("C:\\a\\Zoning.aprx", 0, exists=lambda p: False)
              == "C:\\a\\Zoning.aprx.stalehost-bak-19700101T000000Z",
              "a backup is the file name plus a UTC time stamp")
        taken = set(["p.stalehost-bak-19700101T000000Z",
                     "p.stalehost-bak-19700101T000000Z.1"])
        check(backup_path("p", 0, exists=lambda c: c in taken)
              == "p.stalehost-bak-19700101T000000Z.2",
              "a backup name that is taken is numbered, never overwritten  <-- pinned defect")
        check(sde_temp_path("C:\\a\\b.sde") == "C:\\a\\b-stalehost-tmp.sde",
              "the new connection is built beside the old one and still ends in .sde")
        check(scrub("x abcdef y abc", ["abc", "abcdef"]) == "x *** y ***",
              "a password that holds another is removed whole, longest first  <-- pinned defect")
        check(parse_passwords(b'\xef\xbb\xbf{"SDE_User": "pw1", "Other": "p w2"}')
              == {"sde_user": "pw1", "other": "p w2"},
              "logins are matched without regard to case, and a BOM is tolerated")
        check(parse_passwords(b'{"Sa": "same", "sa": "same"}') == {"sa": "same"},
              "one login in two cases with the same password is one login")
        for label, data in (
                ("not JSON", b"not json at all TOPSECRET"),
                ("one login twice with different passwords",
                 b'{"Sa": "TOPSECRET", "sa": "TOPSECRET2"}'),
                ("a list", b'["TOPSECRET"]'),
                ("a number for a password", b'{"u": 5, "m": "TOPSECRET"}'),
                ("an empty password", b'{"u": ""}'),
                ("not utf-8", b"\xff\xfe TOPSECRET")):
            problem = outcome(lambda d=data: parse_passwords(d))
            check(isinstance(problem, ValueError)
                  and "TOPSECRET" not in str(problem),
                  "stdin that is %s is refused without echoing it  <-- pinned defect"
                  % label)

        # ---- the decision for one .sde
        def props(**changes):
            base = dict((name, "") for name in SDE_PROPS)
            base.update(dbClient="sqlserver", instance="OLDHOST-1\\SQLEXPRESS",
                        database="EXAMPLEDB", authentication_mode="OSA")
            base.update(changes)
            return base

        action, detail, kwargs = sde_decision(props(), host, new, {})
        check(action == "create"
              and kwargs["instance"] == "NEWHOST\\SQLEXPRESS"
              and kwargs["account_authentication"] == "OPERATING_SYSTEM_AUTH"
              and kwargs["database"] == "EXAMPLEDB"
              and kwargs["database_platform"] == "SQL_SERVER"
              and "password" not in kwargs,
              "an OS-authenticated connection is rebuilt and needs no password")
        action, detail, kwargs = sde_decision(
            props(authentication_mode="DBMS", user="SDE_User"), host, new,
            {"sde_user": "pw1"})
        check(action == "create" and kwargs["username"] == "SDE_User"
              and kwargs["password"] == "pw1"
              and kwargs["account_authentication"] == "DATABASE_AUTH"
              and kwargs["save_user_pass"] == "SAVE_USERNAME",
              "a database login is rebuilt with the password for that login, matched in any case")
        action, detail, kwargs = sde_decision(
            props(authentication_mode="DBMS", user="sde_user"), host, new, {})
        check(action == "needs-password" and kwargs is None
              and "sde_user" not in detail,
              "a login with no password supplied is left alone, and the login is not printed")
        check(sde_decision(props(instance="OLDHOST-10"), host, new, {})[0]
              == "declined",
              "a connection to the neighbouring server is declined  <-- pinned defect")
        check(sde_decision(props(instance="", server="OLDHOST-1"), host, new,
                           {})[2]["instance"] == "NEWHOST",
              "the server property stands in when the instance is empty")
        check(sde_decision(props(dbClient="oracle"), host, new, {})[0]
              == "declined",
              "a connection that is not SQL Server is declined")
        check(sde_decision(props(dbClient="SqlServer"), host, new, {})[0]
              == "create",
              "the client name is compared without regard to case")
        check(sde_decision(props(branch="b1"), host, new, {})[0] == "declined"
              and sde_decision(props(historical_name="m"), host, new,
                               {})[0] == "declined"
              and sde_decision(props(historical_timestamp="t"), host, new,
                               {})[0] == "declined",
              "branch and historical connections are declined, not flattened to default")
        check(sde_decision(props(authentication_mode="AAD"), host, new,
                           {})[0] == "declined",
              "an authentication mode this tool does not know is declined")
        kwargs = sde_decision(props(version="sde.DEFAULT", database=""), host,
                              new, {})[2]
        check(kwargs["version_type"] == "TRANSACTIONAL"
              and kwargs["version"] == "sde.DEFAULT" and "database" not in kwargs,
              "a version is carried over and an empty database is left out")

        # ---- rewriting a zip
        multi = os.path.join(workdir, "multi.aprx")
        archive = zipfile.ZipFile(multi, "w", zipfile.ZIP_DEFLATED)
        archive.comment = b"project comment"
        archive.writestr("CIMPATH/", b"")
        archive.writestr("CIMPATH/map.json", connection % host)
        archive.writestr("CIMPATH/other.json", connection % "NEWHOST")
        archive.writestr(zipfile.ZipInfo("stored.txt", (2020, 1, 2, 3, 4, 6)),
                         b"SERVER=OLDHOST-1")
        archive.close()
        rewritten = os.path.join(workdir, "multi-rewritten.aprx")
        zip_count = rewrite_zip(multi, rewritten, host, new)
        old_zip = zipfile.ZipFile(multi)
        new_zip = zipfile.ZipFile(rewritten)
        check(zip_count == 2
              and new_zip.read("CIMPATH/map.json")
              == (connection % new).encode("utf-8")
              and new_zip.read("stored.txt") == b"SERVER=NEWHOST"
              and new_zip.read("CIMPATH/other.json")
              == old_zip.read("CIMPATH/other.json"),
              "every part naming the host is rewritten and the others are copied")
        check([i.filename for i in old_zip.infolist()]
              == [i.filename for i in new_zip.infolist()],
              "part names and their order are kept")
        check([(i.date_time, i.compress_type, i.external_attr)
               for i in old_zip.infolist()]
              == [(i.date_time, i.compress_type, i.external_attr)
                  for i in new_zip.infolist()],
              "time stamps, compression and attributes are kept, stored parts included")
        check(new_zip.comment == b"project comment",
              "the archive comment is kept")
        old_zip.close()
        new_zip.close()
        raises(lambda: rewrite_zip(multi, rewritten, host, new, max_entry=8),
               "a part over the read cap is refused, not skipped")
        damaged = os.path.join(workdir, "damaged.aprx")
        archive = zipfile.ZipFile(damaged, "w", zipfile.ZIP_STORED)
        archive.writestr("CIMPATH/map.json", b"SERVER=OLDHOST-1;x")
        archive.close()
        # Same length, so the archive still parses and only the CRC is wrong.
        _write(damaged, read_bytes(damaged).replace(b";x", b";y"))
        check(isinstance(outcome(lambda: rewrite_zip(damaged, rewritten, host,
                                                     new)),
                         zipfile.BadZipFile),
              "a part whose bytes no longer match its CRC stops the rewrite")

        # ---- rewriting real project files in place
        fixed_name = {}
        for label, source, extension in (
                ("an .aprx", stale_aprx, ".aprx"),
                ("a text .lyrx", stale_lyrx, ".lyrx"),
                ("a utf-16-le .lyrx", wide_lyrx, ".lyrx"),
                ("a zipped .lyrx", zipped_lyrx, ".lyrx")):
            name = "fix_%d%s" % (len(fixed_name), extension)
            target = stale_copy(source, name)
            original = read_bytes(target)
            count, backup = repoint_file(target, extension, host, new, 0)
            fixed_name[label] = target
            check(count == 1 and read_bytes(backup) == original
                  and os.path.basename(backup).startswith(name + BACKUP_MARK)
                  and not file_contains_host(target, extension, host)
                  and file_contains_host(target, extension, new)
                  and not os.path.exists(target + TEMP_MARK),
                  "%s is rewritten, backed up byte for byte, and leaves no temp file"
                  % label)
        check(read_bytes(fixed_name["a text .lyrx"])
              == (connection % new).encode("utf-8"),
              "the rewritten text .lyrx differs from the original only in the host")
        check(read_bytes(fixed_name["a utf-16-le .lyrx"])
              == (connection % new).encode("utf-16-le"),
              "and the utf-16-le one is still utf-16-le  <-- pinned defect")
        target = fixed_name["an .aprx"]
        original = read_bytes(target)
        problem = outcome(lambda: repoint_file(target, ".aprx", host, new, 0))
        check(isinstance(problem, ValueError) and read_bytes(target) == original,
              "a file already repointed is refused on a rerun and left alone  <-- pinned defect")
        target = stale_copy(decoy_lyrx, "decoy_fix.lyrx")
        original = read_bytes(target)
        problem = outcome(lambda: repoint_file(target, ".lyrx", host, new, 0))
        check(isinstance(problem, ValueError) and read_bytes(target) == original
              and not backups_of("decoy_fix.lyrx"),
              "a file that names only the neighbouring server is never touched, and gets no backup")
        target = fixed_name["a text .lyrx"]
        first_backup = backups_of(os.path.basename(target))[0]
        shutil.copy(stale_lyrx, target)
        count, second_backup = repoint_file(target, ".lyrx", host, new, 0)
        check(second_backup.endswith(".1")
              and os.path.exists(os.path.join(workdir, first_backup)),
              "a second backup in the same second is numbered and the first survives  <-- pinned defect")
        target = stale_copy(broken_aprx, "broken_fix.aprx")
        problem = outcome(lambda: repoint_file(target, ".aprx", host, new, 0))
        check(isinstance(problem, zipfile.BadZipFile)
              and read_bytes(target) == b"this is not a zip archive"
              and not os.path.exists(target + TEMP_MARK),
              "a project that will not open fails and is left exactly as it was")

        def refusing_replace(staged, destination):
            raise OSError(13, "the file is open in another process")

        target = stale_copy(stale_lyrx, "locked.lyrx")
        original = read_bytes(target)
        problem = outcome(lambda: repoint_file(target, ".lyrx", host, new, 0,
                                               replace=refusing_replace))
        check(isinstance(problem, OSError) and read_bytes(target) == original
              and not os.path.exists(target + TEMP_MARK)
              and not backups_of("locked.lyrx"),
              "a rename that fails leaves the file, the temp file and the backups as they were  <-- pinned defect")

        target = stale_copy(stale_lyrx, "moving.lyrx")
        edited = b'{"edited": "by a person, with more bytes in it"}'
        problem = outcome(lambda: repoint_file(
            target, ".lyrx", host, new, 0,
            before_swap=lambda: _write(target, edited)))
        check(isinstance(problem, ValueError) and read_bytes(target) == edited
              and not backups_of("moving.lyrx"),
              "a file saved while it was being rewritten is not overwritten  <-- pinned defect")

        def short_copy(source, destination):
            _write(destination, b"short")

        target = stale_copy(stale_lyrx, "badcopy.lyrx")
        original = read_bytes(target)
        problem = outcome(lambda: repoint_file(target, ".lyrx", host, new, 0,
                                               copy=short_copy))
        check(isinstance(problem, ValueError) and read_bytes(target) == original
              and not backups_of("badcopy.lyrx"),
              "a backup that does not match the original stops the swap  <-- pinned defect")

        # ---- the access list. A new file takes its folder's list unless told
        # otherwise, which widened a locked project. The stand-in below plays
        # the Win32 calls, so the same cases run on every host.
        class _Pointer(object):
            def __init__(self):
                self.value = None

        class _Win32(object):
            """Records the calls and returns the codes it was told to."""

            def __init__(self, dacl=1, read=0, flags=0, write=0, ok=1):
                self.dacl, self.read, self.flags = dacl, read, flags
                self.write, self.ok = write, ok
                self.calls = []
                self.freed = []
                self.c_void_p = _Pointer
                self.c_ushort = _Pointer
                self.c_ulong = lambda value=0: value
                self.byref = lambda holder: holder
                self.windll = types.SimpleNamespace(
                    advapi32=types.SimpleNamespace(
                        GetNamedSecurityInfoW=self.get_info,
                        GetSecurityDescriptorControl=self.get_control,
                        SetNamedSecurityInfoW=self.set_info),
                    kernel32=types.SimpleNamespace(LocalFree=self.freed.append))

            def get_info(self, path, kind, part, owner, group, dacl, sacl, sd):
                self.calls.append(("get", path))
                dacl.value = self.dacl
                sd.value = 99
                return self.read

            def get_control(self, descriptor, control, revision):
                control.value = self.flags
                return self.ok

            def set_info(self, path, kind, part, owner, group, dacl, sacl):
                self.calls.append(("set", path, part, dacl.value))
                return self.write

        fake = _Win32(flags=SE_DACL_PROTECTED)
        check(copy_acl("a", "b", importer=lambda: fake) is True
              and fake.calls[1] == ("set", "b", DACL_SECURITY_INFORMATION
                                    | PROTECTED_DACL_SECURITY_INFORMATION, 1)
              and len(fake.freed) == 1,
              "a list that blocks inheritance is copied as one that blocks it  <-- pinned defect")
        fake = _Win32()
        copy_acl("a", "b", importer=lambda: fake)
        check(fake.calls[1][2] == DACL_SECURITY_INFORMATION
              | UNPROTECTED_DACL_SECURITY_INFORMATION,
              "a list that inherits is copied as one that inherits")
        fake = _Win32(dacl=0)
        check(copy_acl("a", "b", importer=lambda: fake) is False
              and len(fake.calls) == 1 and len(fake.freed) == 1,
              "a volume with no access list is left alone, never given an empty one")
        for label, broken in (("read", _Win32(read=5)),
                              ("flag", _Win32(ok=0)),
                              ("set", _Win32(write=5))):
            problem = outcome(lambda w=broken: copy_acl(
                "a", "b", importer=lambda: w))
            check(isinstance(problem, OSError) and len(broken.freed) == (
                0 if label == "read" else 1),
                  "a failed access list %s raises, and a descriptor that was handed out is freed  <-- pinned defect"
                  % label)

        def no_ctypes():
            raise ImportError("no ctypes")

        check(copy_acl("a", "b", importer=no_ctypes) is False
              and copy_acl("a", "b", importer=lambda: types.SimpleNamespace())
              is False,
              "off Windows there is no access list to copy and nothing happens")

        def refusing_acl(source, destination):
            raise OSError(5, "access denied")

        target = stale_copy(stale_lyrx, "acl_fail.lyrx")
        original = read_bytes(target)
        problem = outcome(lambda: repoint_file(target, ".lyrx", host, new, 0,
                                               acl=refusing_acl))
        check(isinstance(problem, OSError) and read_bytes(target) == original
              and not os.path.exists(target + TEMP_MARK)
              and not backups_of("acl_fail.lyrx"),
              "a file whose access list cannot be copied is left as it was, with no backup  <-- pinned defect")

        def listing(path):
            """The access list as icacls prints it, minus the file name."""
            text = subprocess.check_output(["icacls", path]).decode("mbcs")
            return sorted(line.split(path)[-1].strip()
                          for line in text.splitlines()[:-2] if line.strip())

        real_acl_ok = True
        if sys.platform == "win32":
            locked = stale_copy(stale_lyrx, "locked_down.lyrx")
            subprocess.check_call(["icacls", locked, "/inheritance:r"],
                                  stdout=subprocess.PIPE)
            me = os.environ["USERNAME"]
            subprocess.check_call(["icacls", locked, "/grant:r", me + ":(F)"],
                                  stdout=subprocess.PIPE)
            before_list = listing(locked)
            count, kept = repoint_file(locked, ".lyrx", host, new, 0)
            real_acl_ok = (len(before_list) == 1
                           and listing(locked) == before_list
                           and listing(kept) == before_list)
        check(real_acl_ok,
              "a project locked to one account is still locked after the rewrite, and so is its backup (read from icacls on Windows)  <-- pinned defect")
        raises(lambda: verify_rewrite(stale_lyrx, ".lyrx", host, new),
               "a rewrite that still names the old host is refused")
        raises(lambda: verify_rewrite(decoy_lyrx, ".lyrx", host, new),
               "and one that never names the new host is refused too")
        names_both = os.path.join(workdir, "names_both.lyrx")
        _write(names_both, (connection % host).encode("utf-8")
               + b" alias=NEWHOST")
        raises(lambda: verify_rewrite(names_both, ".lyrx", host, new),
               "and one that names the new host and still the old is refused  <-- pinned defect")
        check(outcome(lambda: verify_rewrite(clean_lyrx, ".lyrx", host, new))
              is None,
              "a rewrite that names only the new host passes")

        # ---- the walk hands back every stale path
        hit_list = []
        scan_tree([tree], host, hits=hit_list)
        check(len(hit_list) == 4
              and sorted(e for _p, e in hit_list)
              == [".aprx", ".lyrx", ".sde", ".sde"],
              "the walk returns every stale path with its extension, not only the samples")
        many_hits = []
        scan_tree([many], host, hits=many_hits)
        check(len(many_hits) == 16,
              "all sixteen stale files are returned though only eight are sampled  <-- pinned defect")
        check(scan_tree([tree], host, lister=entry_lister(
            FakeEntry("Odd", projects, dir_raises=True))).scanned == 0,
              "an entry that will not say whether it is a directory is skipped, not fatal")

        # ---- the .sde worker, against a stand-in for arcpy
        stub_dir = os.path.join(workdir, "stub")
        os.makedirs(stub_dir)
        _write(os.path.join(stub_dir, "arcpy.py"), _ARCPY_STUB.encode("ascii"))
        stub_module = types.ModuleType("arcpy")
        exec(_ARCPY_STUB, stub_module.__dict__)
        # A copy of this file beside the stand-in, so the child finds the stand-in
        # first whichever Python runs the test.
        script = os.path.join(stub_dir, "stalehost_copy.py")
        shutil.copy(os.path.abspath(__file__), script)
        WORKER_SCRIPT = script
        # Joined at run time: the copy of this file in the stub folder is read by
        # the password search below, and must not hold the secret itself.
        secret = "S3cret" + "Pw"

        def stub_sde(path, **properties):
            lines = ["%s=%s" % item for item in sorted(properties.items())]
            _write(path, ("\n".join(lines) + "\n").encode("ascii"))

        osa = dict(dbClient="sqlserver", instance="OLDHOST-1\\SQLEXPRESS",
                   database="EXAMPLEDB", authentication_mode="OSA")
        dbms = dict(osa, authentication_mode="DBMS", user="sde_user")

        proc_root = os.path.join(workdir, "proc")
        os.makedirs(proc_root)
        proc = dict((name, os.path.join(proc_root, name + ".sde"))
                    for name in ("ok", "dec", "boom", "desc"))
        stub_sde(proc["ok"], **osa)
        _write(sde_temp_path(proc["ok"]), b"a leftover from a crashed run")
        stub_sde(proc["dec"], **dict(osa, instance="OTHERHOST-9"))
        stub_sde(proc["boom"], **dict(dbms, behaviour="raise"))
        stub_sde(proc["desc"], **dict(osa, behaviour="describe"))
        job = {"old": host, "new": new, "passwords": {"sde_user": secret},
               "files": [proc[k] for k in ("ok", "dec", "boom", "desc")]}
        sink = io.StringIO()
        check(sde_worker(job, lambda: stub_module, sink) == 0,
              "the worker finishes with exit 0 whatever happened to each file")
        seen = {}
        for line in sink.getvalue().splitlines():
            item = json.loads(line[len(RESULT_MARK):])
            seen[item["path"]] = item
        check([seen[proc[k]]["status"] for k in ("ok", "dec", "boom", "desc")]
              == ["created", "declined", "failed", "failed"],
              "the worker reports one status per file: created, declined, failed, failed")
        check(secret not in sink.getvalue() and "***" in seen[proc["boom"]]["detail"],
              "an error that quotes the password has it scrubbed before it is printed  <-- pinned defect")
        check(b"instance=NEWHOST\\SQLEXPRESS"
              in read_bytes(sde_temp_path(proc["ok"])),
              "a leftover temp connection is removed before a new one is built")
        check(not os.path.exists(sde_temp_path(proc["dec"])),
              "a declined connection builds nothing")
        unreachable = os.path.join(proc_root, "np.sde")
        stub_sde(unreachable, **dict(osa, behaviour="noprops"))
        sink = io.StringIO()
        sde_worker(dict(job, files=[unreachable]), lambda: stub_module, sink)
        item = json.loads(sink.getvalue()[len(RESULT_MARK):])
        check(item["status"] == "failed" and "must still answer" in item["detail"]
              and not os.path.exists(sde_temp_path(unreachable)),
              "a connection whose server does not answer is a clear failure, not a traceback  <-- pinned defect")

        late = os.path.join(proc_root, "late.sde")
        stub_sde(late, **osa)
        late_job = json.dumps(dict(job, files=[late])).encode("utf-8")
        code, out = run(["--sde-worker"], stdin=io.TextIOWrapper(
            io.BytesIO(late_job)), loader=lambda: stub_module)
        check(code == 64 and not os.path.exists(sde_temp_path(late)),
              "the worker mode refuses to run without --apply, so nothing writes without it  <-- pinned defect")
        code, out = run(["--sde-worker", "--apply"], stdin=io.TextIOWrapper(
            io.BytesIO(late_job)), loader=lambda: stub_module)
        check(code == 0 and out.startswith(RESULT_MARK)
              and os.path.exists(sde_temp_path(late)),
              "the worker mode reads its job from stdin and answers on stdout")
        # The import is real, so a stand-in is put where the import looks.
        real_arcpy = sys.modules.get("arcpy")
        sys.modules["arcpy"] = stub_module
        try:
            loaded = _load_arcpy()
        finally:
            sys.modules.pop("arcpy", None)
            sys.modules.update({"arcpy": real_arcpy} if real_arcpy else {})
        check(loaded is stub_module,
              "the lazy arcpy import returns whatever module the interpreter resolves")

        # ---- running the worker as a child process
        crash = os.path.join(workdir, "crash_worker.py")
        # The secret is joined at run time so the script file never holds it.
        _write(crash, b"import sys\nsys.stderr.write('boom S3cret' + 'Pw\\n')\nsys.exit(3)\n")
        results, problem = run_sde_worker(
            sys.executable, crash, {"passwords": {"u": secret}, "files": []})
        check(results == {} and "exited 3" in problem and secret not in problem
              and "***" in problem,
              "a worker that dies is reported with its exit code and the password scrubbed  <-- pinned defect")
        noisy = os.path.join(workdir, "noisy_worker.py")
        _write(noisy, (
            "import json\nprint('banner from a driver')\n"
            "print('STALEHOST-RESULT {not json')\n"
            "print('STALEHOST-RESULT ' + json.dumps({'path': 'p1', "
            "'status': 'declined', 'detail': 'd'}))\n").encode("ascii"))
        results, problem = run_sde_worker(
            sys.executable, noisy, {"passwords": {}, "files": ["p1"]})
        check(results == {"p1": ("declined", "d")} and problem == "",
              "output that is not a result line is ignored, and so is a result line that is not JSON")
        results, problem = run_sde_worker(
            os.path.join(workdir, "no-such-python.exe"), script,
            {"passwords": {secret: secret}, "files": []})
        check(results == {} and problem != "" and secret not in problem,
              "a Python that will not start is reported, not raised")

        silent = os.path.join(workdir, "silent_worker.py")
        _write(silent, b"pass\n")
        small = os.path.join(workdir, "small.sde")
        stub_sde(small, **osa)
        rows = repoint_sde([small], host, new, True, {}, sys.executable, 0,
                           silent)
        check(rows[0]["status"] == "failed"
              and "no result" in rows[0]["detail"],
              "a file the worker never answered for is a failure, not a success")
        marker = os.path.join(workdir, "child_ran.txt")
        marker_script = os.path.join(workdir, "marker_worker.py")
        _write(marker_script, ("open(%r, 'w').close()\n" % marker).encode("ascii"))
        rows = repoint_sde([small], host, new, False, {}, sys.executable, 0,
                           marker_script)
        check(rows[0]["status"] == "would" and not os.path.exists(marker),
              "a plan never starts the ArcGIS Pro Python  <-- pinned defect")
        run_sde_worker(sys.executable, marker_script,
                       {"passwords": {}, "files": []})
        check(os.path.exists(marker),
              "and the marker script does write when it is started, so that case could fail")
        hang = os.path.join(workdir, "hang.sde")
        stub_sde(hang, **dict(osa, behaviour="hang"))
        original = read_bytes(hang)
        rows = repoint_sde([hang], host, new, True, {}, sys.executable, 0,
                           script, timeout=1)
        check(rows[0]["status"] == "failed"
              and "did not finish" in rows[0]["detail"]
              and read_bytes(hang) == original,
              "a worker that hangs on a dead database is killed and counted, and the file is untouched  <-- pinned defect")

        # ---- a whole tree, end to end, through main()
        pw_stdin = lambda: io.TextIOWrapper(io.BytesIO(
            json.dumps({"SDE_USER": secret}).encode("utf-8")))

        def build_apply_tree(name):
            root = os.path.join(workdir, name)
            os.makedirs(os.path.join(root, "projects"))
            for source, leaf in ((stale_aprx, "Zoning.aprx"),
                                 (stale_lyrx, "Parcels.lyrx"),
                                 (wide_lyrx, "Wide.lyrx"),
                                 (zipped_lyrx, "Zipped.lyrx"),
                                 (clean_lyrx, "Roads.lyrx"),
                                 (decoy_lyrx, "Decoy.lyrx")):
                shutil.copy(source, os.path.join(root, "projects", leaf))
            shutil.copy(stale_py, os.path.join(root, "nightly.py"))
            for leaf, properties in (
                    ("os", osa), ("db", dbms),
                    ("nopw", dict(dbms, user="other_user")),
                    ("elsewhere", dict(osa, instance="OTHERHOST-9",
                                       note="was OLDHOST-1")),
                    ("raises", dict(dbms, behaviour="raise")),
                    ("noout", dict(osa, behaviour="noout")),
                    ("stale", dict(osa, behaviour="stale"))):
                stub_sde(os.path.join(root, leaf + ".sde"), **properties)
            return root

        apply_root = build_apply_tree("apply_tree")
        before_state = snapshot(apply_root)
        common = [host, "--path", apply_root, "--to", new, "--pro-python",
                  sys.executable]
        code, out = run(common + ["--json"])
        plan = json.loads(out)
        check(snapshot(apply_root) == before_state,
              "without --apply nothing on disk changes, not one byte and not one new file  <-- pinned defect")
        check(plan["repoint"]["apply"] is False and code == 1
              and plan["repoint"]["counts"]["would"] == 11
              and plan["state"] == "STALE",
              "the plan covers the 4 project files and the 7 connections and exits with the scan verdict")
        check(not any(r["path"].endswith("nightly.py")
                      for r in plan["repoint"]["results"]),
              "a script that names the host is left alone, not rewritten")
        code, out = run(common)
        check("  repoint to=NEWHOST mode=plan " in out
              and "  would ... 6 more" in out and out.count("\n  would ") == 6,
              "the text plan caps its rows per status and says how many more")
        code, out = run(common[:-1] + [os.path.join(workdir, "no-such-python.exe"),
                                       "--json"])
        counts = json.loads(out)["repoint"]["counts"]
        check(counts["would"] == 4 and counts["left"] == 7,
              "with no ArcGIS Pro Python the connections are left, and the projects are still planned")

        code, out = run(common + ["--apply", "--passwords-stdin", "--json"],
                        stdin=pw_stdin())
        report = json.loads(out)
        check(report["repoint"]["counts"]
              == {"failed": 3, "fixed": 6, "would": 0, "declined": 1,
                  "needs-password": 1, "left": 0},
              "the apply run fixes 4 projects and 2 connections, declines 1, and reports 3 failures and 1 missing password")
        check(report["state"] == "TEXTONLY" and report["fixable"] == 0
              and code == 0,
              "the verdict is read again after the rewrite: no stale project is left")
        everything = snapshot(workdir)
        check(not any(secret.encode("ascii") in data
                      for data in everything.values())
              and secret not in out,
              "the password is in no file the run wrote and not in its output  <-- pinned defect")
        check(any(r["path"].endswith("raises.sde") and "***" in r["detail"]
                  for r in report["repoint"]["results"]),
              "and the error that quoted it came back scrubbed")
        written = [p for p in snapshot(apply_root)]
        check(len([p for p in written if BACKUP_MARK in p]) == 6
              and not [p for p in written if p.endswith(TEMP_MARK)
                       or p.endswith("-stalehost-tmp.sde")],
              "six backups were taken, one per file fixed, and no temp file is left behind")
        os_sde = read_bytes(os.path.join(apply_root, "os.sde"))
        db_sde = read_bytes(os.path.join(apply_root, "db.sde"))
        check(b"instance=NEWHOST\\SQLEXPRESS" in os_sde
              and b"password_len=8" in db_sde and b"user=sde_user" in db_sde,
              "the rebuilt connections name the new host, and the password reached the driver")
        check(all(read_bytes(os.path.join(apply_root, leaf + ".sde"))
                  == before_state[os.path.join(apply_root, leaf + ".sde")]
                  for leaf in ("nopw", "elsewhere", "raises", "noout", "stale")),
              "every connection that was declined or failed is byte for byte what it was")
        once = len([p for p in snapshot(apply_root) if BACKUP_MARK in p])
        code, out = run(common + ["--apply", "--passwords-stdin", "--json",
                                  "--fail-on-sde"], stdin=pw_stdin())
        again = json.loads(out)
        check(again["repoint"]["counts"]["fixed"] == 0
              and len([p for p in snapshot(apply_root) if BACKUP_MARK in p])
              == once,
              "a second run fixes nothing and takes no new backup  <-- pinned defect")
        check(code == 1 and again["state"] == "STALE",
              "--fail-on-sde makes the connections that could not be rebuilt exit 1")

        no_pw_root = build_apply_tree("no_password_tree")
        os.environ["SDE_USER_PW"] = secret
        os.environ["SDE_USER_PASSWORD"] = secret
        try:
            code, out = run([host, "--path", no_pw_root, "--to", new,
                             "--pro-python", sys.executable, "--apply",
                             "--json"])
        finally:
            del os.environ["SDE_USER_PW"]
            del os.environ["SDE_USER_PASSWORD"]
        counts = json.loads(out)["repoint"]["counts"]
        check(counts["needs-password"] == 3 and counts["failed"] == 2
              and secret not in out,
              "a password in the environment is ignored: the logins still wait for stdin  <-- pinned defect")

        squat_root = os.path.join(workdir, "squat_tree")
        os.makedirs(squat_root)
        squat = os.path.join(squat_root, "Parcels.lyrx")
        shutil.copy(stale_lyrx, squat)
        os.makedirs(squat + TEMP_MARK)
        code, out = run([host, "--path", squat_root, "--to", new, "--apply"])
        check(code == 1 and out.startswith("STALE ") and "failed=1" in out
              and read_bytes(squat) == read_bytes(stale_lyrx),
              "a project that could not be rewritten keeps the run at exit 1 and is left intact")

        # ---- usage errors
        class Terminal(object):
            def isatty(self):
                return True

        raises(lambda: read_stdin(Terminal()),
               "a terminal is refused, never prompted for a password")
        raises(lambda: read_stdin(None),
               "a closed stdin is refused, not a traceback  <-- pinned defect")
        saved_stdin = sys.stdin
        sys.stdin = None
        try:
            code, out = run([host, "--path", tree, "--to", new, "--apply",
                             "--passwords-stdin"])
        finally:
            sys.stdin = saved_stdin
        check(code == 64 and "Traceback" not in out and "closed stdin" in out,
              "a run with no stdin at all exits 64 with one line  <-- pinned defect")
        check(read_stdin(io.BytesIO(b"abc")) == b"abc",
              "a pipe is read whole")
        raises(lambda: read_stdin(io.BytesIO(b"x" * 1048577)),
               "a pipe over one megabyte is refused")
        code, out = run([host, "--apply"])
        check(code == 64, "--apply without --to is a usage error, exit 64")
        code, out = run([host, "--passwords-stdin"])
        check(code == 64, "--passwords-stdin without --to is a usage error")
        unread = pw_stdin()
        code, out = run([host, "--to", new, "--passwords-stdin", "--path", tree],
                        stdin=unread)
        check(code == 64 and unread.buffer.tell() == 0,
              "--passwords-stdin without --apply is a usage error, and stdin is not read  <-- pinned defect")
        code, out = run([host, "--to", "OLDHOST-1.example.org", "--path", tree])
        check(code == 64 and "token" in out,
              "a replacement that still names the old host is a usage error")
        code, out = run([host, "--to", new, "--apply", "--passwords-stdin",
                         "--path", tree], stdin=io.TextIOWrapper(
                             io.BytesIO(b"{not json TOPSECRET")))
        check(code == 64 and "TOPSECRET" not in out,
              "unreadable stdin is a usage error that does not echo it")
        code, out = run([host, "--to", new, "--apply", "--passwords-stdin",
                         "--path", tree], stdin=Terminal())
        check(code == 64, "a terminal on stdin is a usage error")
        code, out = run([host, "--path", os.path.join(workdir, "missing-" + secret),
                         "--to", new, "--apply", "--passwords-stdin"],
                        stdin=pw_stdin())
        check(code == 1 and out.startswith("ERROR ") and secret not in out
              and "***" in out,
              "an error line never carries a password, even one inside the message  <-- pinned defect")

        # ---- the pieces that need a platform
        def no_winreg():
            raise ImportError("no winreg here")

        def no_ctypes():
            raise ImportError("no ctypes here")

        check(_pro_in_registry(importer=no_winreg) is False,
              "a host with no winreg module reports no Pro and does not raise")
        check(_windows_drive_type("C:\\", importer=no_ctypes) == 0,
              "a host with no ctypes reports an unknown drive type")

        # ---- the harness itself, run against cases that are meant to fail
        meta_out = io.StringIO()
        meta_check, meta_raises, meta_finish = _harness(meta_out)

        def wrong_error():
            raise KeyError("x")

        meta_check(True, "kept")
        meta_check(False, "dropped")
        meta_raises(wrong_error, "wrong error")
        meta_raises(lambda: None, "no error")
        meta_raises(lambda: host_needles(""), "right error")
        meta_code = meta_finish()
        check(meta_code == 1 and "5 assertions, 3 failed" in meta_out.getvalue()
              and "  FAILED: dropped" in meta_out.getvalue()
              and "FAIL  no error (no error raised)" in meta_out.getvalue(),
              "the harness counts a failure, names it, and returns exit 1  <-- pinned defect")
        meta_out = io.StringIO()
        meta_check, meta_raises, meta_finish = _harness(meta_out)
        meta_check(True, "kept")
        check(meta_finish() == 0 and "1 assertions, 0 failed" in meta_out.getvalue(),
              "and a clean run returns exit 0 with the footer the README quotes")

        # ---- argument handling
        args = _parse([host])
        check(args.old_server == host, "the old host name is read")
        check(args.fail_on_sde is False, "--fail-on-sde defaults to OFF")
        check(args.scan_temp is False, "--scan-temp defaults to OFF")
        check(args.fail_on_text is False, "--fail-on-text defaults to OFF")
        check(args.text_ext is None,
              "--text-ext defaults to the documented bucket")
        check(_parse([host, "--fail-on-text"]).fail_on_text is True,
              "--fail-on-text is read")
        check(_parse([host, "--text-ext", ".txt", "--text-ext",
                      ".config"]).text_ext == [".txt", ".config"],
              "--text-ext is repeatable")
        check(args.json is False, "--json defaults to OFF")
        check(args.path is None, "--path defaults to every local fixed drive")
        check(args.max_seconds == 600,
              "--max-seconds defaults to the measured 10x headroom")
        check(_parse([host, "--fail-on-sde"]).fail_on_sde is True,
              "--fail-on-sde is read")
        check(_parse([host, "--scan-temp"]).scan_temp is True, "--scan-temp is read")
        check(_parse([host, "--json"]).json is True, "--json is read")
        check(_parse([host, "--path", "D:\\GIS"]).path == "D:\\GIS",
              "--path is read")
        check(_parse([host, "--max-seconds", "30"]).max_seconds == 30,
              "--max-seconds is read")
        check(_parse(["--self-test"]).self_test, "--self-test parses")
        # A unique prefix of a real flag is refused, not read as that flag.
        # With abbreviations on, --fail-on-t was taken for --fail-on-text.
        def parse_refused(argv):
            saved_err = sys.stderr
            sys.stderr = io.StringIO()
            try:
                _parse(argv)
            except SystemExit:
                return True
            finally:
                sys.stderr = saved_err
            return False

        check(parse_refused([host, "--fail-on-t"]),
              "a unique prefix of --fail-on-text is refused by "
              "the parser  <-- pinned defect")
        check(not parse_refused([host]), "an ordinary command line is read")
        check(args.to is None and args.apply is False
              and args.passwords_stdin is False,
              "--to, --apply and --passwords-stdin default to OFF")
        check(args.pro_python == "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python"
              "\\envs\\arcgispro-py3\\python.exe",
              "--pro-python defaults to the Python that ships with Pro")
        wanted = _parse([host, "--to", "NEWHOST", "--apply",
                         "--passwords-stdin", "--pro-python", "P"])
        check(wanted.to == "NEWHOST" and wanted.apply and wanted.passwords_stdin
              and wanted.pro_python == "P",
              "--to, --apply, --passwords-stdin and --pro-python are read")
        check(parse_refused([host, "--to", "NEWHOST", "--ap"]),
              "a unique prefix of --apply is refused by the parser, so --ap cannot write  <-- pinned defect")
        check(parse_refused([host, "--passwords"]),
              "a unique prefix of --passwords-stdin is refused by the parser  <-- pinned defect")
        check(parse_refused([host, "--password", "x"]),
              "there is no flag that takes a password  <-- pinned defect")
        check(parse_refused([host, "--pro", "P"]),
              "a unique prefix of --pro-python is refused by the parser")
        check(_parse(["--sde-worker", "--apply"]).sde_worker,
              "the internal worker flag parses")
    finally:
        WORKER_SCRIPT = saved_script
        shutil.rmtree(workdir, ignore_errors=True)

    return finish()


# ----------------------------------------------------------------------- cli

def _parse(argv):
    parser = argparse.ArgumentParser(
        prog="stalehost.py",
        allow_abbrev=False,
        description="Find every ArcGIS file on this device that still names the "
                    "host you are retiring, and never call a tree clean that "
                    "could not be finished.",
        epilog="Without --apply stalehost writes nothing, opens no database "
               "connection and never reaches the network. With --to and "
               "--apply it rewrites .aprx and .lyrx files in place and "
               "rebuilds .sde connections through ArcGIS Pro's Python, each "
               "after a backup. Exit 1 means stale content, or a scan that "
               "could not be trusted.",
    )
    parser.add_argument("old_server", nargs="?",
                        help="the host name being retired, matched as a whole "
                             "token and case-insensitively")
    parser.add_argument("--path", default=None,
                        help="scan this root only. The default is every local "
                             "fixed drive.")
    parser.add_argument("--max-seconds", dest="max_seconds", type=int,
                        default=DEFAULT_MAX_SECONDS,
                        help="stop the walk after this many seconds and report "
                             "INCOMPLETE (default: %d)" % DEFAULT_MAX_SECONDS)
    parser.add_argument("--fail-on-sde", dest="fail_on_sde", action="store_true",
                        help="treat a stale .sde as remediable work, so it sets "
                             "exit 1. Off by default: see WHY .sde IS INVENTORY "
                             "in the module docstring before turning it on.")
    parser.add_argument("--fail-on-text", dest="fail_on_text",
                        action="store_true",
                        help="treat a stale text file as remediable work, so "
                             "it sets exit 1. Off by default: the text bucket "
                             "is a guess about your fleet, and flipping the "
                             "exit code under a deployed remediation is not.")
    parser.add_argument("--text-ext", dest="text_ext", action="append",
                        metavar="EXT",
                        help="add an extension to the text bucket, repeatable "
                             "or comma separated, for example --text-ext "
                             ".txt,.config (default: %s)"
                             % ",".join(TEXT_EXTENSIONS))
    parser.add_argument("--scan-temp", dest="scan_temp", action="store_true",
                        help="include AppData\\Local\\Temp, where Pro leaves "
                             "thousands of scratch .sde connections")
    parser.add_argument("--json", action="store_true",
                        help="emit the result as JSON instead of the text line")
    parser.add_argument("--to", dest="to", default=None, metavar="NEWHOST",
                        help="the host that replaces the old one. On its own "
                             "this prints the plan and writes nothing.")
    parser.add_argument("--apply", action="store_true",
                        help="with --to, rewrite the files. This is the only "
                             "flag that writes.")
    parser.add_argument("--passwords-stdin", dest="passwords_stdin",
                        action="store_true",
                        help="with --apply, read one JSON object of database "
                             "login to password from stdin, for the .sde "
                             "connections that store a login. There is no "
                             "flag or environment variable for a password.")
    parser.add_argument("--pro-python", dest="pro_python", default=PRO_PYTHON,
                        metavar="PATH",
                        help="the ArcGIS Pro python.exe that rebuilds .sde "
                             "connections (default: %s)" % PRO_PYTHON)
    parser.add_argument("--sde-worker", dest="sde_worker", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run the offline assertions and exit")
    return parser.parse_args(argv)


def _remediation_args(args, stdin):
    """Validate --to, --apply and --passwords-stdin. Return (new host, passwords).

    Raises ValueError for any combination that would write without a target, or
    read a password it will not use.
    """
    if args.to is None:
        if args.apply or args.passwords_stdin:
            raise ValueError("--apply and --passwords-stdin need --to")
        return None, {}
    new = check_new_host(args.to, args.old_server)
    if args.passwords_stdin and not args.apply:
        raise ValueError("--passwords-stdin needs --apply")
    if not args.passwords_stdin:
        return new, {}
    return new, parse_passwords(read_stdin(stdin))


def _worker_main(args, stdin, loader):
    """The hidden --sde-worker mode. It writes, so it needs --apply like the rest."""
    if not args.apply:
        print("error: --sde-worker is internal and needs --apply",
              file=sys.stderr)
        return 64
    job = json.loads(getattr(stdin, "buffer", stdin).read().decode("utf-8"))
    return sde_worker(job, loader, sys.stdout)


ROW_ORDER = ("failed", "fixed", "would", "declined", "needs-password", "left")


def repoint_counts(rows):
    return dict((status, len([r for r in rows if r["status"] == status]))
                for status in ROW_ORDER)


def repoint_lines(new, apply, rows):
    """The repoint report: one totals line, then rows capped per status."""
    counts = repoint_counts(rows)
    lines = ["  repoint to=%s mode=%s %s" % (
        new, "apply" if apply else "plan",
        " ".join("%s=%d" % (status, counts[status]) for status in ROW_ORDER))]
    for status in ROW_ORDER:
        chosen = [r for r in rows if r["status"] == status]
        for row in chosen[:SAMPLE_CAP]:
            lines.append("  %s %s -- %s" % (status, format_sample(row["path"]),
                                            row["detail"]))
        if len(chosen) > SAMPLE_CAP:
            lines.append("  %s ... %d more" % (status, len(chosen) - SAMPLE_CAP))
    return lines


def main(argv=None, stdin=None, arcpy_loader=None):
    args = _parse(sys.argv[1:] if argv is None else argv)
    stdin = sys.stdin if stdin is None else stdin

    # A project path can hold characters the console codepage cannot encode.
    # With a cp1252 stdout, which is what a scheduled task on Windows gets, one
    # such path ended a finished scan with a UnicodeEncodeError and lost the
    # report. Replace those characters instead.
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    if args.self_test:
        return self_test()

    if args.sde_worker:
        return _worker_main(args, stdin, arcpy_loader or _load_arcpy)

    try:
        host_needles(args.old_server)
    except ValueError as exc:
        print("error: %s. Give the host name being retired, for example "
              "OLDHOST-1." % exc, file=sys.stderr)
        return 64

    try:
        text_exts = text_extensions(args.text_ext)
    except ValueError as exc:
        print("error: %s. Pass --text-ext only for plain text types."
              % exc, file=sys.stderr)
        return 64

    host = args.old_server.strip()
    try:
        new, passwords = _remediation_args(args, stdin)
    except ValueError as exc:
        print("error: %s." % exc, file=sys.stderr)
        return 64

    rows = None
    try:
        roots = scan_roots(args.path)
        hits = [] if new else None
        walk = scan_tree(roots, host, max_seconds=args.max_seconds,
                         include_temp=args.scan_temp, text_exts=text_exts,
                         hits=hits)
        if new:
            rows = repoint_all(hits, host, new, args.apply, passwords,
                               args.pro_python, time.time(), WORKER_SCRIPT)
        if new and args.apply:
            # The verdict is the one for the tree as it stands now, so a file
            # that could not be repointed still says STALE, and a repointed one
            # is read again by the same matcher before it is called clean.
            walk = scan_tree(roots, host, max_seconds=args.max_seconds,
                             include_temp=args.scan_temp, text_exts=text_exts)
    except Exception as exc:
        # Everything is caught here on purpose. A traceback on stderr and a
        # non-zero exit that means nothing in particular is how an unverified
        # device gets read as a broken script and then ignored. Fail toward
        # remediation with a line the grid can show.
        print("%s STALEHOST %s" % (ERROR, scrub(str(exc), passwords.values())))
        return 1

    state, code = verdict(walk, args.fail_on_sde, args.fail_on_text)
    pro = "yes" if pro_installed() else "no"

    if args.json:
        report = walk.to_dict(state, code, host, pro, text_exts)
        if rows is not None:
            report["repoint"] = {"to": new, "apply": args.apply,
                                 "counts": repoint_counts(rows),
                                 "results": rows}
        print(json.dumps(report, indent=2))
        return code

    print(summary_line(state, host, walk, pro, text_exts))
    # Paths only, never contents. Fixable hits first: those are the actionable
    # ones, and a console capture is short. Text next, because a text rewrite
    # needs no ArcGIS licence, and the .sde worklist last.
    for path in walk.samples:
        print("  hit %s" % format_sample(path))
    for path in walk.text_samples:
        print("  txt %s" % format_sample(path))
    for path in walk.sde_samples:
        print("  sde %s" % format_sample(path))
    if rows is not None:
        for line in repoint_lines(new, args.apply, rows):
            print(line)
    return code


if __name__ == "__main__":  # pragma: no branch
    # shortcut: the false branch needs an import, and this file is only ever
    # measured by running it, so coverage cannot reach it.
    sys.exit(main())
