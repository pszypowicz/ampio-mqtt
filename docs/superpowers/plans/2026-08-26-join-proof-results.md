# Join-rule proof results (2026-08-26)

Live run of the Task 1 probe (`/tmp/join_proof.py`) against the full catalogue,
admin account, `device_api/to/<mac>/get_data` fan-out plus `devicesDetails` and
`devices` config reads. Read-only: no `/api/set`, no writes.

## Coverage

36 of 39 modules answered `get_data` inside the 45 second window. 3 stayed
silent, listed by catalogue `id`: 1, 35, 36. 36/39 clears the >= 30 acceptance
bar, so no second run was needed.

## Winning out-no key: last `leafId` segment, not `funkcja`

For every kind where the leaf-key join produces a clean single-descType
majority, `funkcja` does worse or ties only on a tiny sample:

| typ_komponentu  | leaf-key winner (hit rate) | funkcja hit rate                                                       | verdict                                       |
| --------------- | -------------------------- | ---------------------------------------------------------------------- | --------------------------------------------- |
| przekaznik      | OUTPUTS 37/39 (95%)        | 48 hits on 39 objects (over 100%, matches multiple entries per object) | leaf wins - funkcja is not a clean 1:1 signal |
| roleta_procenty | ROLLER 15/15 (100%)        | 12/15 (80%)                                                            | leaf wins                                     |
| roleta_lamelki  | ROLLER 5/5 (100%)          | 5/5 (100%)                                                             | tie, but n=5                                  |
| led             | OUT_OC_U8 2/2 (100%)       | 0/2 (0%)                                                               | leaf wins outright                            |
| rgbw            | descType 34, 4/4 (100%)    | 1/4 (25%)                                                              | leaf wins                                     |

`funkcja` never beats the leaf key and in several kinds it clearly
under-matches (led, rgbw, roleta_procenty) or over-matches into noise
(przekaznik, and the ambiguous kinds below). The last `leafId` segment is the
out-no join key; `funkcja` is not.

## Proven pairs

A kind counts as PROVEN when its leaf-key hits land on exactly one descType
whose hit rate exceeds 50% of `total`, with every other descType staying
below that bar (misses explainable as modules that never had a description
written for that output).

| typ_komponentu  | descType       | hits/total | notes                                                                                 |
| --------------- | -------------- | ---------- | ------------------------------------------------------------------------------------- |
| przekaznik      | 12 (OUTPUTS)   | 37/39      | confirms the already-proven pair                                                      |
| roleta_procenty | 26 (ROLLER)    | 15/15      | confirms the already-proven pair                                                      |
| roleta_lamelki  | 26 (ROLLER)    | 5/5        | new pair, shares descType with roleta_procenty                                        |
| led             | 16 (OUT_OC_U8) | 2/2        | new pair; small sample (n=2)                                                          |
| rgbw            | 34             | 4/4        | new pair; small sample (n=4); descType 34 has no symbolic name in the documented enum |

## Unproven kinds

None of these land on a single dominant descType - each has two or more
descTypes simultaneously clearing the 50% bar, so the join is ambiguous and
not shipped:

| typ_komponentu | total | leaf-key hits by descType                                 | why unproven                                                            |
| -------------- | ----- | --------------------------------------------------------- | ----------------------------------------------------------------------- |
| bit32          | 6     | {29: 6, 32: 6}                                            | two descTypes tied at 100%                                              |
| flaga          | 7     | {FLAG_BIN: 7, INPUTS: 5, OUTPUTS: 4, DEVICE_NAME: 1}      | three descTypes each clear 50% (FLAG_BIN 100%, INPUTS 71%, OUTPUTS 57%) |
| lin_wej        | 45    | {38: 45, 39: 45, OW: 23, DEVICE_NAME: 19, FLAG_BIN: 3}    | two descTypes tied at 100%, plus OW at 51%                              |
| satel_alarm    | 2     | {SatelZone: 2, SatelOutput: 2}                            | two descTypes tied at 100%; also too small a sample to judge            |
| temp           | 6     | {DEVICE_NAME: 6, 38: 6, 39: 6, OW: 6, 40: 6, FLAG_BIN: 1} | five descTypes tied at 100%                                             |

## Decision

DESC_TYPE_BY_KIND ships exactly these pairs: przekaznik -> 12, roleta_procenty -> 26, roleta_lamelki -> 26, led -> 16, rgbw -> 34.
