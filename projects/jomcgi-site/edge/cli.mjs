import { main } from "./apply.mjs";

try {
  await main();
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
