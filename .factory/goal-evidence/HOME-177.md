1. From the repository root, run the extraction with Renovate 44.14.10 and
   compare its JSON output to the YAML files in the checkout.

   ```sh
   LOG_LEVEL=debug LOG_FORMAT=json renovate --platform=local --dry-run=extract --enabled-managers=kubernetes,custom.regex,helmv3 >extract.jsonl
   python3 projects/platform/renovate/check_factory_extraction.py <extract.jsonl
   ```

   The checker prints the manifest, image and update source for each pin.
   Substrate-owned images follow its release. Its PostgreSQL and agentgateway
   images have independent tag and digest pins in
   `projects/platform-factory/substrate/resources/kustomization.yaml`.

2. The grouped update in `renovate.json` runs
   `projects/platform-factory/substrate/render-upstream.sh --refresh` and
   includes the generated files in its branch. The required
   `//projects/factory/chart:chart_test` compares both kagent versions with
   the renderer version and checks the vendored CRDs.
