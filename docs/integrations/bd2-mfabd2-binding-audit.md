# BD2 / MFABD2 selected Todo binding audit

Source audited on 2026-08-30:

- upstream repository: `https://github.com/sunyink/MFABD2`
- audited `main`: `55b6849a5770de0a75a129ee523f6e7ebfaed8dd`
- installed stable release: `v4.4.1`
- official Windows x64 archive SHA-256: `798cb954ebad045dc7caf7304679ad33fcf9da24cecf29f3c1f79f321218ca8f`

The release is installed on the local disk only. Neither the release nor the
source checkout is run from the NAS.

## Selected Todo mapping

| Manager operation | MFABD2 entries | current status | reason |
| --- | --- | --- | --- |
| `attach-home` | `Global_ToHomePage`, then `Global_ToHomePage_Enter` | blocked | The latter requires the upstream OCR and color recognizers, but no same-run live replay has established a Manager receipt. |
| `daily-claim` | `RewardsDaily_Start`, then `Pass_HomePage` | blocked | These are reward-only entries, but the MFA Agent IPC and per-entry node transcript have not been exercised against the installed account. |
| `stamina-sweep` | `QuickHunt_Start` | blocked | The safe candidate override disables `QuickHunt_AdventureRoute` and `QuickHunt_CrystalCave`, keeps hunting grounds, and routes remaining free rice back to hunting grounds. A same-run AP-before/AP-after and reward receipt is still missing. |

`Daily_HomePage` is deliberately excluded: it includes guild, restaurant,
intimacy and daily prayer behavior and is not equivalent to a reward-only Todo.
The following entries are also excluded from this binding: gacha, PVP, shop
arbitrage, events, event rewards and mail.

## Promotion gate

Do not publish an execution-ready Adapter until an isolated local replay proves:

1. MFAAvalonia/Python Agent IPC starts and exits under the Adapter Host job.
2. Each selected entry produces its own Maa task/node transcript.
3. `attach-home` ends with the two-part upstream home recognition.
4. `daily-claim` proves both reward pages were inspected and records whether a
   claim was made or no reward was available.
5. `stamina-sweep` records free-rice before/after values and a fresh reward
   frame, with adventure route and crystal cave absent from the node transcript.

Until those conditions exist, transport exit, `StopTask`, an empty red-dot scan,
or process exit must not complete any BD2 Todo.
