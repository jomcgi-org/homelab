// Fixture transport only. Production still uses SvelteKit's deserialize and
// unchanged action/proxy modules. These tests assert UI outcomes, not wire encoding.
export const deserialize = JSON.parse;
