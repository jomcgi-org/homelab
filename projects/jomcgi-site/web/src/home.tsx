import { mount } from "./mount";
import { PAGES } from "./pages";

const Page = PAGES["index.html"]!;
mount(<Page />);
