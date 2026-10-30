# Field UI validation

Use this runbook for a Windows Capsule Vision Inspection startup/navigation/Close smoke test and for
isolated native Qt layout checks. It is for engineers and automated agents.
It does not authorize Production Start, camera connection/capture, PLC operations,
configuration edits, deployment or environment repair. See [architecture](architecture.md)
and [configuration](configuration.md) for application ownership and saved settings.

## Evidence and site profile

**Verified on yy30lane1, 2026-10-24:** SSH identity/revision checks, shortcut inspection,
an Interactive/Limited scheduled task launching the normal desktop shortcut,
native Qt screen capture, and operator-assisted five-page navigation/theme/Close.
Retained process handles established real Python process exit, independently of
window disappearance. Earlier isolated UI checks used the same task mechanism;
they do not establish a real `app.py` lifecycle pass. Record launch, navigation,
process-exit results and task cleanup separately for each smoke run.

| Observation | Verified example; rediscover on another machine |
| --- | --- |
| SSH alias / Windows host | `yy30lane1` / `DESKTOP-L4O6HDH` |
| Repository | `C:\Users\YY\Projects\capsule-vision-inspection` |
| Expected revision | `5fb2ddc52c946f705a0c02548079ed5a926317ad` |
| User / interactive session | `DESKTOP-L4O6HDH\YY`, active console Session 1 |
| SSH PowerShell session | Session 0; not the interactive desktop |
| OS / provisioned runtime | Windows 10 IoT Enterprise LTSC 19044; Python 3.14.8, Qt/PySide6 6.11.2 |
| Display | HDMI, 1080×1920, inverted portrait, 96 logical DPI, DPR 1.0; work area 1080×1880 |
| Desktop shortcut | `多通道胶囊检测.lnk`; target is the user's `WindowsApps\wt.exe` |
| Shortcut arguments / working directory | `pwsh.exe -Command "uv run --no-sync python app.py"`; repository above |
| Python resolved in repository | `.venv\Scripts\python.exe`, prefix is the existing repository `.venv` |

The alias is a client-local convenience, not a project configuration setting.
This field Python version differs from the development target; preserve it as found.
Keep run artifacts under a fresh `outputs/` directory on development and a uniquely
named temporary directory on the field PC. Do not commit screenshots, host inventories
or operational configuration. Do not include credentials in logs or this document.

## Human prerequisites and installation

The following is **general Windows guidance**, not a claim that installation or
permission changes were performed on yy30lane1.

1. Identify the machine owner, authorized Windows account, repository location and
   expected revision. Arrange a stopped application and an engineer who can unlock
   the desktop, approve access and close an application if automation is blocked.
2. Provision the supported Windows/vendor installation, licenses, models, Recipes,
   saved MachineLayout, CameraSources and `app.json` using the deployment process.
   A clone does not contain these assets. Do not copy a development `.venv` to the
   field PC or run dependency synchronization to make a smoke test work.
3. Install and sign into Tailscale on both endpoints using the organization's
   approved process. A human performs authentication and device approval; keep
   tokens/private keys outside the repository. Verify the intended host in the
   tailnet and the permitted SSH route. See [Tailscale Windows setup](https://tailscale.com/docs/install/windows).
4. Have an administrator provision Windows OpenSSH Server and the engineer's
   public key. Review firewall/access scope with the owner. This workflow uses
   Windows OpenSSH over Tailscale; Tailscale connectivity alone is not SSH access.
   Follow [Microsoft OpenSSH setup](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse?tabs=powershell)
   for installation, key placement and account-specific permissions.
5. Install/configure Parsec through its approved process. The human signs in,
   enables the intended host and grants keyboard/mouse access where needed.
   Establish an unlocked Windows login for the same desktop account used by the
   interactive task. See [Parsec installation](https://support.parsec.app/hc/en-us/articles/32381368159124-Install-Parsec-App-on-Windows)
   and [hosting/permissions](https://support.parsec.app/hc/en-us/articles/32381747079572-Hosting-and-Permissions).
6. Confirm the normal desktop shortcut works for that user, including its target,
   arguments, working directory and existing `uv`/PowerShell availability. Preserve
   display orientation, scaling, vendor PATH and runtime settings during validation.

Administrator-only OpenSSH setup examples, **not executed by this runbook's smoke
procedure**, are:

```powershell
Get-WindowsCapability -Online | Where-Object Name -Like 'OpenSSH*'
# Only after installation approval, if Server is absent:
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
```

Use official key/ACL instructions appropriate to the account. Do not paste a
private key, blindly rewrite ACLs, disable host-key checking or expose PLC ports
to repair access. Installation, authentication and security changes require the
appropriate human owner; agents must not click through those prompts.

## SSH identity and quoting

**General setup:** create a new local SSH key only if required, at an unused path,
and arrange public-key authorization with the owner. Add a client-local host entry
to `%USERPROFILE%\.ssh\config`, substituting approved values:

```sshconfig
Host field-vision
    HostName <approved-Tailscale-IP-or-DNS-name>
    User <Windows-account>
    IdentityFile C:/Users/<engineer>/.ssh/<field-key>
    IdentitiesOnly yes
```

Verify the server fingerprint through a trusted owner/console before first use.
An owner can display the server's public host-key fingerprint with
`ssh-keygen -lf C:\ProgramData\ssh\ssh_host_ed25519_key.pub`. Unexpected key changes
need investigation, not automatic deletion of `known_hosts` entries.

**Verified read-only checks** (replace the alias on other installations):

```powershell
$FieldHost = 'yy30lane1'
ssh -G $FieldHost | Select-String '^(hostname|user|port|identityfile) '
ssh -o BatchMode=yes -o ConnectTimeout=10 $FieldHost whoami
ssh $FieldHost quser
ssh $FieldHost git -C C:/Users/YY/Projects/capsule-vision-inspection rev-parse HEAD
ssh $FieldHost git -C C:/Users/YY/Projects/capsule-vision-inspection status --short
```

On the field host, `Get-Service sshd`, `tailscale status --json` and `tailscale ip -4`
were also verified. Retain only the required host/status fields from inventories;
tailnet output can contain unrelated device/account information. The
[Tailscale CLI reference](https://tailscale.com/docs/reference/tailscale-cli) describes these diagnostics.

Do not assume the remote default shell is PowerShell. A semicolon-separated command
sent to the Windows default SSH shell may become arguments to the first program.
For multi-line PowerShell, use the **verified explicit-shell transport**:

```powershell
$remoteScript = @'
$ProgressPreference = 'SilentlyContinue'
whoami
quser
Get-Process -Id $PID | Select-Object Id,SessionId,ProcessName
'@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($remoteScript))
ssh $FieldHost powershell.exe -NoProfile -EncodedCommand $encoded
if ($LASTEXITCODE -ne 0) { throw 'Remote command failed' }
```

For longer helpers, write a reviewed UTF-8 file, copy it with `scp`, and execute it
explicitly. Use UTF-8 with BOM for PowerShell 5.1 files containing non-ASCII paths.
Do not treat PowerShell CLIXML progress output as application diagnostics. Record
native-command exit codes before running another command that overwrites them.

## Preflight without changing settings

Run these **read-only commands on the field host** through the verified transport
above, using the installation's actual `$repo`. Check its deployed revision first;
the current configuration hash check includes the TSV MachineLayout:

```powershell
$repo = 'C:\Users\YY\Projects\capsule-vision-inspection'
git -C $repo rev-parse HEAD
git -C $repo status --short
quser
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match 'python|uv|pwsh|WindowsTerminal|parsec|explorer' } |
    Select-Object ProcessId,ParentProcessId,SessionId,Name,CommandLine
Get-ChildItem -LiteralPath "$repo\data" -File -Recurse |
    Where-Object { $_.Extension -in '.json', '.tsv' } |
    Get-FileHash -Algorithm SHA256 | Select-Object Path,Hash
& "$repo\.venv\Scripts\python.exe" --version
```

Save configuration hashes before/after, including the current `machine.tsv`.
Read the saved language, source mode and service enablement without editing them.
Saved Web enablement can restore a listener on
normal startup; reporting enablement does not itself start Production. If normal
startup's saved behavior is not permitted at the site, stop for owner review instead
of disabling preferences for the test. Do not change language in the operational
instance. Theme switching is session-only.

Inspect shortcuts with this **verified read-only COM operation**:

```powershell
$ws = New-Object -ComObject WScript.Shell
Get-ChildItem -LiteralPath ([Environment]::GetFolderPath('Desktop')) -Filter *.lnk |
    ForEach-Object {
        $link = $ws.CreateShortcut($_.FullName)
        [pscustomobject]@{
            Path = $_.FullName; Target = $link.TargetPath
            Arguments = $link.Arguments; WorkingDirectory = $link.WorkingDirectory
        }
    }
```

Also inspect `C:\Users\Public\Desktop` when appropriate. Do not call the link's
`Save()` method. In the verified site profile the shortcut runs `uv run --no-sync`
in the repository; preserve that chain rather than substituting a fake UI launcher.
Confirm actual child executable paths/PIDs after launch. A Windows virtual-environment
redirector and its Python child can represent one application: count matching
visible Qt windows and trace parent PIDs before declaring a duplicate instance.

## Interactive desktop and normal launch

SSH's Session 0 is suitable for files/process observations, not proof of visible
GUI execution. A successful `ssh ... python app.py` is not an interactive smoke
test. Likewise, a SYSTEM or non-interactive scheduled task is not a substitute
for the logged-in user's desktop.

**Verified task mechanism:** stage reviewed helpers/evidence in a fresh directory
outside the operational repository, for example under that user's `%TEMP%`.
Register a uniquely named, manually started task for the account returned by
`whoami`/`quser`, with Interactive logon and Limited run level. The account must
actually be logged in; confirm the helper's PID is in the expected active session.
Do not assume that every workstation uses Session 1, or that multiple logins for
the same user are interchangeable.

The following pattern was verified for a native capture helper. Run it on the
field host after setting the installation-specific variables and copying the helper:

```powershell
$account = '<verified-host>\<logged-in-user>'
$taskName = 'ViMo-UI-' + [guid]::NewGuid().ToString('N')
$scratch = '<new-absolute-evidence-directory>'
$pythonw = Join-Path $repo '.venv\Scripts\pythonw.exe'
$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument ('"' + $scratch + '\snapshot.py" before') -WorkingDirectory $scratch
$principal = New-ScheduledTaskPrincipal -UserId $account `
    -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal
Start-ScheduledTask -TaskName $taskName
Get-ScheduledTaskInfo -TaskName $taskName
```

See Microsoft's [scheduled-task principal reference](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtaskprincipal)
for the logon/security context. Use the actual logged-in account, not SYSTEM or a
stored password. `LastTaskResult = 0` only proves that helper's exit; also inspect
its output and timestamp. A running task is not a completed test.

**Normal-entry launch procedure:** a human double-clicks the inspected desktop
shortcut in Parsec, or a temporary Interactive task uses
`Start-Process -FilePath $shortcutPath` on that exact `.lnk`. Check for an existing
`app.py` process before either path. The shortcut's working directory and arguments
must remain unchanged. A launcher script may monitor process handles and record
exits, but must not send Production commands or silently relaunch after failure.
Validate this normal-entry path with a real window/process observation; the
isolated task mechanism alone is not evidence that the normal application launched.
The temporary task/shortcut path was verified on yy30lane1; the `wt.exe` shortcut
resolved to the provisioned `.venv` redirector and its existing Python child in
Session 1. Both were monitored; two parent/child PIDs were one visible application.

Use Parsec for human input or the existing approved computer-use tool when it can
observe the viewer reliably. Target the returned Parsec window, inspect a fresh
image/accessibility state, perform one known action, then reobserve. Do not type
commands into a terminal through UI automation. If the shortcut uses Windows
Terminal, it is only the existing launcher; do not automate that terminal's input.
Do not guess remote control coordinates from a remembered viewport.

## Native screenshots and geometry

Capture in the active field session using its existing PySide6/native Windows
backend. An observer can read the desktop without creating another application instance
or importing the inference/camera adapters. The core of the **verified native
capture method** is:

```python
import json
import sys
import traceback
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

out = Path("<existing-absolute-evidence-directory>")
sys.stdout = sys.stderr = (out / "observer.log").open(
    "w", encoding="utf-8", buffering=1
)
qt = QApplication(["field-desktop-observer", "-platform", "windows"])

def capture():
    try:
        rows = []
        for index, screen in enumerate(qt.screens()):
            rows.append({
                "geometry": screen.geometry().getRect(),
                "workarea": screen.availableGeometry().getRect(),
                "dpr": screen.devicePixelRatio(),
                "logical_dpi": screen.logicalDotsPerInch(),
                "orientation": screen.orientation().name,
            })
            pixmap = screen.grabWindow(0)
            assert not pixmap.isNull()
            assert pixmap.save(str(out / f"desktop-{index}.png"))
        (out / "screens.json").write_text(json.dumps(rows), encoding="utf-8")
        qt.quit()
    except Exception:
        traceback.print_exc()
        qt.exit(1)

QTimer.singleShot(600, capture)
raise SystemExit(qt.exec())
```

Use a fresh output name/directory per observation and bounded helper lifetime.
Record `ProcessIdToSessionId`, `QApplication.platformName()` and scaling environment
variables as well. The observer's Qt geometry describes the desktop, not the
operational application's internal widgets. For the real application, match the
visible application HWND to its Python PID. The verified field baseline used
`ViMo · Production`; current builds use `Capsule Vision Inspection · Production`.
Read `GetWindowRect`, `GetClientRect`, `GetDpiForWindow` and `IsZoomed`, and retain
the native desktop image.
These are read-only observations, not message/input injection.

Do not set `QT_QPA_PLATFORM=offscreen`, `QT_SCALE_FACTOR` or font overrides for field
acceptance. Distinguish native pixels, work area, application client pixels and
Qt logical units. Equate them only when the application's scaling/DPR evidence
supports it; do not infer widget dimensions from the Parsec viewer's size.
Inspect PNG dimensions, non-black content, actual application title/controls and
taskbar/work-area relationship. An unlocked wallpaper capture with no application window
proves desktop observation only, not startup success.

## Real application smoke checklist

Record each step, including failures. The first clean cycle should establish:

1. Matching approved revision, unchanged worktree, known shortcut/environment,
   active unlocked desktop and no conflicting application instance.
2. Launch through the normal shortcut. Record new process IDs, executable paths,
   parent chain and one expected visible Qt window. Confirm native maximization
   without manually resizing. Record initial visible READY/UNCONFIGURED/FAULT and
   any warnings; do not repair settings to obtain READY.
3. Visit Production, Recipe, Communication, History and Vision using the top
   navigation. Observe selected-page transitions and the primary controls:
   player/counters; Recipe model/rules/Save; reporting/Web/diagnostics; History
   filters/table/matrix; Vision source cards/canvas/Save. Empty History or absent
   live images can be legitimate. Do not press operational action buttons.
4. Switch the theme and confirm visible presentation changes. Leave the saved
   language alone; do not click Save/Apply/Discard, source discovery/connection,
   capture, cache preparation, register read/write or Web power for this smoke test.
5. Close via the real application's normal window Close action. Record any dirty
   prompt or delayed-cleanup state instead of bypassing it.
6. Confirm all identified application Python processes actually exit. Hold process
   handles before Close when exit codes are required; inspect `HasExited`,
   `WaitForExit`/`ExitCode` from a separate observer, never by blocking Qt dispatch.
   Requery CIM process/session information and any application-owned listener.
   The window disappearing or a launcher exiting is insufficient.
7. Compare saved JSON hashes and Git status, then perform a second clean cycle
   where practical. Separate a passed first cycle from an unexecuted second one.
   Check for accumulating application processes or new launch failures.

Normal application startup can open/checkpoint the History database and apply its
ordinary retention policy. Do not equate SQLite byte changes with configuration
edits, copy an active database without its WAL/SHM requirements, or delete it for
smoke preparation. No new Production cycles should be admitted by navigation.

## Focused UI checks and optional tools

Real normal-entry smoke and isolated layout checks answer different questions.
For layout/multilingual work, use a temporary source preview and synthetic data
outside the operational installation, with engine creation and real network/device
operations forbidden. Capture the preview in the logged-in desktop, label its
evidence as isolated, and never count it as a real operational launch/Close pass.

Prefer the [README developer workflow](../README.md#developer-tools-and-checks).
On development, a small relevant check can be:

```powershell
$pytestBase = Join-Path 'outputs/pytest' ([guid]::NewGuid().ToString('N'))
uv run --no-sync pytest tests/integration/test_global_shell.py --tb=short --basetemp $pytestBase
```

Allocate a new `--basetemp` for every invocation. Do not run a full suite on an
operational field installation merely to establish UI acceptance. Documentation
and smoke observation do not require GPU, camera, PLC or throughput tests.

Use existing OpenSSH, PowerShell, Task Scheduler, Parsec and PySide6 first. Install
an additional tool only for a demonstrated missing capability within an approved
scope; document its source/version, isolation, permissions and removal. Do not
modify the vendor `.venv`, drivers, security settings or system PATH to make a
desktop automation tool work. New access, installation/authentication or elevation
requires the appropriate human; do not store credentials in task arguments.

## Troubleshooting, cleanup and handoff

| Observation | Response |
| --- | --- |
| SSH refused, wrong host or key mismatch | Check owner-confirmed identity, tailnet reachability, `sshd` and authorized-key setup. Do not disable verification or repair permissions blindly. |
| SSH works but GUI is invisible/black | Check PID session, actual unlocked desktop and task principal. Session 0/offscreen success is not field acceptance. Ask the logged-in operator if blocked. |
| Parsec app is listed but capture/input fails | A title bar/accessibility root is not proof of usable remote video. Re-select/reobserve once under the tool's recovery rules; do not guess clicks or invent a new automation system. Request minimal operator input. |
| `FrameArrived timed out`, `window capture timed out`, or `coordinate input geometry is unavailable` | These failures were observed with the local Parsec automation path. Native Session 1 capture can still work. Record the failed path separately from human-assisted or isolated observations. |
| Duplicate `app.py` processes/window | Identify the launcher/redirector parent chain and owner. Do not start another app or terminate unrelated Python processes. |
| UNCONFIGURED, FAULT, startup or navigation exception | Preserve native image, console/log evidence, revision and JSON hashes. Report a reproducible defect; do not broaden the smoke phase into recovery/refactoring. |
| Close remains pending | Keep the actual owner, inspect remaining processes/threads and request operator assistance. A timeout is not cleanup; never force-cancel native SDK work. |

After verified completion, inspect the exact temporary tasks and their outputs:

```powershell
Get-ScheduledTask -TaskName $taskName
Get-ScheduledTaskInfo -TaskName $taskName
# Only after this exact owned helper has finished:
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
```

Recheck process IDs/command lines, saved JSON hashes, field Git status and task
absence. Do not use a broad `Stop-Process python*` or kill an owner simply because
a window closed. A failed isolated observer is not a successful normal Close;
record its failure and cleanup separately. Preserve screenshots, process/exit
records and logs in the run's evidence directory; ignored status does not permit
deleting old assets. Retained scratch evidence is not deployment.

Handoff must state which launch/navigation/Close cycles passed, which failed or
were not executed, initial state, revision/environment, screenshots, exit evidence,
configuration changes, temporary tools/tasks and required human actions. If input,
unlocking or permissions are blocked, keep the dependent checks unverified rather
than substituting an isolated UI harness or claiming autonomous acceptance.
