# BeyondATC Unmutable Chimes Fix

A small patch that silences BeyondATC's two hardcoded UI chimes: the logo chime on launch and the "BeyondATC connected" bark once the sim connection completes.

## Overview
Both sounds fire unconditionally from `Assembly-CSharp.dll` — there is no in-app setting to disable either one (the "UI sounds" toggle in Options only gates a different set of cues: mic on/off, radio auto-tune, speech-recognized). This patch NOPs out the two `AudioSource.Play()` call sites that trigger them.

## Patch Details
Two call sites are patched, both a 3-instruction MSIL sequence (`<load AudioSource field>; ldfld; callvirt AudioSource::Play()`) turned into NOPs:

| Sound | Method | Field |
|---|---|---|
| Launch chime | `AudioManager.OnTriggerChimeSfx()` | `chimeSfx` |
| "Connected" bark | `MainInterface`'s `PanelSequence()` iterator (`<PanelSequence>d__198.MoveNext`) | `connectedBark` |

The script doesn't hardcode file offsets or metadata tokens. It resolves the target methods and fields by name via the assembly's .NET metadata tables (`dnfile`), then wildcard-scans each method's IL for the `Play()` call shape and NOPs it in place. This survives BeyondATC rebuilds shifting file/method offsets around, as long as the member names and call shape stay the same.

## Usage
Close BeyondATC, then run (as Administrator — the DLL lives under `Program Files`):

```
python patch_beyondatc_sounds.py "C:\Program Files\BeyondATC\BeyondATC_Data\Managed\Assembly-CSharp.dll"
```

Requires the `dnfile` Python package (`pip install dnfile`).

A `.bak` file is created on first run. Restoring it reverts the change. After a BeyondATC update, rerun the script; it will either apply the patch again or report that the pattern no longer matches.

## Notes
This repository contains only the patcher and documentation. No BeyondATC files are included.
