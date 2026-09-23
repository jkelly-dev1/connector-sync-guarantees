# Sample run

Captured 2026-09-16 on one Linux machine with Python 3.13.7, superseding the
2026-08-23 capture. Every command below was executed exactly as written, in
this order, from a clean checkout.

No output below was altered, and `scripts/check_sample_run.py` checks
that: it re-runs each command below and requires the same output, byte for
byte, and CI runs it.

Only the suite's last line is not compared verbatim. Its test count is
checked; its duration is wall-clock, and that duration is the only wall-clock
figure anywhere in this repository. Every other duration below is simulated
seconds from a clock the code advances explicitly, so the capture, the
committed results and the README agree on any machine.

The last block is the proof rather than the claim: the whole experiment set is
run a second time and diffed against the first, and the diff is empty.

The whole thing needs `python3` AND NOTHING ELSE. No container, no database, no
service, no network, no credentials.

```
$ python3 scripts/exp1_rate_limits.py
=== rate_bound (allowance 100000, 5 calls/sec) ===
  naive         complete  records  4000  calls    83  throttled   62  sim      4.2s
  fixed_sleep   complete  records  4000  calls    21  throttled    0  sim      8.1s
  token_bucket  complete  records  4000  calls    21  throttled    0  sim      7.5s
  aimd          complete  records  4000  calls    21  throttled    0  sim      5.8s

=== cap_bound (allowance 100, 5 calls/sec) ===
  naive         INCOMPLETE records   500  calls   100  throttled   76  sim      5.0s
  fixed_sleep   INCOMPLETE records  2000  calls   100  throttled    1  sim     40.0s
  token_bucket  INCOMPLETE records  2000  calls   100  throttled    1  sim     39.5s
  aimd          INCOMPLETE records  1860  calls   100  throttled    8  sim     25.3s

=== access patterns, same 4,000 records, generous allowance ===
  paged_scan                     complete  calls     21  sim      7.5s
  paged_scan_plus_detail_fetch   complete  calls   4021  sim   1607.5s
  bulk_export                    complete  calls      1  sim    900.0s

=== access patterns under the tight daily cap ===
  paged_scan                     complete  calls     21  sim      7.5s
  paged_scan_plus_detail_fetch   INCOMPLETE calls    100  sim     39.5s
  bulk_export                    complete  calls      1  sim    900.0s

under a per-second ceiling the PACED strategies spread by 1.38x; all four including naive spread by 1.94x (naive fastest, fixed_sleep slowest)
under the daily cap, 0 of 4 strategies completed
the N+1 access pattern costs 191x the calls of a paged scan
prediction: QUALIFIED
wrote results/exp1_rate_limits.json

$ python3 scripts/exp2_incremental_loss.py
=== atlas ===
  naive              accuracy 0.9831  missing    0  stale   66  ghost  104
  +tiebreaker        accuracy 0.9831  missing    0  stale   66  ghost  104
  +inclusive_bound   accuracy 0.9831  missing    0  stale   66  ghost  104
  +overlap_60s       accuracy 0.9849  missing    0  stale   59  ghost  104
  +overlap_300s      accuracy 0.9854  missing    0  stale   57  ghost  104
  +deletes_api       accuracy 0.9854  missing    0  stale   57  ghost    1
  overlap sweep (tiebreaker + inclusive bound already on)
    overlap      0s  accuracy 0.9831  wrong  170  redundant reads     91  calls   117
    overlap     30s  accuracy 0.9841  wrong  166  redundant reads    171  calls   117
    overlap     60s  accuracy 0.9849  wrong  163  redundant reads    263  calls   117
    overlap    120s  accuracy 0.9854  wrong  161  redundant reads    473  calls   117
    overlap    300s  accuracy 0.9854  wrong  161  redundant reads   1034  calls   117
    overlap    600s  accuracy 0.9856  wrong  160  redundant reads   1960  calls   117
    overlap   1800s  accuracy 0.9856  wrong  160  redundant reads   5474  calls   117
=== beacon ===
  naive              accuracy 0.9863  missing    0  stale   53  ghost  124
  +tiebreaker        accuracy 0.9863  missing    0  stale   53  ghost  124
  +inclusive_bound   accuracy 0.9863  missing    0  stale   53  ghost  124
  +overlap_60s       accuracy 0.9876  missing    0  stale   48  ghost  124
  +overlap_300s      accuracy 0.9876  missing    0  stale   48  ghost  124
  +scan_archived     accuracy 0.9876  missing    0  stale   48  ghost    3
  overlap sweep (tiebreaker + inclusive bound already on)
    overlap      0s  accuracy 0.9863  wrong  177  redundant reads    210  calls   137
    overlap     30s  accuracy 0.9871  wrong  174  redundant reads    338  calls   137
    overlap     60s  accuracy 0.9876  wrong  172  redundant reads    442  calls   137
    overlap    120s  accuracy 0.9876  wrong  172  redundant reads    645  calls   137
    overlap    300s  accuracy 0.9876  wrong  172  redundant reads   1329  calls   137
    overlap    600s  accuracy 0.9876  wrong  172  redundant reads   2401  calls   137
    overlap   1800s  accuracy 0.9879  wrong  171  redundant reads   6431  calls   137

best configuration on atlas: 58 changes still wrong
  SILENT_UPDATE missed 54 of 85 (63.5%)
  each wrong record blamed on its LATEST unreflected change:
    SILENT_UPDATE       45 of 58 wrong records
    UPDATE               8 of 58 wrong records
    IN_FLIGHT_UPDATE     4 of 58 wrong records
    DELETE               1 of 58 wrong records
prediction: REFUTED
wrote results/exp2_incremental_loss.json

$ python3 scripts/exp3_webhooks_vs_polling.py
=== atlas ===
  poll_only                  missed   67/1188 (0.0564)  p50  154.1s  max 17802.6s  calls   213
  webhook_only               missed   44/1188 (0.0370)  p50    6.0s  max    39.9s  calls  1165
  webhook_plus_poll          missed    4/1188 (0.0034)  p50    6.1s  max  2219.3s  calls  1357
  webhook_plus_poll_outage   missed   51/1188 (0.0429)  p50  111.6s  max 17802.6s  calls   485
=== beacon ===
  poll_only                  missed   55/1191 (0.0462)  p50  152.5s  max 15714.0s  calls   137
  webhook_only               missed   46/1191 (0.0386)  p50    6.3s  max    40.0s  calls  1186
  webhook_plus_poll          missed    0/1191 (0.0000)  p50    6.3s  max   295.3s  calls  1282
  webhook_plus_poll_outage   missed   44/1191 (0.0369)  p50  103.3s  max  7918.7s  calls   411

poll alone   p50 154.1s, missed 67
webhook+poll p50 6.1s, missed 4
webhook-to-poll delta: 58 changes the poll caught first
outage arm   missed 51, subscription active at end: False
             881 events dropped at the source AFTER the subscription was disabled
             it was disabled at t=7744.6s and never re-enabled
prediction: held
wrote results/exp3_webhooks_vs_polling.json

$ python3 scripts/exp4_reconciliation.py
=== atlas ===
  none                   calls     0  detected    0/  58 (0.0000)  missing   0 ghost   0 wrong   0
  count_check            calls     1  detected    0/  58 (0.0000)  missing   0 ghost   0 wrong   0
  partitioned_checksum   calls    41  detected    1/  58 (0.0172)  missing   0 ghost   1 wrong   0
  full_id_inventory      calls     4  detected    1/  58 (0.0172)  missing   0 ghost   1 wrong   0
  full_field_compare     calls    20  detected   58/  58 (1.0000)  missing   0 ghost   1 wrong  57

=== beacon ===
  none                   calls     0  detected    0/  51 (0.0000)  missing   0 ghost   0 wrong   0
  count_check            calls     1  detected    0/  51 (0.0000)  missing   0 ghost   0 wrong   0
  partitioned_checksum   calls    43  detected    3/  51 (0.0588)  missing   0 ghost   3 wrong   0
  full_id_inventory      calls     4  detected    3/  51 (0.0588)  missing   0 ghost   3 wrong   0
  full_field_compare     calls    39  detected   51/  51 (1.0000)  missing   0 ghost   3 wrong  48

count check detected 0 of 58 (0.0%) for 1 call(s)
partitioned checksum detected 1 of 58 (1.7%) for 41 calls
full field compare detected 58 of 58 (100.0%) for 20 calls
ONLY full_field_compare sees a silently wrong VALUE: 57 of them

AT THIS CORPUS SIZE (4000 records) THE COST ORDER IS NOT THE EXPECTED ONE:
  full_id_inventory      4 calls, 1.7% detected
  partitioned_checksum  41 calls, 1.7% detected  -- strictly worse here
  full_field_compare    20 calls, 100.0% detected  -- 5.0x the cheapest useful check, and it catches everything
A partitioned checksum pays a fixed probe cost per partition, so it
only wins once a full id inventory stops fitting in the budget.
prediction: REFUTED
wrote results/exp4_reconciliation.json

$ python -m pytest -q
........................................................................ [ 63%]
.........................................                                [100%]
113 passed in 1.63s

$ python3 scripts/check_readme_numbers.py
73 figures and 4 charts re-derived from results/*.json and checked against README.md
all present

$ the reproducibility claim, checked
$ cp -r results /tmp/run-a && for s in scripts/exp*.py; do python3 $s >/dev/null; done && diff -r /tmp/run-a results
(no output from diff: all four result files are byte-for-byte identical)
```
