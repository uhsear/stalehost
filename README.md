# stalehost

Find every ArcGIS file on a workstation that still names the host you are retiring, and never call a
tree clean that you could not finish reading.

The database moves to a new name on Saturday. Three hundred workstations hold projects that still
name the old one, and nobody knows which. So you write a detection script, ship it through your
fleet tool, and let it tell you which devices need the fix. The script is one line:

```
Get-ChildItem -Path C:\ -Recurse -ErrorAction SilentlyContinue | Select-String OLDHOST-1
```

Every device reports healthy. The fleet console shows a clean estate and you sign off.

Two things were wrong. On Windows PowerShell 5.1 that recursive enumeration stops at the first
`UnauthorizedAccessException`, and one protected user profile is enough, so most devices returned
nothing after reading almost nothing. `SilentlyContinue` hid it. And an `.aprx` is a ZIP archive, so
even on the devices that finished, the host name was never in the bytes the search read.

Nobody found out until Monday, when the projects opened with red exclamation marks on every layer.

This tool answers the same question and keeps the answer honest. Every way the scan can fall short
has its own counter and its own verdict, and a tree that was only half read is never reported clean.

```
$ python stalehost.py --self-test
stalehost self-test: no arcpy, no network, no registry write
----------------------------------------------------------------------
PASS  a non-ASCII host name is refused rather than never matching
PASS  a host between separators is a hit
PASS  the neighbouring server OLDHOST-10 is not a hit  <-- pinned defect
PASS  a longer name ending in the host is not a hit  <-- pinned defect
PASS  a host followed by a port is a hit
PASS  a host followed by an instance is a hit
...
PASS  a utf-16-le connection string is a hit with no BOM
PASS  a utf-16-le host at an odd byte offset is still a hit  <-- pinned defect
PASS  the OLDHOST-10 decoy in utf-16-le is not a hit  <-- pinned defect
...
PASS  a .lyrx saved as utf-16-le is still a hit  <-- pinned defect
PASS  a zipped .lyrx is routed by its PK header, not its extension  <-- pinned defect
PASS  an .sde carrying the host as utf-16-le is a hit  <-- pinned defect
PASS  an .sde naming OLDHOST-10 in utf-16-le is not a hit  <-- pinned defect
PASS  the raw .aprx bytes do not contain the host name  <-- pinned defect
...
PASS  the walk scanned the six documents and skipped the pruned trees
PASS  the AppData Local Temp scratch is pruned by default  <-- pinned defect
PASS  an .aprx that will not open is counted unreadable, not clean  <-- pinned defect
PASS  an unread .sde is counted apart from an unread project  <-- pinned defect
PASS  an unread .sde alone stays CLEAN exit 0, so the loop ends  <-- pinned defect
PASS  a scan root that is not a directory is a root failure
PASS  and that device is UNVERIFIED exit 1, never CLEAN  <-- pinned defect
PASS  a subdirectory that will not enumerate is not a root failure  <-- pinned defect
PASS  a listing that fails part way through degrades, it does not escape  <-- pinned defect
PASS  one unreadable subdirectory does not flag the device, the rest is still read  <-- pinned defect
PASS  a junction is refused by its reparse attribute  <-- pinned defect
...
PASS  stale .sde alone is SDEONLY exit 0, so no remediation loop  <-- pinned defect
PASS  a pile of stale .sde does not mask one stale .aprx  <-- pinned defect
PASS  an unread project is UNVERIFIED exit 1, not CLEAN  <-- pinned defect
PASS  found work outranks both ways a scan can fall short
PASS  --fail-on-sde turns the .sde worklist into remediable work
...
PASS  the verdict word leads the line, because the grid truncates from the right  <-- pinned defect
PASS  it reports paths and counts, never file contents  <-- pinned defect
PASS  a mistyped scan root is an error exit 1, never a confident CLEAN  <-- pinned defect
PASS  --fail-on-sde defaults to OFF
PASS  --scan-temp defaults to OFF
PASS  --fail-on-text defaults to OFF
PASS  --text-ext defaults to the documented bucket
PASS  --fail-on-text is read
PASS  --text-ext is repeatable
PASS  --json defaults to OFF
PASS  --path defaults to every local fixed drive
PASS  --max-seconds defaults to the measured 10x headroom
PASS  --fail-on-sde is read
PASS  --scan-temp is read
PASS  --json is read
PASS  --path is read
PASS  --max-seconds is read
PASS  --self-test parses
PASS  a unique prefix of --fail-on-text is refused by the parser  <-- pinned defect
----------------------------------------------------------------------
177 assertions, 0 failed
```

## Requirements

Python 3.9 or newer. Standard library only: `zipfile`, `os`, `json`, `argparse`, `time`. No `arcpy`,
no ArcGIS licence, no install step, no network and no database connection. It runs on ArcGIS Pro's
Python, on a plain `python3`, and on a device with no Esri software at all.

`winreg` and `ctypes` are imported inside the two functions that need them, so the tool also runs on
Linux and macOS. There it reports no ArcGIS Pro and needs `--path`.

```
git clone https://github.com/uhsear/stalehost.git
```

## Quick start

```
python stalehost.py --self-test
python stalehost.py OLDHOST-1
```

## Usage

```
python stalehost.py OLDHOST-1
python stalehost.py OLDHOST-1 --path D:\GIS
python stalehost.py OLDHOST-1 --json
python stalehost.py OLDHOST-1 --max-seconds 120
python stalehost.py OLDHOST-1 --scan-temp
python stalehost.py OLDHOST-1 --fail-on-sde
```

| Flag | Default | What it does |
|---|---|---|
| `old_server` | none | The host name being retired. Required unless `--self-test`. |
| `--path` | every local fixed drive | Scan this root only. |
| `--max-seconds` | 600 | Stop the walk after this many seconds and report `INCOMPLETE`. |
| `--fail-on-sde` | off | Treat a stale `.sde` as remediable work, so it sets exit 1. |
| `--scan-temp` | off | Include `AppData\Local\Temp`, which Pro fills with scratch connections. |
| `--json` | off | Emit the result as JSON instead of the text line. |
| `--self-test` | off | Run the offline assertions and exit. |

A scan of a small site tree:

```
$ python stalehost.py OLDHOST-1 --path .
STALE STALEHOST old=OLDHOST-1 fix=3 (aprx=1 lyrx=2) sde=2 scanned=8 unreadable=1 elapsed=0s pro=yes unverified=1
  hit .\projects\Parcels.lyrx
  hit .\projects\Wide.lyrx
  hit .\projects\Zoning.aprx
  sde .\legacy.sde
  sde .\projects\prod.sde
```

The verdict word leads the line because a fleet console truncates the output column from the right.
Every row used to open with the same dozen identical characters, so the one word that mattered was
the one that got cut off.

`unverified=`, `rootfail=` and `timeout=` are printed only when they changed the verdict, so a clean
device stays on one short line. `pro=` is a triage hint: `pro=no` means this device could never
repoint an `.sde` even if its connections were operating-system authenticated.

`--json` carries the same verdict with the full counts, for a fleet tool that collects structured
output rather than a console line:

```
$ python stalehost.py OLDHOST-1 --path . --json
{
  "state": "STALE",
  "exitCode": 1,
  "oldServer": "OLDHOST-1",
  "fixable": 3,
  "aprx": 1,
  "lyrx": 2,
  "sde": 2,
  "scanned": 8,
  "unreadableFixable": 1,
  "unreadableSde": 0,
  "rootFailed": false,
  "timedOut": false,
  "elapsedSeconds": 0,
  "proInstalled": true,
  "samples": [
    ".\\projects\\Parcels.lyrx",
    ".\\projects\\Wide.lyrx",
    ".\\projects\\Zoning.aprx"
  ],
  "sdeSamples": [
    ".\\legacy.sde",
    ".\\projects\\prod.sde"
  ]
}
```

## What it checks

Three file types, because those are the three that hold a server name a remediation can rewrite.

- **`.aprx`** is a ZIP archive of CIM parts. Every entry is decompressed and searched. Searching the
  container's own bytes finds nothing, which is the whole reason this is not a text search. That is
  pinned by an assertion, and it holds for real projects. A 101 KB `.aprx` saved by ArcGIS Pro on
  this machine names `services.arcgisonline.com` in 2 of its 21 parts. The raw file does not contain
  that string anywhere, in ASCII or in UTF-16LE.
- **`.lyrx`** is CIM JSON on disk, and is sometimes a ZIP instead. It is routed by the `PK\x03\x04`
  local file header rather than by its extension, because a zipped `.lyrx` read as text matches
  nothing and reports the layer as already repointed.
- **`.sde`** is a length-prefixed binary property set. There is no text layer to decode. The host
  name is stored as UTF-16LE with no byte order mark, at whatever offset the property set landed on.
  Derived and legacy files can carry an ASCII copy instead.

Everything else, `.mxd` included, is out of scope and is not counted as scanned.

**The host name is matched as a whole token.** The character on either side must not be a letter, a
digit or a hyphen. So `OLDHOST-1` does not match inside `OLDHOST-10` or inside `MYOLDHOST-1`. The
end of the buffer is a boundary, so `SERVER=OLDHOST-1` matches, and so do `OLDHOST-1,1433` and
`OLDHOST-1\SQLEXPRESS`. This is not a detail. `OLDHOST-10` is usually a real server in the same rack
that is not moving, and a substring match reports every workstation that names it.

Here is that rule on two real ArcGIS Pro projects, searched for a host name one character short of
the one they contain:

```
$ python stalehost.py services.arcgisonline.com --path 7A7B_NT_6a0a5f
STALE STALEHOST old=services.arcgisonline.com fix=2 (aprx=2 lyrx=0) sde=0 scanned=2 unreadable=0 elapsed=0s pro=yes
  hit 7A7B_NT_6a0a5f\p30\CrimeIncidents.aprx
  hit 7A7B_NT_6a0a5f\p20\CrimeIncidents.aprx

$ python stalehost.py services.arcgisonline.co --path 7A7B_NT_6a0a5f
CLEAN STALEHOST old=services.arcgisonline.co fix=0 (aprx=0 lyrx=0) sde=0 scanned=2 unreadable=0 elapsed=0s pro=yes
```

The second run reads the same two projects and finds nothing, because the character after the name
is `m`.

## One lowered buffer, two needles

The first version of this check decoded the whole file as latin-1 and ran a boundary regex over the
text. It then decoded the same bytes twice more, as UTF-16LE at offset 0 and at offset 1, and ran
the regex again each time. That is correct. It also builds three strings the size of the file to
answer one question.

This version lower-cases the bytes once. `bytes.lower()` folds only ASCII A-Z and leaves every other
byte alone, so the single lowered buffer serves an ASCII needle and a UTF-16LE needle alike. The
search runs at every byte offset, not only even ones, so alignment does not matter. Measured here on
a 64 KB buffer, 300 runs each:

```
decode to text, three decodes, regex : 2.38 ms
one lowered buffer, two byte needles : 0.06 ms
```

Forty times is not the point on one file. It is the point on a connection cache with a thousand
`.sde` in it, which is what a workstation that has used ArcGIS Pro for two years actually holds.

## What it refuses

**It refuses to call an unread tree clean.** A directory that will not enumerate is skipped and
counted, not ignored. A root that will not enumerate makes the whole device `UNVERIFIED`, because
the answer for that device is unknown. A subdirectory that will not enumerate does not, because
access denied on a protected profile is routine on Windows and would otherwise flag every device in
the fleet forever. A project that will not open is `UNVERIFIED` too: a scanner that returns "no
hits" for a file it could not read gives exactly the answer that is certainly wrong.

A full walk of both local fixed drives on the workstation this was written on took 45 seconds, read
152 documents and found 5 it could not open. Those five are why the counter exists.

**It refuses to loop.** The three failure states below fail toward remediation on purpose. An
idempotent remediation changes nothing on a clean device, while a missed device means broken
connections for a user. The cost is that a device which can never finish reports an
issue every cycle. That is a visible signal in the grid rather than a silent miss, and the counter
that caused it is printed.

**A stale `.sde` is inventory, not a verdict.** It is counted, sampled and reported, and by default
it does not set exit 1:

```
$ python stalehost.py OLDHOST-1 --path .
SDEONLY STALEHOST old=OLDHOST-1 fix=0 (aprx=0 lyrx=0) sde=1 scanned=1 unreadable=0 elapsed=0s pro=yes
  sde .\prod.sde
```

That run exits 0.

A database-authenticated `.sde` stores a password, so repointing one needs a person to re-enter it.
On the dry run that produced this tool, all 1157 `.sde` files found on one workstation were
database-authenticated and 0 were repointed automatically. Without ArcGIS Pro on the device, arcpy
cannot rewrite one at all. Letting `.sde` set exit 1 makes the fleet tool re-run a remediation
forever against a device it can never clean. The `sde=` count is the manual worklist instead.

`--fail-on-sde` turns that off, for a site whose remediation really can fix them:

```
$ python stalehost.py OLDHOST-1 --path . --fail-on-sde
STALE STALEHOST old=OLDHOST-1 fix=0 (aprx=0 lyrx=0) sde=1 scanned=1 unreadable=0 elapsed=0s pro=yes
  sde .\prod.sde
```

The same device, the same files, exit 1.

**It refuses to print file contents.** The output is paths and counts. A connection string holds a
user name and sometimes a stored credential, and this output goes to a fleet console that a wide
group can read. Sample paths are truncated from the left, keeping the file name, because the
meaningful part of a GIS project path is its end.

**It never writes anything.** No file is opened for writing, no registry value is set, no database
is contacted and no network call is made. There is no `--apply`, because there is nothing to apply.

## Exit codes

The polarity is the one a detect-and-remediate loop expects, which is the opposite of a Windows
application detection rule:

| Code | Meaning |
|---|---|
| 1 | Issue found, or the scan could not be trusted. Run the remediation. |
| 0 | Nothing for the remediation to do. |
| 64 | Usage error. No host name was given, or it was not ASCII. |

| State | Code | When |
|---|---|---|
| `STALE` | 1 | A stale `.aprx` or `.lyrx` was found, or `--fail-on-sde` and a stale `.sde`. |
| `INCOMPLETE` | 1 | The walk hit `--max-seconds` before it finished. |
| `UNVERIFIED` | 1 | A scan root would not enumerate, or a project would not open. |
| `ERROR` | 1 | The tool itself failed. The reason is on the line. |
| `SDEONLY` | 0 | Stale `.sde` only. Nothing automatic can fix them. |
| `CLEAN` | 0 | Nothing stale, and the whole tree was read. |

64 is reserved for an argument mistake, which happens while the package is being built and never on
a deployed run, where the arguments are fixed. Every runtime failure is 1.

## Limits

- It reports what a file says, not what a database holds. A project on the new alias may still point
  at a table that was dropped last week.
- It does not fix anything. Repointing an `.aprx` means rewriting its CIM connection strings, which
  is `arcpy.mp` and a licensed seat, or a text rewrite of the parts inside the archive.
- Only local fixed drives are scanned when `--path` is absent. A mapped drive is another machine's
  tree, and a scan that follows one blocks for the full network timeout when the share is gone.
- `AppData\Local\Temp` is pruned by default. ArcGIS Pro drops GUID-named scratch `.sde` connections
  there and never cleans them up: 2533 of them on the workstation this was measured on. They are
  regenerated on the next launch. A device whose only stale content is scratch reads `CLEAN`. Pass
  `--scan-temp` if you disagree.
- `Windows`, `Program Files`, `Program Files (x86)`, `ProgramData`, `$Recycle.Bin`,
  `System Volume Information`, `node_modules` and `.git` are skipped at any depth.
- Junctions and symbolic links are not followed, so the walk cannot cycle. A project reachable only
  through a junction is not seen.
- A file over 64 MB is refused rather than read into memory, and counted unreadable. Real `.aprx`
  and `.sde` files are far smaller, and this runs as a privileged account over every local drive.
- Run it in a 64-bit Python. The ArcGIS Pro registry check reads a 64-bit key, and a 32-bit host
  process is redirected to `WOW6432Node`, where no Esri key exists. It asks for the 64-bit view
  explicitly, but only Python's own architecture can be relied on for the rest.
- `pro=` is reported, never a verdict input. Pro's absence does not change what is stale.
- The elapsed time is whole seconds, and the cap is checked between directories. A single enormous
  directory can overshoot it.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [cimscan](https://github.com/uhsear/cimscan) - what the layers inside those same projects point at, in full, with the credentials redacted
- [agol-relink](https://github.com/uhsear/agol-relink) - the same retired host hunted across Portal and ArcGIS Online content instead of files on disk
