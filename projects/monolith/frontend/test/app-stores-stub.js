const value = {
  url: new URL("https://example.test/"),
  status: 404,
  error: null,
  route: { id: "/public/app/grimoire" },
};

export const page = {
  subscribe(run) {
    run(value);
    return () => {};
  },
};
