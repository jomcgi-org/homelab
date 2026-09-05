export const PREEMPTION_COPY =
  "The machine running this database was shut down at short notice, which is normal on the cheap capacity it runs on. It is coming back automatically with its data intact.";

const PHASE_COPY = {
  confirming: "It is checking whether the machine is really gone.",
  restoring: "It is coming back with its data.",
  cold: "It is starting fresh.",
};

export function preemptionCopy(phase) {
  const phaseCopy = PHASE_COPY[phase];
  return phaseCopy ? `${PREEMPTION_COPY} ${phaseCopy}` : PREEMPTION_COPY;
}
