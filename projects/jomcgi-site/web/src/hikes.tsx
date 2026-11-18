import { HikesPage } from "./hikes-page";
import { mount } from "./mount";

mount(
  <HikesPage
    published={document.getElementById("hikes-published")?.innerHTML}
  />,
);
