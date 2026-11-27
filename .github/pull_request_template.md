<!-- One task, or one phase of a work plan, per pull request. -->

**Task:** <!-- e.g. 2.4 k3s across Oracle and Azure -->

**What changed and why:**

**How it was tested:** <!-- commands run and their result; for a live change, the run name or drill -->

**Live system:**
- [ ] Does not touch the live aggregator, sites or tailnet
- [ ] Touches them: agreed slot ____, rollback ____, the services on the do-not-touch list are unaffected

**Before asking for review:**
- [ ] No secrets, keys, tokens, SAS URLs or private addresses in this PR
- [ ] CI checks pass
