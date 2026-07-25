/**
 * @see https://prettier.io/docs/en/configuration.html
 *
 * Plugin require() resolves relative to this file (then walks up to the
 * workspace root node_modules). Bazel includes the plugin via the prettierrc
 * js_library deps so the hermetic prettier binary can resolve it from
 * runfiles. PATH prettier (tools image) relies on NODE_PATH pointing at
 * /usr/local/lib/node_modules where the plugin is packaged.
 */
const config = {
  tabWidth: 2,
  plugins: [
    // require() so resolution starts from this file, not prettier's install dir.
    require("prettier-plugin-svelte"),
  ],
};

// eslint-disable-next-line no-undef
module.exports = config;
