import Config

# Force exqlite to compile its bundled sqlite3.c amalgamation from source via
# elixir_make instead of downloading a precompiled NIF from GitHub releases. The
# RBE build executor is offline, so the download path (cc_precompiler's default)
# would fail; from-source uses the executor's cc/make. The build host and every
# cluster node are amd64, so the resulting amd64 NIF is correct for deployment.
config :exqlite, force_build: true
