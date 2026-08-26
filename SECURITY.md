# Security Policy

## Reporting a Vulnerability

Please report suspected vulnerabilities privately, not through public issues or
pull requests.

Preferred channel: use GitHub's private vulnerability reporting for this
repository (the "Report a vulnerability" button on the Security tab). It opens a
private advisory visible only to the maintainer.

Aim is to acknowledge a report within 5 business days and to share a resolution
or mitigation plan within 30 days. Timelines may vary, as this is maintained in
personal time.

Please include enough detail to reproduce the issue: the affected file or
endpoint, the version or commit, steps to reproduce, and the impact you observed.

## Supported Versions

This is a personal learning and portfolio project. Only the latest commit on the
`main` branch is supported; there are no maintained release branches or
backports.

## Scope

These are self-contained demo projects, not production services. This one is
unusually easy to reason about: it has no network, no server, no database, no
container and no credentials of any kind. It is a simulation that runs entirely
in one Python process and writes JSON files. There is nothing to expose and
nothing to authenticate to.

The two "vendors" are fakes. `sim/vendors.py` implements a SHAPE modeled on how
systems of that kind publicly behave. No real API is contacted, no account or
token exists, and nothing in this repository is evidence about any real
product's behavior or security posture.

There is no corpus and no downloaded data. Every record, mutation and webhook
delivery is generated from a fixed seed, so no real person, company or
identifier appears anywhere.

What this repository is about is a security-adjacent concern in its own right.
An integration that silently loses deletions keeps serving records the source
system deleted, which for personal data is a retention and erasure problem
rather than merely a data-quality one. Section 2 measures exactly that: how many
records a connector goes on holding after they are gone upstream, and which
mechanism is the only one that notices.

Limitations that the README documents as deliberate, out-of-scope seams are
noted but may not be actioned.
