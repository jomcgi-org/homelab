"""Semgrep Pro rule-pack digests baked into the Firecracker/EmberVM guest image.

Manual pin (like semgrep_experimental). Intentionally NOT updated by
.github/workflows/update-semgrep-pro.yaml so CI digest churn does not rebuild the
deployable guest. Bump deliberately when shipping new guest rule packs; that
change rebuilds the guest and requires chart bumps for fc-invoke + embervm.
"""

SEMGREP_GUEST_DIGESTS = {
    "rules_golang": "sha256:3b4962725eeba008159cff4140cab426439277a8f4cc80187690ba5154d0d11b",
    "rules_python": "sha256:3d86725726d4cd26607cf752d524980cc4778da0c1a67f9e0157cb0b30e5ae66",
    "rules_javascript": "sha256:d9789e2eba75c0cb1317a4a1b1838bae6a571e6bf83c87992f762221b95ba69c",
    "rules_kubernetes": "sha256:eaeeeff194bad2f8ab7433a172e6968b853a2cf3be358563b1134f0b4a447602",
    "rules_rust": "sha256:14f66ffe8d3250b8855a0fced76de01942bb30b52d7be40d123e874ab337a7d7",
}
