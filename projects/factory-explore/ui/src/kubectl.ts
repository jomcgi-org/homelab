let compiled: Promise<WebAssembly.Module> | undefined;

export function loadKubectl() {
  return (compiled ??= new Promise<WebAssembly.Module>((resolve, reject) => {
    const worker = new Worker("/kubectl-worker.js");
    const fail = () => {
      worker.terminate();
      reject(new Error("Unable to load kubectl"));
    };
    worker.onerror = fail;
    worker.onmessage = ({ data }) => {
      if (!data.module) return fail();
      worker.terminate();
      resolve(data.module);
    };
    worker.postMessage({ compile: true });
  }).catch((error) => {
    compiled = undefined;
    throw error;
  }));
}

export function schedulePreload() {
  const device = navigator as Navigator & {
    deviceMemory?: number;
    connection?: { saveData?: boolean };
  };
  if (
    device.connection?.saveData ||
    (device.deviceMemory ?? 4) < 4 ||
    matchMedia("(pointer: coarse)").matches
  )
    return;
  const preload = () => void loadKubectl().catch(() => {});
  if (window.requestIdleCallback) {
    const id = window.requestIdleCallback(preload);
    return () => window.cancelIdleCallback(id);
  }
  const id = setTimeout(preload, 0);
  return () => clearTimeout(id);
}
