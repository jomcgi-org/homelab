import config from "../vitest.config.js";

export default {
  ...config,
  test: {
    ...config.test,
    include: [
      "src/routes/public/ember/agents/agents.dom.test.js",
      "src/lib/public/agentstory/timeline.test.js",
      "src/routes/public/profile-shell.dom.test.js",
      "src/lib/public/styles/technical-drawing.test.js",
    ],
    maxWorkers: 2,
  },
};
