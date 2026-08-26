# connector-sync-guarantees

[![CI](https://github.com/jkelly-dev1/connector-sync-guarantees/actions/workflows/ci.yml/badge.svg)](https://github.com/jkelly-dev1/connector-sync-guarantees/actions/workflows/ci.yml)

"We do incremental sync on a modified-since timestamp" is the most common data
ingestion design in the industry. This measures one against a world where every
change is known, and counts what it misses.

The claim this repository makes in one line: a modified-since watermark is
lossy by construction, in ways that no amount of retrying, pacing or overlap
will fix, and the losses sort into kinds, so a single accuracy figure tells
you nothing about which of them you have.

A personal learning project. Claims have tests, figures come from the shipped
results files, and the predictions were recorded before the runs that refuted
several of them.

What you need to run it: `python3`. That is the list. No container, no database,
no service, no credentials, no network. The test suite finishes in about a
second and the four experiments in under a minute.

## Why there are no wall-clock timings in this repository

Everything runs on a SIMULATED CLOCK that the code advances explicitly. That is
the design decision the rest of the repository hangs off, and it buys one
thing worth more than convenience:

Every published figure is exactly reproducible. Not "identical except timings":
identical. Running all four experiments twice and diffing `results/*.json`
produces no differences at all, which is checked as part of the sample run.

A rate-limiter sweep measured against a real clock measures the interpreter,
the scheduler and the machine at least as much as it measures the strategy.
Against a simulated clock it measures the strategy. Durations here are
SIMULATED SECONDS: a property of the algorithm and the vendor's stated limits,
not of this laptop.

What that gives up is stated plainly in "What this does not measure": this is an
algorithm against a MODEL of a vendor, and no claim about real-world speed is
made or implied.

## The two mock vendors

Fakes of a SHAPE, not reimplementations of a product. No real API is called and
no account is used. The asymmetry is the point; a connector tuned for one is
measurably wrong for the other.

| | Atlas (Salesforce-shaped) | Beacon (HubSpot-shaped) |
| --- | --- | --- |
| rate limit | daily allowance + per-second ceiling | burst limit over a rolling window + daily cap |
| rejected calls | still cost quota | do not cost quota |
| bulk | async export job, high latency | small synchronous batches |
| deletes | hard delete, dedicated endpoint with a retention window | ARCHIVE: the record stays readable with a flag |
| search | ordered scan with a tiebreaker | separate endpoint, tighter limit, silent result ceiling |

## The world, and the answer key

The world holds 4,000 records per vendor and a timeline of about 1,190 changes over 8 simulated
hours, all a pure function of a seed. The timeline is the answer key: the
harness knows every change and the instant it occurred, so "did the connector
see it" is a measurement rather than an estimate.

Six kinds of change, each modeling a documented way real ingestion loses data:

| Kind | What it models | Why a watermark scan struggles |
| --- | --- | --- |
| `UPDATE` | the ordinary case | nothing; it should be caught |
| `SILENT_UPDATE` | a bulk import or admin path that bypasses the trigger | the timestamp being filtered on does not move AT ALL |
| `IN_FLIGHT_UPDATE` | committed during a pass, stamped before it began | the scan has already read past that position |
| `LATE_CLOCK_UPDATE` | the vendor stamps from a clock 11s behind | it lands below a watermark taken from your clock |
| `DELETE` | the record is gone | there is no timestamp to scan; absence is not observable |
| `MERGE` | two records become one | the loser vanishes silently, leaving a ghost locally |

THE MIX IS INVENTED and is the single biggest assumption here. Every loss rate
below is a function of it. What transfers is which kinds are unfixable by
which mitigation, not the rates.

## 1. What a rate limit costs, and what actually fixes it

Four limiter strategies backfilling the same 4,000 records, under two
scenarios, because there are two kinds of limit and they are not the same
problem.

RATE-BOUND (generous daily allowance, 5 calls/sec ceiling):

| Strategy | Completed | Calls | Throttled | Simulated seconds |
| --- | --- | --- | --- | --- |
| naive | yes | 83 | 62 | 4.15 |
| fixed_sleep | yes | 21 | 0 | 8.05 |
| token_bucket | yes | 21 | 0 | 7.45 |
| aimd | yes | 21 | 0 | 5.85 |

```
bar length = calls issued, including rejected ones

naive         ############################################  83 calls  (62 rejected)
fixed_sleep   ###########                                   21 calls
token_bucket  ###########                                   21 calls
aimd          ###########                                   21 calls
```

The bars are derived, not drawn. `scripts/charts.py` renders each one from
`results/*.json`, and `scripts/check_readme_numbers.py` requires the rendered
block CHARACTER FOR CHARACTER, so a chart that has drifted from the data fails
the same check a stale number fails. Two rendering rules are worth knowing
before reading the rest: any nonzero value gets at least one character, so the
bars stop being proportional at the very bottom of the range and the number
beside each bar is the authoritative value; and a true zero gets no bar at
all, which happens exactly once, in section 4.

Every chart says what its bar length means on its first line, because a bar
sitting next to a four-column table is ambiguous otherwise; the reader has to
guess which column it encodes. In section 4 they would guess wrong: the bars are
the detection rate, while the larger and more eye-catching number beside them is
a call count.

The naive limiter is the fastest and that is not an endorsement. It finishes in
4.15 simulated seconds by issuing 83 calls to do 21 calls of work: 62 of them
rejected. On this vendor rejected calls still cost quota, so hammering consumes
the customer's allowance as well as wasting effort. The three paced strategies
all do the job in the minimum 21 calls and differ only in how long they take;
the spread between them is 1.38x (aimd 5.85s to fixed_sleep 8.05s). Measured
across all four, naive included, it is 1.94x, but that span is anchored on the
strategy this sentence just excluded, so it is not the number for "between
them".

CAP-BOUND (allowance of 100 calls, work that needs about 200):

| Strategy | Completed | Calls | Throttled | Records retrieved |
| --- | --- | --- | --- | --- |
| naive | **no** | 100 | 76 | 500 |
| fixed_sleep | **no** | 100 | 1 | 2,000 |
| token_bucket | **no** | 100 | 1 | 2,000 |
| aimd | **no** | 100 | 8 | 1,860 |

None of them complete. A per-second limit is a throughput problem that pacing
solves; a daily cap is a CAPACITY problem, and no pacing strategy creates
capacity. The only thing pacing changes is how much you got before you ran out,
and naive got 500 records where the two paced limiters got 2,000 and aimd got
1,860, because it spent three quarters of the allowance on rejections.

### The comparison that dwarfs both

Same 4,000 records, same limiter, three different access patterns:

| Access pattern | Completed | Calls | Simulated seconds |
| --- | --- | --- | --- |
| paged scan | yes | 21 | 7.45 |
| paged scan + a detail fetch per record | yes | 4,021 | 1,607.45 |
| async bulk export | yes | 1 | 900.05 |

```
bar length = calls issued -- NOT simulated seconds

paged scan                       #                                             21 calls
paged scan + detail fetch (N+1)  ############################################  4,021 calls
async bulk export                #                                             1 call
```

The n+1 pattern costs 191.5x the calls of a paged scan. Tuning the limiter moved
1.38x. Choosing the endpoint moved 191x. Under the tight cap the N+1 pattern
cannot complete at all while the paged scan finishes comfortably.

Nor is the bulk path free: one call, and 900 simulated seconds of job
latency. That is why it belongs to a backfill and not to a five-minute
incremental sync.

The prediction was that a smarter limiter finishes faster, and it is qualified.
True under a rate ceiling, irrelevant under a cap, and swamped by the access
pattern in both.

## 2. Does incremental sync actually catch everything?

The headline. Mitigations added one at a time, in the order a real team reaches
for them, on Atlas:

| Configuration | Accuracy | Stale | Ghost records | Total wrong |
| --- | --- | --- | --- | --- |
| naive | 0.9831 | 66 | 104 | 170 |
| + tiebreaker | 0.9831 | 66 | 104 | 170 |
| + inclusive bound | 0.9831 | 66 | 104 | 170 |
| + overlap 60s | 0.9849 | 59 | 104 | 163 |
| + overlap 300s | 0.9854 | 57 | 104 | 161 |
| + deletes endpoint | 0.9854 | 57 | **1** | **58** |

```
bar length = ghost records still present

naive               ############################################  104 ghost records
+ tiebreaker        ############################################  104 ghost records
+ inclusive bound   ############################################  104 ghost records
+ overlap 60s       ############################################  104 ghost records
+ overlap 300s      ############################################  104 ghost records
+ deletes endpoint  #                                             1 ghost record
```

Only the deletes endpoint touches ghost records: 104 to 1. No overlap, no
tiebreaker and no bound change moves that column by a single record, because a
watermark scan cannot observe an absence. A team that responds to missing
deletions by widening the overlap will widen it forever and never fix anything.

### Where the remaining 58 wrong records come from

Each wrong record is attributed to the LATEST change on it that the connector
failed to reflect, so the counts sum to the number of records rather than
double-counting:

| Cause | Wrong records |
| --- | --- |
| `SILENT_UPDATE` | 45 of 58 |
| `UPDATE` | 8 of 58 |
| `IN_FLIGHT_UPDATE` | 4 of 58 |
| `DELETE` | 1 of 58 |

Silent updates are 78 percent of the remaining damage, and they are missed 63.5
percent of the time even with every mitigation switched on. The ones that do get
caught are caught by luck. A later ordinary update on the same record happens to
re-read it. There is nothing in the wire protocol that could reveal the rest.

The prediction that a watermark sync observes every change is refuted, and it is
scored on the BEST configuration rather than the naive one, because refuting it
with the naive setup would prove nothing.

### The overlap window has brutal diminishing returns

| Overlap | Records still wrong | Redundant reads |
| --- | --- | --- |
| 0s | 170 | 91 |
| 30s | 166 | 171 |
| 60s | 163 | 263 |
| 120s | 161 | 473 |
| 300s | 161 | 1,034 |
| 600s | 160 | 1,960 |
| 1800s | 160 | 5,474 |

Going from 120s to 1800s, fifteen times the window, recovers one record and
costs 5,001 extra redundant reads. The knee is around 60 to 120 seconds and
everything past it is paying for nothing.

### Archive semantics beat hard deletes outright

The same ladder on Beacon, which archives rather than deletes:

| Configuration | Accuracy | Ghost records |
| --- | --- | --- |
| + overlap 300s | 0.9876 | 124 |
| + scan for the archived flag | 0.9876 | **3** |

An archived record stays readable, so deletion detection falls out of the
ORDINARY SCAN. Atlas needs a dedicated endpoint with a retention window; Beacon
needs one boolean. If you are choosing between integrating two systems, that
single design difference is worth more than any rate limit comparison.

## 3. Do webhooks fix it?

Every delivery is treated as a HINT ("something about record X changed") and
triggers a re-read. Trusting the payload would layer an ordering bug on a
delivery bug.

| Configuration | Missed | p50 detection | p95 | Worst | Calls |
| --- | --- | --- | --- | --- | --- |
| poll only | 67 / 1,188 | 154.1s | 295.0s | 17,802.6s | 213 |
| webhook only | 44 / 1,188 | 6.0s | 10.0s | 39.9s | 1,165 |
| webhook + poll | **4 / 1,188** | 6.1s | 36.0s | 2,219.3s | 1,357 |
| webhook + poll, receiver outage | 51 / 1,188 | 111.6s | 291.5s | 17,802.6s | 485 |

Webhooks plus polling is 25x faster to detect and misses 17x less, and it costs
6.4x the calls. Webhooks alone are fast and still miss 44 changes: best-effort
delivery is a latency optimization, not a correctness mechanism. 49 duplicate
deliveries were absorbed by de-duplicating on the event id.

The prediction held. The finding is in the fourth row.

### The failure worth naming

In the outage arm the receiver was down from t=7,200 to t=10,800. The vendor
tripped its consecutive-failure threshold and disabled the subscription at
t=7,744.6 seconds. It never re-enabled. 881 further events were dropped at the
source, including every one after the receiver came back healthy.

From inside the receiver, a dead subscription and a quiet period look
identical. That is why subscription STATUS has to be monitored rather than
receipt rate, and why the poll has to stay even when the webhooks are working.

The webhook-to-poll delta is 58: the number of changes the poll caught that the
webhook path did not deliver first. That single metric is how much your webhook
channel can be trusted, and almost nobody measures it.

## 4. What reconciliation costs and what it catches

Five strategies against the drift the best incremental configuration leaves
behind on Atlas. 58 wrong records, of which 57 are wrong VALUES and 1 is a
ghost:

| Strategy | Calls | Calls per 1k records | Detected | Detection rate |
| --- | --- | --- | --- | --- |
| none | 0 | 0.00 | 0 / 58 | 0.0000 |
| count check | 1 | 0.25 | 0 / 58 | 0.0000 |
| partitioned checksum | 41 | 10.25 | 1 / 58 | 0.0172 |
| full id inventory | 4 | 1.00 | 1 / 58 | 0.0172 |
| full field compare | 20 | 5.00 | **58 / 58** | **1.0000** |

```
bar length = detection rate -- NOT the call count in brackets

count check                                                         0.0000  (1 call)
full id inventory     #                                             0.0172  (4 calls)
partitioned checksum  #                                             0.0172  (41 calls)
full field compare    ############################################  1.0000  (20 calls)
```

The accuracy column's denominator is upstream-live records, and ghosts are not
in it. A ghost is a record upstream deleted and the connector still holds, so by
construction it cannot lower a figure computed over the records upstream still
has. That is why the "+ deletes endpoint" row removes 103 of 104 ghosts and
moves Accuracy by 0.0000. Counted against everything the connector holds, naive
Atlas is 0.9575 rather than 0.9831. Both numbers are real; the columns answer
different questions, and the ghost column is the one to read for deletions.

A count check detects nothing. It names no record even when the counts DO
differ, which here they do: 3,897 local against 3,896 upstream, a single ghost.
A count check that is off by one tells you a number is wrong and cannot tell you
which record, so it names nothing and the strategy scores 0.0000 anyway. The
rest of the drift is wrong values, and a count of records is blind to every one
of them. The prediction that a count check catches most drift is refuted at
0.0000.

Only a field comparison sees a silently wrong value. The identifier is present
and correct on both sides; only the value differs. Counts, checksums over ids,
and full id inventories are all structurally incapable of seeing it, and that is
57 of the 58 problems.

### The cost ordering is not the expected one, and it depends on scale

At 4,000 records the full field comparison costs 20 calls, five times the
cheapest useful check, and catches everything. The partitioned checksum costs
41 calls and is strictly worse than simply listing every id, because it pays a
fixed probe cost per partition and there are only four pages of ids to list.

A partitioned checksum only starts winning once a full id inventory stops
fitting in the call budget. The per-1,000-record column is the figure that
transfers; the absolute counts do not.

## Reproducing it

```
python3 scripts/exp1_rate_limits.py
python3 scripts/exp2_incremental_loss.py
python3 scripts/exp3_webhooks_vs_polling.py
python3 scripts/exp4_reconciliation.py
```

Under a minute in total. Run them twice and diff `results/`. The files are
byte-for-byte identical, which is what the simulated clock is for.

```
pip install pytest
python -m pytest -q
python3 scripts/check_readme_numbers.py
python3 scripts/charts.py
```

## Claims backed by tests

The rows the results depend on are mutation-checked and say so.

| Claim | Test |
| --- | --- |
| Every published chart says what its bars mean | `tests/test_charts.py::test_every_published_chart_says_what_its_bars_mean` |
| A nonzero value is never charted as an empty bar | `tests/test_charts.py::test_a_nonzero_value_is_never_drawn_as_an_empty_bar` |
| A true zero is charted as no bar at all | `tests/test_charts.py::test_a_zero_is_drawn_as_no_bar_at_all` |
| A bar is proportional to its value | `tests/test_charts.py::test_a_bar_is_proportional_to_its_value` |
| A negative value is refused rather than charted | `tests/test_charts.py::test_a_negative_value_is_refused_rather_than_drawn` |
| The ghost ladder is five flat bars and a cliff | `tests/test_charts.py::test_the_ghost_ladder_is_five_flat_bars_and_a_cliff` |
| Every published chart renders from the shipped results | `tests/test_charts.py::test_every_published_chart_renders_from_the_shipped_results` |
| The clock refuses to run backward | `tests/test_sim.py::test_the_clock_refuses_to_go_backward` |
| A skewed vendor clock reports a different now | `tests/test_sim.py::test_a_skewed_clock_reports_a_different_now` |
| The world is a pure function of the seed | `tests/test_sim.py::test_the_world_is_a_pure_function_of_the_seed` |
| Nothing in the generator reads the clock or a random stream | `tests/test_sim.py::test_nothing_in_the_generator_reads_the_clock_or_a_random_stream` |
| No mutation carries a timestamp from the future (mutation-checked: build the timeline in sequence order and the watermark jumps to the end of the run) | `tests/test_sim.py::test_no_mutation_carries_a_timestamp_from_the_future` |
| A silent update does not advance the stamp | `tests/test_sim.py::test_a_silent_update_does_not_advance_the_stamp` |
| A late-clock update is stamped behind its moment | `tests/test_sim.py::test_a_late_clock_update_is_stamped_behind_its_moment` |
| Every mutation kind in the mix actually occurs | `tests/test_sim.py::test_every_mutation_kind_in_the_mix_actually_occurs` |
| Every call costs simulated time (mutation-checked: make a call free and naive completes in 0.0 seconds with 100,000 requests) | `tests/test_sim.py::test_every_call_costs_simulated_time` |
| A rejected call costs time too | `tests/test_sim.py::test_a_rejected_call_costs_time_too` |
| A daily cap raises a different exception from a rate limit | `tests/test_sim.py::test_a_daily_cap_raises_a_different_exception_from_a_rate_limit` |
| Atlas hard-deletes and Beacon archives | `tests/test_sim.py::test_atlas_hard_deletes_and_beacon_archives` |
| The deletes endpoint forgets beyond its retention window | `tests/test_sim.py::test_the_deletes_endpoint_forgets_beyond_its_retention_window` |
| The scan orders by timestamp then id | `tests/test_sim.py::test_the_scan_orders_by_timestamp_then_id` |
| Beacon's search truncates silently at its ceiling | `tests/test_sim.py::test_beacon_search_truncates_silently_at_its_ceiling` |
| A token bucket paces to its rate and permits a burst | `tests/test_sim.py::test_a_token_bucket_paces_to_its_rate` |
| AIMD decreases multiplicatively and increases additively | `tests/test_sim.py::test_aimd_decreases_multiplicatively_and_increases_additively` |
| A limiter honors Retry-After rather than its own backoff | `tests/test_sim.py::test_a_limiter_honors_retry_after_rather_than_its_own_backoff` |
| Upsert is idempotent on the source id | `tests/test_connector.py::test_upsert_is_idempotent_on_the_source_id` |
| An out-of-order delivery cannot revert a newer record (mutation-checked: remove the version check and event 1 after event 2 undoes it) | `tests/test_connector.py::test_an_out_of_order_delivery_cannot_revert_a_newer_record` |
| The watermark is set from the START of the backfill | `tests/test_connector.py::test_the_watermark_is_set_from_the_START_of_the_backfill` |
| An incremental pass advances the watermark only to what it saw | `tests/test_connector.py::test_an_incremental_pass_advances_the_watermark_only_to_what_it_saw` |
| A quota exhaustion during backfill is not retried forever | `tests/test_connector.py::test_a_quota_exhaustion_during_backfill_is_not_retried_forever` |
| A silent update is never reflected by a watermark scan | `tests/test_connector.py::test_a_silent_update_is_never_reflected_by_a_watermark_scan` |
| Pacing helps under a rate ceiling | `tests/test_results_invariants.py::test_pacing_helps_under_a_rate_ceiling` |
| The naive limiter wastes calls it did not need (mutation-checked: make rejected calls free and naive looks strictly better) | `tests/test_results_invariants.py::test_the_naive_limiter_wastes_calls_it_did_not_need` |
| No limiter strategy survives a daily cap | `tests/test_results_invariants.py::test_no_limiter_strategy_survives_a_daily_cap` |
| The access pattern dominates the limiter choice | `tests/test_results_invariants.py::test_the_access_pattern_dominates_the_limiter_choice` |
| The bulk path trades calls for latency | `tests/test_results_invariants.py::test_the_bulk_path_trades_calls_for_latency` |
| The N+1 pattern cannot fit under the cap | `tests/test_results_invariants.py::test_the_n_plus_one_pattern_cannot_fit_under_the_cap` |
| Incremental sync does not catch everything | `tests/test_results_invariants.py::test_incremental_sync_does_not_catch_everything` |
| A silent update is missed at every setting | `tests/test_results_invariants.py::test_a_silent_update_is_missed_at_every_setting` |
| Only a deletes mechanism removes ghost records | `tests/test_results_invariants.py::test_only_a_deletes_mechanism_removes_ghost_records` |
| Archive semantics detect deletion without a special endpoint | `tests/test_results_invariants.py::test_archive_semantics_detect_deletion_without_a_special_endpoint` |
| A wider overlap never makes accuracy worse | `tests/test_results_invariants.py::test_a_wider_overlap_never_makes_accuracy_worse` |
| The overlap window has sharp diminishing returns | `tests/test_results_invariants.py::test_the_overlap_window_has_sharp_diminishing_returns` |
| Webhooks detect changes far sooner than polling | `tests/test_results_invariants.py::test_webhooks_detect_changes_far_sooner_than_polling` |
| Webhooks alone still miss changes | `tests/test_results_invariants.py::test_webhooks_alone_still_miss_changes` |
| Webhooks plus polling beats either alone | `tests/test_results_invariants.py::test_webhooks_plus_polling_beats_either_alone` |
| A receiver outage permanently disables the subscription | `tests/test_results_invariants.py::test_a_receiver_outage_permanently_disables_the_subscription` |
| The webhook-to-poll delta is published | `tests/test_results_invariants.py::test_the_webhook_to_poll_delta_is_published` |
| A count check catches essentially nothing | `tests/test_results_invariants.py::test_a_count_check_catches_essentially_nothing` |
| Only a field comparison sees a silently wrong value | `tests/test_results_invariants.py::test_only_a_field_comparison_sees_a_silently_wrong_value` |
| A full field comparison detects everything | `tests/test_results_invariants.py::test_a_full_field_comparison_detects_everything` |
| Drift is dominated by wrong values, not missing records | `tests/test_results_invariants.py::test_drift_is_dominated_by_wrong_values_not_missing_records` |
| All four experiments measured the same generated world | `tests/test_results_invariants.py::test_all_four_experiments_measured_the_same_generated_world` |

## What this does not measure

- **A model of a vendor, not a vendor.** Atlas and Beacon implement a SHAPE
  taken from how systems of that kind publicly behave. No real Salesforce or
  HubSpot endpoint is called, no account is used, and nothing here is evidence
  about either product's actual behavior.
- **No wall-clock performance claim.** Simulated seconds are a property of the
  algorithm and the modeled limits. Nothing here says how fast a real connector
  runs.
- **Synthetic mutations.** The mix of change kinds, their rate and their
  clustering are invented. Every loss rate is a function of that mix. The
  ORDERING, which kinds are unfixable by which mitigation, is what transfers.
- **One tenant, one scale, one object type.** Multi-tenant fairness, budget
  partitioning across tenants and scheduler behavior are not measured. The
  reconciliation cost ordering in section 4 is explicitly scale-dependent.
- **No real authentication.** Token lifecycle is not modeled at all; no OAuth
  handshake happens against anything.
- **Not evidence of CRM domain experience.** The setting is chosen because it
  makes the constraints concrete.

## Related repositories

[Roster-entity-resolution](https://github.com/jkelly-dev1/roster-entity-resolution)
is the one to read directly against this repository. It takes four rosters that
already disagree and decides which records are the same provider, treating the
matching threshold as a cost decision. This repository is the step before that
one: how the rosters get out of the source systems, and what is already missing
by the time anybody tries to match them. The two are independent and share no
code; that one needs a Postgres container, this one needs nothing.

[context-quality-ablation](https://github.com/jkelly-dev1/context-quality-ablation)
is the step AFTER this one. It takes context assembled from records like these
and measures whether the quality of that context changes what a model answers,
across ten single-variable conditions and two model vendors. The failure kinds
measured here appear there as its stale and incomplete conditions: this
repository says what a sync loses, that one says whether losing it changes the
answer.

All three follow the same rules: no claim without a test, mutation checks on the
tests that matter, every number re-derived from the shipped results by script,
and predictions recorded before the run so the refuted ones survive. Two of the
four predictions here were refuted, one was qualified, and all of them are
still in the code.

## License

MIT. See `LICENSE`.
