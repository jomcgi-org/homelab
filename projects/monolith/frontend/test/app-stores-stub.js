const value = {
  url: new URL("https://example.test/"),
};

export const page = {
  subscribe(run) {
    run(value);
    return () => {};
  },
};
