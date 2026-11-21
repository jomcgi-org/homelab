Run the DNS preflight locally from the repository root with PyYAML installed.
Supply the baseline and rendered workload policies together, for example

```bash
set -o pipefail
( cat projects/platform-factory/network-policy/resources/baseline.yaml &&
  helm template factory projects/factory/chart -n kagent -f projects/factory/deploy/values.yaml
) | python3 projects/platform-factory/network-policy/dns_preflight.py --namespace kagent
```

Each stdout row lists the declaring namespace, policy, external name and coverage.
Exit 1 indicates missing or wildcard-only coverage. Keep the output local.
The comparison checks DNS name rules on port 53 for identical endpoint selectors
or matching subsets of workload labels. It does not simulate runtime reachability.
