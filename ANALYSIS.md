# BeyondATC "Unmutable Chimes" — Technical Analysis

**Date:** 2026-07-19
**Research Method:** .NET metadata inspection (ilspy-mcp decompilation) + IL byte-pattern verification
**Analysis Tool:** ilspy-mcp (decompilation), `dnfile` (metadata/IL parsing, Python)
**Target:** `BeyondATC_Data/Managed/Assembly-CSharp.dll` (Mono-scripted Unity title)

---

## Executive Summary

BeyondATC plays two `AudioSource.Play()` cues that have no corresponding settings toggle:

1. A logo chime on launch.
2. A "BeyondATC connected" bark once the sim connection sequence finishes.

The app's Options screen exposes a single "UI sounds" `BoolValue` (`uiSoundsEnabledSetting`), but it only gates three *other* cues — mic on/off (`micOnSfx`/`micOffSfx`) and radio auto-tune (`radioAutoTuneSfx`) — plus speech-recognized in `AudioManager.OnSpeechRecognized()`. The chime and connected-bark calls are unconditional `AudioSource.Play()` invocations with no guard at all.

Since `Assembly-CSharp.dll` is standard Mono/IL managed code (not IL2CPP — the `Managed/` folder ships plain .NET assemblies), this required no native disassembly; the logic is directly decompilable and directly IL-patchable.

---

## Investigation Path

```
┌────────────────────────────────────────────┐
│ BeyondATC_Data/Managed/Assembly-CSharp.dll  │
├────────────────────────────────────────────┤
│ AudioManager (MonoBehaviour)                │
│   OnTriggerChimeSfx() -> chimeSfx.Play()    │
│   wired to onLogoChime event, no gate       │
│                                              │
│ UI.MainInterface (MonoBehaviour)             │
│   PanelSequence() coroutine                 │
│     ... yield return new WaitForSeconds(.2) │
│     onLoadObjects.Invoke();                 │
│     connectedBark.Play();   <- unconditional│
└────────────────────────────────────────────┘
```

`AudioManager` was found by searching assembly members for "Chime"; `MainInterface.connectedBark` was found by searching for "Connected". Both hits decompiled cleanly via ilspy-mcp, confirming the exact call sites and that neither is gated by `uiSoundsEnabledSetting` (unlike the sibling `OnSpeechRecognized`/`OnAutoTunedRadio` methods in the same class, which do check it).

`BeyondATC.exe` itself (the Unity native launcher, briefly opened in Ghidra/Hydra) is not where this logic lives — it's a bootstrap stub. The actual game/UI logic, including all `AudioSource.Play()` call sites, is managed IL in `Assembly-CSharp.dll`, so ilspy-mcp (a .NET decompiler) rather than native disassembly was the right tool.

---

## Root Cause

Both sounds are single unconditional statements:

```csharp
// AudioManager.OnTriggerChimeSfx()
public void OnTriggerChimeSfx()
{
    chimeSfx.Play();
}

// UI.MainInterface.PanelSequence() (compiler-generated iterator, MoveNext)
...
onLoadObjects.Invoke();
connectedBark.Play();
simbriefDownloadPanel.OnInitializePanel();
...
```

There's no `if (uiSoundsEnabledSetting.Value)` or equivalent check on either path, unlike the other four `AudioSource.Play()` sites in `AudioManager`. This looks like an oversight rather than an intentional always-on cue — the settings toggle exists, it's just not wired to these two.

---

## Confirmed Patch Site

Both calls compile to the same 3-instruction MSIL shape: push the `AudioSource` field, then `callvirt AudioSource::Play()`.

| Target | Method | IL sequence removed |
|---|---|---|
| Launch chime | `AudioManager.OnTriggerChimeSfx` | `ldarg.0 ; ldfld chimeSfx ; callvirt Play()` (11 bytes) |
| Connected bark | `<PanelSequence>d__198.MoveNext` | `ldloc.1 ; ldfld connectedBark ; callvirt Play()` (11 bytes) |

Confirmed via `dnfile` against the shipped `Assembly-CSharp.dll`:

```
AudioManager.OnTriggerChimeSfx (RVA 0x23aed, tiny header):
  02 7b 1d040004 6f ff03000a 2a
  ldarg.0 ; ldfld 0x0400041d (chimeSfx) ; callvirt 0x0a0003ff (AudioSource::Play) ; ret

<PanelSequence>d__198.MoveNext (RVA 0x10f370, fat header), IL offset 681:
  07 7b b80b0004 6f ff03000a
  ldloc.1 ; ldfld 0x04000bb8 (connectedBark) ; callvirt 0x0a0003ff (AudioSource::Play)
```

Both are replaced with `0x00` (`nop`) for the full 11-byte span. This is stack-neutral in both directions (the removed sequence itself has zero net stack effect — it loads a reference, drills into a field, and calls a `void`-returning method), so no other instruction in either method needs adjustment, and no relocation or offset shift occurs elsewhere in the file.

Because IL2CPP-style fixed offsets would break on every BeyondATC rebuild (recompilation renumbers RVAs and metadata tokens even for unrelated changes elsewhere in the assembly), `patch_beyondatc_sounds.py` does not hardcode any of the above hex. Instead it:

1. Walks `TypeDef`/`MethodDef` to find `AudioManager.OnTriggerChimeSfx` and `<PanelSequence>d__198.MoveNext` **by name**.
2. Resolves the `chimeSfx`/`connectedBark` field tokens and the `AudioSource::Play` memberref token **by name**, not by hardcoded token value.
3. Scans each method's IL for `<load ref>; ldfld <resolved field token>; callvirt <resolved Play token>` within a small window, and NOPs whatever byte range it finds.
4. Recognizes an already-patched method (an 11-byte run of `nop`) and skips it rather than double-patching or erroring.

If a future update renames these members, changes the audio trigger mechanism, or (for the iterator method specifically) shifts the compiler-generated class's ordinal suffix — `<PanelSequence>d__198`'s `198` is assigned by member order in `MainInterface` and can change if methods/properties are added or removed above it in source order — the script fails closed: it reports it couldn't find the call and aborts, rather than guessing.

**Verification:** ran against a scratch copy of the shipped DLL — first pass reported both 11-byte NOP writes at the expected file offsets; second pass against the same file correctly reported both targets "already patched, skipping."

---

## Security/Design Assessment

### Why this happened
`AudioManager` already has the plumbing for a UI-sounds toggle (`uiSoundsEnabledSetting`), consistently applied to mic and radio-tune cues, but the logo chime and connected bark were left as bare `Play()` calls — most likely because they're treated as "core" feedback (app launched / connection succeeded) rather than optional UI polish, even though many users would prefer to silence them like any other cue.

### Why the fix works
NOPing the `Play()` call sequence removes only the invocation; the `AudioSource` components themselves, their volume/mixer routing, and the events that trigger these methods (`onLogoChime`, `onLoadObjects`, etc.) are untouched, so nothing else in the audio or event pipeline is affected. Both methods still return normally.

### Residual Risk
- Per-build patch: relies on both members keeping their current names and the `Play()` call keeping this exact IL shape. A refactor (e.g., wrapping the call behind a helper, or renaming `connectedBark`) breaks the pattern match and requires re-deriving the field/method names via ilspy-mcp.
- The `<PanelSequence>d__198` ordinal is the most likely thing to drift across updates, since it's a compiler artifact tied to unrelated code changes in `MainInterface`, not to the sounds themselves.
- No other `AudioSource.Play()` sites were audited beyond these two named targets; only the launch chime and connected bark were in scope.

---

## References & Evidence

| Item | Contains |
|---|---|
| `patch_beyondatc_sounds.py` | The IL patcher itself, with inline metadata-resolution rationale |
| `AudioManager.OnTriggerChimeSfx` / `UI.MainInterface.PanelSequence` (ilspy-mcp decompilation) | Confirmed unconditional `Play()` call sites, contrasted with the gated sibling methods in the same class |

---

**Analysis Method:** ilspy-mcp decompilation of `Assembly-CSharp.dll` + `dnfile`-based IL verification
**Analysis Date:** 2026-07-19
**Analysis Tool Chain:** ilspy-mcp (decompiler), `dnfile` (pure-Python ECMA-335 metadata/IL parser)
