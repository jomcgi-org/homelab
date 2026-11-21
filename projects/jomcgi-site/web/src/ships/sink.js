export function product(key, signal) {
  if (
    !/^(latest\.json|(?:snapshot|heat|tracks\/\d{1,15})-[a-f0-9]{64}(?:-[A-Z2-7]{26})?\.json)$/.test(
      key,
    )
  )
    throw new Error("Invalid Ships object key");
  return fetch("https://factory-data.jomcgi.dev/ships/" + key, {
    signal,
    credentials: "omit",
  });
}
