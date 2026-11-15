// The Worker must send this as a response header too, adding frame-ancestors
// 'none' (which browsers ignore in a meta policy). Vite emits the meta policy
// only in builds; its development CSS/HMR transport is not the shipped app.
export const contentSecurityPolicy =
  "default-src 'none'; script-src 'self'; style-src 'self'; " +
  "img-src 'self' data:; font-src 'self'; connect-src 'self'; " +
  "base-uri 'none'; form-action 'none'";
