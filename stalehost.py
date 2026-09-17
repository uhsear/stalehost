#!/usr/bin/env python
r"""Find every ArcGIS file on a workstation that still names the host you are retiring, and never call a tree clean that you could not finish reading.

Point it at a machine and it walks the local fixed drives for .aprx, .lyrx and
.sde files, decides whether each one still names the old host as a whole token,
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
tree, so a permission-denied profile looks exactly like a clean one.

    python stalehost.py --self-test
    python stalehost.py OLDHOST-1
    python stalehost.py OLDHOST-1 --path D:\GIS
    python stalehost.py OLDHOST-1 --json
    python stalehost.py OLDHOST-1 --fail-on-sde

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
      3. a scan root could not be enumerated, or a .aprx or
         .lyrx could not be read, so the device is unproven   (UNVERIFIED)
      4. the tool itself threw                                (ERROR)

    exit 0 -- nothing for the remediation to do:
      5. no stale content at all                              (CLEAN)
      6. stale .sde only. See WHY .sde IS INVENTORY below     (SDEONLY)

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
database password, so repointing one needs a human to re-enter it: on the dry
run that produced this tool, all 1157 .sde files found were DBMS-auth and 0 were
repointed. Without ArcGIS Pro on the device, arcpy cannot rewrite one at all.
Letting .sde set exit 1 makes the loop re-run a remediation forever on a device
it can never clean. The sde= count is the manual worklist instead. Pass
--fail-on-sde only when your remediation really can fix them.
"""

from __future__ import print_function

import argparse
import json
import os
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

SCANNED_EXTENSIONS = FIXABLE_EXTENSIONS + INVENTORY_EXTENSIONS

# Sample paths printed. Fixable hits are sampled separately from .sde so that a
# connection cache full of .sde cannot crowd the actionable .aprx and .lyrx
# paths out of the report. A fleet console usually captures about 2048
# characters of output; 5 + 3 lines of ~115 characters leaves room to spare.
SAMPLE_CAP = 5
SDE_SAMPLE_CAP = 3

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
# AppData\Local\Temp and never cleans them up: 2533 of them on the workstation
# this was measured on, the single biggest cost in a full walk. They are
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

# Verdict states. The word leads the summary line because a fleet grid truncates
# the output column from the right, and every row otherwise opened with the same
# dozen identical characters, so the one word that mattered was the one cut off.
STALE = "STALE"
INCOMPLETE = "INCOMPLETE"
UNVERIFIED = "UNVERIFIED"
SDEONLY = "SDEONLY"
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


def find_token(haystack, needle, wide):
    """True when needle appears in haystack as a whole token.

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
        return False
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return False
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
        return True


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


def verdict(walk, sde_is_work=False):
    """Return (state, exit code) for a finished walk. The whole decision.

    Kept in one function with no I/O so the exit code a fleet tool acts on can
    be asserted directly rather than inferred from a scan.
    """
    fixable = walk.aprx + walk.lyrx
    if fixable or (sde_is_work and walk.sde):
        return STALE, 1
    if walk.timed_out:
        return INCOMPLETE, 1
    if walk.root_failed:
        return UNVERIFIED, 1
    if walk.unreadable_fix:
        return UNVERIFIED, 1
    if sde_is_work and walk.unreadable_sde:
        return UNVERIFIED, 1
    if walk.sde:
        return SDEONLY, 0
    return CLEAN, 0


def format_sample(path, width=SAMPLE_WIDTH):
    """Truncate a sample path from the left, keeping the meaningful end."""
    if len(path) <= width:
        return path
    return "..." + path[-(width - 3):]


def summary_line(state, host, walk, pro):
    """The one line a fleet console captures. Paths and counts only."""
    line = ("%s STALEHOST old=%s fix=%d (aprx=%d lyrx=%d) sde=%d scanned=%d "
            "unreadable=%d elapsed=%ds pro=%s"
            % (state, host, walk.aprx + walk.lyrx, walk.aprx, walk.lyrx,
               walk.sde, walk.scanned,
               walk.unreadable_fix + walk.unreadable_sde, walk.elapsed, pro))
    # Printed only when they changed the verdict, so the common case stays short.
    if walk.unreadable_fix:
        line += " unverified=%d" % walk.unreadable_fix
    if walk.root_failed:
        line += " rootfail=yes"
    if walk.timed_out:
        line += " timeout=yes"
    return line


class Walk(object):
    """Counts from one scan. Built with keywords so a test can state one case."""

    def __init__(self, aprx=0, lyrx=0, sde=0, unreadable_fix=0,
                 unreadable_sde=0, root_failed=False, timed_out=False,
                 scanned=0, elapsed=0, samples=None, sde_samples=None):
        self.aprx = aprx
        self.lyrx = lyrx
        self.sde = sde
        self.unreadable_fix = unreadable_fix    # unread .aprx or .lyrx
        self.unreadable_sde = unreadable_sde    # unread .sde
        self.root_failed = root_failed
        self.timed_out = timed_out
        self.scanned = scanned
        self.elapsed = elapsed
        self.samples = [] if samples is None else samples
        self.sde_samples = [] if sde_samples is None else sde_samples

    def to_dict(self, state, code, host, pro):
        return {
            "state": state,
            "exitCode": code,
            "oldServer": host,
            "fixable": self.aprx + self.lyrx,
            "aprx": self.aprx,
            "lyrx": self.lyrx,
            "sde": self.sde,
            "scanned": self.scanned,
            "unreadableFixable": self.unreadable_fix,
            "unreadableSde": self.unreadable_sde,
            "rootFailed": self.root_failed,
            "timedOut": self.timed_out,
            "elapsedSeconds": self.elapsed,
            "proInstalled": pro == "yes",
            "samples": list(self.samples),
            "sdeSamples": list(self.sde_samples),
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


def file_contains_host(path, extension, host):
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
              matcher=None, lister=None, clock=time.time):
    """Walk the roots and count what names the host. Never raises for one file.

    An explicit stack, not os.walk and not a recursive helper. The equivalent
    one-liner on the shell this was deployed from aborts the entire enumeration
    on the first access-denied directory and returns nothing, which a fleet tool
    reads as a healthy device. A per-directory try/except degrades instead of
    lying: the directory is skipped, the counter that says so is raised, and the
    verdict reflects it.
    """
    if matcher is None:
        matcher = file_contains_host
    if lister is None:
        # Replaced only by the self-test, which cannot create an access-denied
        # directory or a junction on every host it has to be green on. clock is
        # a seam for the same reason: the deadline boundary is only assertable
        # against a clock that does not move between two reads of it.
        lister = os.scandir
    walk = Walk()
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
            if extension not in SCANNED_EXTENSIONS:
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
                else:
                    walk.unreadable_fix += 1
                continue
            if not hit:
                continue
            if extension == ".sde":
                walk.sde += 1
                if len(walk.sde_samples) < SDE_SAMPLE_CAP:
                    walk.sde_samples.append(entry.path)
            else:
                if extension == ".aprx":
                    walk.aprx += 1
                else:
                    walk.lyrx += 1
                if len(walk.samples) < SAMPLE_CAP:
                    walk.samples.append(entry.path)

        for entry in subdirectories:
            if skip_directory(entry.name, entry.path, include_temp):
                continue
            if is_reparse(entry):
                continue
            stack.append(entry.path)

    walk.elapsed = int(clock() - started)
    return walk


def _pro_in_registry(subkey=PRO_REGISTRY_KEY):
    """True when ArcGIS Pro is registered, whatever path it was installed to."""
    try:
        import winreg  # imported here so the tool runs on a non-Windows host
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


def _windows_drive_type(root):
    """GetDriveTypeW for a drive root. 0, meaning unknown, off Windows."""
    try:
        import ctypes  # imported here so the tool runs on a non-Windows host
        return int(ctypes.windll.kernel32.GetDriveTypeW(root))
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


def self_test():
    """Assertions over the matchers, the walk and the verdict. No network."""
    import io
    import shutil
    import tempfile

    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def run(argv):
        """main() with both streams captured, returning (exit code, output)."""
        buffer = io.StringIO()
        saved = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = buffer, buffer
        try:
            code = main(argv)
        finally:
            sys.stdout, sys.stderr = saved
        return code, buffer.getvalue()

    host = "OLDHOST-1"
    decoy = "OLDHOST-10"

    print("stalehost self-test: no arcpy, no network, no registry write")
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
                         stat_raises=False):
                self.name = name
                self.path = path
                self._symlink = symlink
                self._attributes = attributes
                self._stat_raises = stat_raises

            def is_dir(self, follow_symlinks=True):
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

        code, out = run([host, "--path", os.path.join(workdir, "nope")])
        check(code == 1 and out.startswith("ERROR "),
              "a mistyped scan root is an error exit 1, never a confident CLEAN  <-- pinned defect")
        code, out = run([])
        check(code == 64, "no host name is a usage error, exit 64")
        code, out = run(["   "])
        check(code == 64, "a blank host name is a usage error, exit 64")

        # ---- argument handling
        args = _parse([host])
        check(args.old_server == host, "the old host name is read")
        check(args.fail_on_sde is False, "--fail-on-sde defaults to OFF")
        check(args.scan_temp is False, "--scan-temp defaults to OFF")
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
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("-" * 70)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for label in failed:
            print("  FAILED: %s" % label)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _parse(argv):
    parser = argparse.ArgumentParser(
        prog="stalehost.py",
        description="Find every ArcGIS file on this device that still names the "
                    "host you are retiring, and never call a tree clean that "
                    "could not be finished.",
        epilog="stalehost never writes a file, never opens a database "
               "connection and never reaches the network. Exit 1 means stale "
               "content, or a scan that could not be trusted.",
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
    parser.add_argument("--scan-temp", dest="scan_temp", action="store_true",
                        help="include AppData\\Local\\Temp, where Pro leaves "
                             "thousands of scratch .sde connections")
    parser.add_argument("--json", action="store_true",
                        help="emit the result as JSON instead of the text line")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run the offline assertions and exit")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

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

    try:
        host_needles(args.old_server)
    except ValueError as exc:
        print("error: %s. Give the host name being retired, for example "
              "OLDHOST-1." % exc, file=sys.stderr)
        return 64

    host = args.old_server.strip()
    try:
        roots = scan_roots(args.path)
        walk = scan_tree(roots, host, max_seconds=args.max_seconds,
                         include_temp=args.scan_temp)
    except Exception as exc:
        # Everything is caught here on purpose. A traceback on stderr and a
        # non-zero exit that means nothing in particular is how an unverified
        # device gets read as a broken script and then ignored. Fail toward
        # remediation with a line the grid can show.
        print("%s STALEHOST %s" % (ERROR, exc))
        return 1

    state, code = verdict(walk, args.fail_on_sde)
    pro = "yes" if pro_installed() else "no"

    if args.json:
        print(json.dumps(walk.to_dict(state, code, host, pro), indent=2))
        return code

    print(summary_line(state, host, walk, pro))
    # Paths only, never contents. Fixable hits first: those are the actionable
    # ones, and a console capture is short.
    for path in walk.samples:
        print("  hit %s" % format_sample(path))
    for path in walk.sde_samples:
        print("  sde %s" % format_sample(path))
    return code


if __name__ == "__main__":
    sys.exit(main())
