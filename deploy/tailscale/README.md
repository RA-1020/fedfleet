# Tailnet policy

`policy.hujson` is the tailnet's access policy, kept here so every change is reviewed and recorded.

Machines and tags (set in the admin console, Machines > Edit ACL tags):

| Machine | Tags |
| --- | --- |
| fedfleet-agg (Oracle, Mumbai) | tag:aggregator, tag:region-ap-mumbai |
| fedfleet-site2 (Azure) | tag:site, tag:residency-apac |
| rohaans-macbook-air | none (the owner's device; runs development sites 0 and 1) |

Rules in short: the owner reaches everything; other members reach the aggregator's SSH port
only; a site reaches the aggregator's fleet port (9092) only, and only if its residency tag
allows the aggregator's region. Both servers run Tailscale SSH, so SSH access is decided by the
`ssh` section, not by authorized_keys.

To change it: edit this file in a pull request, paste it into the JSON editor, save (the
`tests` run on save), then check `ssh ubuntu@fedfleet-agg 'echo ok'` and `nc -zv fedfleet-agg 9092`.
Residency check of 24 Nov 2026: with tag:residency-apac the Azure site took part in a 2-round run
(3 sites online); with tag:residency-eu it could not connect (2 sites online).
