export function qwenFlashApiBase() {
  const base = process.env.QWEN_FLASH_API_BASE;
  if (!base) {
    throw new Error("QWEN_FLASH_API_BASE is required");
  }
  return base.replace(/\/$/, "");
}
