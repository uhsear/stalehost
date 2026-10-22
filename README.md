# stalehost

Find every ArcGIS file on a workstation that still names the host you are retiring, never call a tree
clean that you could not finish reading, and, when you ask, repoint what it found without touching
the neighbouring server.

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

The fix was no better. A repoint script walked the share, found each connection with a substring
test, and rewrote it. `OLDHOST-1` is a substring of `OLDHOST-10`, a server in the same rack that was
not moving, so every connection to that server was repointed too. The script also wrote its backup to
`<file>.bak`. The operator ran it a second time to catch the stragglers, and the second run copied
the already damaged file over the only good copy. Nothing in the output said either thing had
happened.

This tool answers the detection question and keeps the answer honest. Every way the scan can fall
short has its own counter and its own verdict, and a tree that was only half read is never reported
clean. With `--to` it prints the plan. With `--to` and `--apply` it repoints the files, each one
after a byte-checked backup, and then reads the tree again before it says `CLEAN`.

## The existing alternative

Esri's supported route is `arcpy.mp`. `ArcGISProject.updateConnectionProperties(current_connection_info,
new_connection_info, {auto_update_joins_and_relates}, {validate}, {ignore_case})` replaces connection
properties inside a project, and with `validate` set it only updates to a connection that is valid. It
works through Pro's own object model. Use it when the device has Pro and you can open each project.
It is a method on one project. Finding the projects, backing them up and judging the whole tree stay
with you.

`stalehost` is for the case in front of that one: finding the files across a fleet, and rewriting the
simple ones with no licence at all. For an `.aprx` or `.lyrx` it rewrites the host name inside the
file. Esri's documentation does not say how an `.aprx` is stored, so that part rests on what the files
are. See [Limits](#limits). For an `.sde` it does not edit bytes. It asks ArcGIS Pro's own Python to
rebuild the connection.

A recursive text search is the other alternative. It reads the bytes of the container, and for an
`.aprx` those bytes do not contain the host name. A self-test case pins that.

```
$ python stalehost.py --self-test
@@SELFTEST@@
```

## Requirements

Python 3.9 or newer. Standard library only: `zipfile`, `os`, `json`, `argparse`, `subprocess`,
`filecmp`, `shutil`, `time`. No `arcpy` in this process, no install step and no network. It runs on
ArcGIS Pro's Python, on a plain `python3`, and on a device with no Esri software at all.

Rebuilding an `.sde` is the one step that needs ArcGIS Pro. The tool starts Pro's `python.exe` as a
child process for it. Everything else, the scan and the `.aprx` and `.lyrx` rewrite, does not.

`winreg` and `ctypes` are imported inside the two functions that need them, so the tool also runs on
Linux and macOS. There it reports no ArcGIS Pro and needs `--path`.

```
git clone https://github.com/uhsear/stalehost.git
```

## Quick start

```
python stalehost.py --self-test
python stalehost.py OLDHOST-1
python stalehost.py OLDHOST-1 --to NEWHOST
python stalehost.py OLDHOST-1 --to NEWHOST --apply
```

The second line scans. The third prints what would change and changes nothing. The fourth changes it.

## Usage

```
python stalehost.py OLDHOST-1
python stalehost.py OLDHOST-1 --path D:\GIS
python stalehost.py OLDHOST-1 --json
python stalehost.py OLDHOST-1 --max-seconds 120
python stalehost.py OLDHOST-1 --scan-temp
python stalehost.py OLDHOST-1 --fail-on-sde
python stalehost.py OLDHOST-1 --text-ext .txt,.config --fail-on-text
python stalehost.py OLDHOST-1 --to NEWHOST --path D:\GIS
python stalehost.py OLDHOST-1 --to NEWHOST --path D:\GIS --apply --passwords-stdin < logins.json
```

| Flag | Default | What it does |
|---|---|---|
| `old_server` | none | The host name being retired. Required unless `--self-test`. |
| `--path` | every local fixed drive | Scan this root only. |
| `--max-seconds` | 600 | Stop the walk after this many seconds and report `INCOMPLETE`. |
| `--fail-on-sde` | off | Treat a stale `.sde` as remediable work, so it sets exit 1. |
| `--fail-on-text` | off | Treat a stale text file as remediable work, so it sets exit 1. |
| `--text-ext` | `py,bat,cmd,ps1,sql,ini` | Add extensions to the text bucket. Repeatable or comma separated. |
| `--scan-temp` | off | Include `AppData\Local\Temp`, which Pro fills with scratch connections. |
| `--json` | off | Emit the result as JSON instead of the text line. |
| `--to` | none | The host that replaces the old one. On its own it prints the plan and writes nothing. |
| `--apply` | off | With `--to`, rewrite the files. This is the only flag that writes. |
| `--passwords-stdin` | off | With `--apply`, read a JSON object of database login to password from stdin. |
| `--pro-python` | the default Pro install path | The `python.exe` of ArcGIS Pro, used to rebuild `.sde` connections. |
| `--self-test` | off | Run the offline assertions and exit. |

The parser has `allow_abbrev` off. `--ap` is not `--apply`, `--passwords` is not `--passwords-stdin`,
and a typo that is a prefix of a flag is an error and not a different command. Each of those is
pinned in the self-test.

A scan of a small site tree:

```
$ python stalehost.py OLDHOST-1 --path .
STALE STALEHOST old=OLDHOST-1 fix=3 (aprx=1 lyrx=2) sde=2 text=0 scanned=8 unreadable=1 elapsed=0s pro=yes unverified=1 textext=py,bat,cmd,ps1,sql,ini
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
rebuild an `.sde`. `textext=` is always last and says which text extensions this run read, so a
`CLEAN` states what it covered.

`--json` carries the same verdict with the full counts, for a fleet tool that collects structured
output rather than a console line. With `--to` it adds a `repoint` object with the counts and one row
per file.

## What it checks

Four kinds of file, because those are the kinds that hold a server name a remediation can act on.

- **`.aprx`** is a ZIP archive of CIM parts. Every entry is decompressed and searched. Searching the
  container's own bytes finds nothing, which is the whole reason this is not a text search. That is
  pinned by an assertion, and it holds for real projects, as the next example shows.
- **`.lyrx`** is CIM JSON on disk, and is sometimes a ZIP instead. It is routed by the `PK\x03\x04`
  local file header rather than by its extension, because a zipped `.lyrx` read as text matches
  nothing and reports the layer as already repointed.
- **`.sde`** is a length-prefixed binary property set. There is no text layer to decode. The host
  name is stored as UTF-16LE with no byte order mark, at whatever offset the property set landed on.
  Derived and legacy files can carry an ASCII copy instead.
- **Text**: `.py`, `.bat`, `.cmd`, `.ps1`, `.sql` and `.ini`, read as bytes so a UTF-16 script from
  the PowerShell ISE is found too. `--text-ext` widens the list. Text is reported, and it is never
  rewritten.

Everything else, `.mxd` included, is out of scope and is not counted as scanned.

The raw bytes of a project saved by ArcGIS Pro 3.6 hold the host name in neither ASCII nor UTF-16LE,
and the scan still finds it. The second run searches for `OLDHOST`, which is `OLDHOST-1` without its
last two characters. A hyphen continues a token, so the name in the project is not a match:

```
$ python stalehost.py OLDHOST-1 --path .
STALE STALEHOST old=OLDHOST-1 fix=1 (aprx=1 lyrx=0) sde=0 text=0 scanned=1 unreadable=0 elapsed=0s pro=yes textext=py,bat,cmd,ps1,sql,ini
  hit .\Project.aprx

$ python stalehost.py OLDHOST --path .
CLEAN STALEHOST old=OLDHOST fix=0 (aprx=0 lyrx=0) sde=0 text=0 scanned=1 unreadable=0 elapsed=0s pro=yes textext=py,bat,cmd,ps1,sql,ini
```

**The host name is matched as a whole token.** The character on either side must not be a letter, a
digit or a hyphen. So `OLDHOST-1` does not match inside `OLDHOST-10` or inside `MYOLDHOST-1`. The
end of the buffer is a boundary, so `SERVER=OLDHOST-1` matches, and so do `OLDHOST-1,1433` and
`OLDHOST-1\SQLEXPRESS`. This is not a detail. `OLDHOST-10` is usually a real server in the same rack
that is not moving, and a substring match reports every workstation that names it. The rewrite uses
the same rule as the scan, from one shared function, so the file it writes is judged by the check
that found the problem.

## Repointing

`--to NEWHOST` prints the plan: one `would` row per file, and no write of any kind. `--apply` does the
work. The replacement host must be a plain host name. A backslash, a quote or a comma would corrupt
the JSON or the property set it is written into, so those are refused. A name that still contains the
old host as a token is refused too. `OLDHOST-1.example.org` would be found again on the next run and
grown to `OLDHOST-1.example.org.example.org`.

**An `.aprx` or `.lyrx`** is rewritten in process, in this order:

1. The new file is built beside the old one, as `<name>.stalehost-tmp`. A ZIP keeps its part names,
   their order, time stamps, compression, attributes and comment. Only the parts that named the host
   change. Every part is read through `zipfile`, which checks its CRC, so a damaged archive stops the
   rewrite.
2. The new file is read back with the scan's own matcher. It must name the new host and must not
   name the old one.
3. The original is compared with the size and time stamp taken before step 1. A project someone
   saved in the meantime is refused, not overwritten.
4. The original is copied to `<name>.stalehost-bak-<UTC time stamp>` and compared byte for byte. If
   that name exists, `.1`, `.2` and so on are tried. A backup is never overwritten and never deleted.
5. `os.replace` puts the new file in place. Python documents it as atomic when it succeeds, and as
   able to fail across filesystems, which is why the new file is built in the same folder. If the
   rename fails, because another process holds the file open, the backup is removed again and the
   original is exactly as it was.

If any step fails, that file is reported `failed` and left as it was, and the run goes on to the next
file.

**An `.sde`** is a binary property set and is never edited. The tool starts the Python that ships with
ArcGIS Pro (`--pro-python`) as a child process and runs this same file there in a hidden worker mode,
which refuses to start without `--apply`. For each connection the worker reads the properties with
`arcpy.Describe(...).connectionProperties`, decides, and calls
`arcpy.management.CreateDatabaseConnection` with the new host. It builds the new file beside the old
one. The parent then checks that the new file names the new host and not the old one, and swaps it in
with the same backup steps as above. A connection is **declined**, and left alone, when it is not SQL
Server, when it is a branch or historical connection, when its instance does not name the old host as
a token, or when its authentication mode is unknown. A neighbouring server is declined by the same
rule as in the scan.

**Passwords.** An operating-system-authenticated connection needs none. A database login is rebuilt
only if you supply its password, as one JSON object on stdin:

```
{"first_login": "password one", "second_login": "password two"}
```

```
$ type logins.json | python stalehost.py OLDHOST-1 --to NEWHOST --apply --passwords-stdin
```

There is no flag that takes a password, and the environment is never read for one. Setting a
`..._PW` variable changes nothing, and a self-test case pins that. The password reaches the child
process on its stdin and nowhere else, so it is not in any process listing. A terminal on stdin is
refused and the tool does not prompt. Logins are matched without regard to case. Every message that
comes back from the child, and every error line the tool prints, has each password replaced with
`***` first. A login that has no password in the object is reported `needs-password`, and its name
is not printed.

**The exit code is read after the work.** After `--apply` the tree is scanned again and the exit code
is the verdict for the tree as it now stands. A project that could not be rewritten still says
`STALE`, exit 1. A repointed project is read again by the matcher before the run says `CLEAN`.

A real run on a small tree, on a Windows machine with ArcGIS Pro 3.6. The `.aprx` is a project saved
by Pro itself:

```
@@RUN@@
```

The second `--apply` fixed nothing and took no new backup. `Neighbour.lyrx` names `OLDHOST-10`, and
no row mentions it.

The statuses on a row:

| Status | Meaning |
|---|---|
| `would` | The plan. Nothing was written. |
| `fixed` | Rewritten, with a backup beside it. |
| `failed` | Left as it was. The reason is on the row. |
| `declined` | An `.sde` that is not ours to repoint. Left as it was. |
| `needs-password` | A database login with no password supplied. Left as it was. |
| `left` | An `.sde`, and `--pro-python` does not exist. Left as it was. |

Each status shows five rows and then `... N more`. `--json` has them all.

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

A full walk of both local fixed drives on the workstation this was written on took under a minute,
read about a hundred and fifty documents and found a handful it could not open. Those are why the
counter exists.

**It refuses to loop.** The three failure states below fail toward remediation on purpose. An
idempotent remediation changes nothing on a clean device, while a missed device means broken
connections for a user. The cost is that a device which can never finish reports an
issue every cycle. That is a visible signal in the grid rather than a silent miss, and the counter
that caused it is printed.

**A stale `.sde` is inventory, not a verdict.** It is counted, sampled and reported, and by default
it does not set exit 1:

```
$ python stalehost.py OLDHOST-1 --path .
SDEONLY STALEHOST old=OLDHOST-1 fix=0 (aprx=0 lyrx=0) sde=1 text=0 scanned=1 unreadable=0 elapsed=0s pro=yes textext=py,bat,cmd,ps1,sql,ini
  sde .\prod.sde
```

That run exits 0.

A database-authenticated `.sde` stores a password, so repointing one needs a person to supply it. On
the dry run that produced this tool, every one of the thousand-odd `.sde` files found on one
workstation was database-authenticated and none was repointed automatically. Without ArcGIS Pro on
the device, arcpy cannot rewrite one at all. Letting `.sde` set exit 1 makes the fleet tool re-run a
remediation forever against a device it can never clean. The `sde=` count is the manual worklist
instead.

`--fail-on-sde` turns that off, for a site whose remediation really can fix them:

```
$ python stalehost.py OLDHOST-1 --path . --fail-on-sde
STALE STALEHOST old=OLDHOST-1 fix=0 (aprx=0 lyrx=0) sde=1 text=0 scanned=1 unreadable=0 elapsed=0s pro=yes textext=py,bat,cmd,ps1,sql,ini
  sde .\prod.sde
```

The same device, the same files, exit 1.

**It refuses to print file contents.** The output is paths and counts. A connection string holds a
user name and sometimes a stored credential, and this output goes to a fleet console that a wide
group can read. Sample paths are truncated from the left, keeping the file name, because the
meaningful part of a GIS project path is its end. A database login name is not printed either.

**Nothing writes without `--apply`.** Without `--to` and `--apply` no file is opened for writing, no
registry value is set, no child process starts, no database is contacted and no network call is made.
The self-test takes a snapshot of every byte of a tree, runs the plan, and compares.

## Exit codes

The polarity is the one a detect-and-remediate loop expects, which is the opposite of a Windows
application detection rule:

| Code | Meaning |
|---|---|
| 1 | Issue found, or the scan could not be trusted. Run the remediation. |
| 0 | Nothing for the remediation to do. |
| 64 | Usage error. No host name, a host that is not ASCII, an unusable `--to`, or `--apply` or `--passwords-stdin` without what they need. |

| State | Code | When |
|---|---|---|
| `STALE` | 1 | A stale `.aprx` or `.lyrx` was found, or `--fail-on-sde` and a stale `.sde`, or `--fail-on-text` and a stale text file. |
| `INCOMPLETE` | 1 | The walk hit `--max-seconds` before it finished. |
| `UNVERIFIED` | 1 | A scan root would not enumerate, or a project or script would not open. |
| `ERROR` | 1 | The tool itself failed. The reason is on the line. |
| `TEXTONLY` | 0 | Stale text only. Nothing here rewrites text. |
| `SDEONLY` | 0 | Stale `.sde` only. |
| `CLEAN` | 0 | Nothing stale, and the whole tree was read. |

64 is reserved for an argument mistake, which happens while the package is being built and never on
a deployed run, where the arguments are fixed. Every runtime failure is 1.

## Sources

- [`CreateDatabaseConnection`](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/create-database-connection.html).
  The parameter names and keywords used: `out_folder_path`, `out_name`, `database_platform`
  (`SQL_SERVER`), `instance`, `account_authentication` (`DATABASE_AUTH`, `OPERATING_SYSTEM_AUTH`),
  `username`, `password`, `save_user_pass` (`SAVE_USERNAME`), `database`, `version_type`
  (`TRANSACTIONAL`), `version`. The page says the output file has the `.sde` extension. It does not
  say whether an existing output is overwritten, so the tool never leaves one to be overwritten.
- [Workspace properties](https://doc.esri.com/en/arcgis-pro/latest/arcpy/functions/workspace-properties.html).
  `connectionProperties` and its `dbClient`, `instance`, `server`, `database`, `authentication_mode`
  (`OSA` or `DBMS`), `user`, `version`, `branch`, `historical_name` and `historical_timestamp`.
- [`ArcGISProject`](https://doc.esri.com/en/arcgis-pro/latest/arcpy/mapping/arcgisproject-class.html).
  `updateConnectionProperties` and its `validate` argument. The page does not say what an `.aprx` is
  on disk.
- [`os.replace`](https://docs.python.org/3/library/os.html#os.replace). Replaces an existing file, can
  fail across filesystems, and is atomic when it succeeds.

## Limits

- It reports what a file says, not what a database holds. A project on the new alias may still point
  at a table that was dropped last week.
- **An `.aprx` rewrite rests on an undocumented format.** Esri's `ArcGISProject` page does not say that
  an `.aprx` is a ZIP of JSON parts. The scan has relied on it from the start, and every project
  tried was one. The rewrite was checked once against a real project saved by ArcGIS Pro 3.6: the
  tool changed it, and `arcpy.mp.ArcGISProject` opened the result and read the layer's database as the
  new path. A future Pro that changes the layout makes the matcher find nothing, or the read-back
  refuse the file. It does not make the tool write a broken project. If you want Esri's route, use
  `updateConnectionProperties`.
- **Close the project first.** Pro holds a project it has open. A rename onto it fails, and the file
  is reported `failed`. A project that was open and then saved by Pro after the rewrite is saved over
  it, and the old host comes back. Run this when nobody has the project open.
- **An `.sde` rebuild needs the old host to answer.** On ArcGIS Pro 3.6, `arcpy.Describe` of a
  connection file whose server does not answer returns an object with no `connectionProperties`. The
  tool reports that file `failed` with that reason and changes nothing. Repoint before you switch the
  old host off, or after, from an alias that still resolves.
- **A successful `.sde` rebuild was never run against a live SQL Server.** There is none on the
  machine this was built on. What was run: the exact `CreateDatabaseConnection` arguments the tool
  builds, for a database login with a version and for operating-system authentication, were passed to
  real ArcGIS Pro 3.6. It accepted them and wrote a file that names the new host, does not name the
  old one, and does not hold the password in ASCII or UTF-16. The parent and child protocol, the swap,
  the backups and every failure were run against a stand-in `arcpy`. The real Pro Python was also run
  as the child against real connection files with an unreachable server, and returned the `failed`
  row above. Whether `Describe` returns the `instance` text your connections need was not seen.
- Text files are reported and never rewritten. A host name in a script is often a decision and not a
  connection.
- A branch or historical connection, a connection that is not SQL Server, and a connection whose
  instance does not name the old host are declined, not guessed at.
- Only local fixed drives are scanned when `--path` is absent. A mapped drive is another machine's
  tree, and a scan that follows one blocks for the full network timeout when the share is gone.
- `AppData\Local\Temp` is pruned by default. ArcGIS Pro drops GUID-named scratch `.sde` connections
  there and never cleans them up: thousands of them on the workstation this was measured on. They are
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
  explicitly, but only Python's own architecture can be relied on for the rest. Pro is found by the
  default path or the registry for the `pro=` hint only. Rebuilding uses `--pro-python`.
- `pro=` is reported, never a verdict input. Pro's absence does not change what is stale.
- The elapsed time is whole seconds, and the cap is checked between directories. A single enormous
  directory can overshoot it. The cap applies to each of the two scans in an `--apply` run. The
  Pro child has its own cap of 30 minutes.
- Coverage. `coverage run --branch stalehost.py --self-test` reports @@COVWIN@@ on Windows. On Linux it
  reports @@COVLIN@@: the lines it cannot reach there are the success path of the Windows registry
  read. The success path of `_load_arcpy` is reached with a stand-in module on `sys.modules`. The
  `if __name__` guard carries `# pragma: no branch`, because its false branch is an import and the
  file is only measured by running it. Code that runs in the child process is not measured by
  coverage, so the worker is also run in process.
- The self-test prints @@COUNT@@ assertions on Windows with Python 3.13 and @@COUNT@@ on Linux with
  Python 3.12. They are equal because no case is skipped on either host.

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
