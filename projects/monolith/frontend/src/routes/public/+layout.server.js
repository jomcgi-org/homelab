export function load() {
  return {
    maintenanceBanner: process.env.PUBLIC_MAINTENANCE_BANNER || "",
  };
}
